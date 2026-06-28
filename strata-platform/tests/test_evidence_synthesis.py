"""Evidence Synthesis: two-pass generation with an automated groundedness gate.

The trust invariants, all checked here with an injected FakeReasoner (no network):
- an unsupported claim is filtered (audit trail), not shown in the brief;
- groundedness_score = retained / generated;
- every retained claim carries non-empty provenance;
- every claim_id in the narrative exists in the retained set (no invented facts);
- an empty store fails loud (no silent empty dossier).
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from strata_platform.capabilities.evidence_synthesis import EvidenceSynthesis
from strata_platform.substrate.contracts import CapabilityRequest, Chunk, Decision, DocType
from strata_platform.substrate.store import InMemoryStore


class FakeReasoner:
    """Scripted three-pass reasoner: extract → gate (per claim) → compose."""

    def __init__(self) -> None:
        self._gate_calls = 0

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        s = system or ""
        if "extract the factual claims" in s:
            return json.dumps({"claims": [
                {"dimension": "efficacy", "text": "PFS benefit was shown",
                 "chunk_indices": [0]},
                {"dimension": "economic", "text": "Cured the disease entirely",
                 "chunk_indices": [0]},
            ]})
        if "one word" in s:  # gate — first SUPPORTED, second UNSUPPORTED
            self._gate_calls += 1
            return "SUPPORTED" if self._gate_calls == 1 else "UNSUPPORTED"
        if "dossier-style evidence narrative" in s:
            # reference the retained claim (the first "<id>: text" line in the prompt)
            cid = prompt.split("Claims:\n", 1)[1].split(":", 1)[0].strip()
            return json.dumps({"paragraphs": [
                {"text": "The evidence shows a PFS benefit.", "claim_ids": [cid]},
                {"text": "Invented paragraph.", "claim_ids": ["deadbeef"]},  # bad id
            ]})
        return "[]"


def _decision() -> Decision:
    return Decision(decision_id="TA1156", decision_date=date(2026, 5, 21),
                    drug="osimertinib", indication="nsclc")


def _store() -> InMemoryStore:
    s = InMemoryStore()
    s.add([Chunk(text="Progression-free survival benefit vs chemotherapy.",
                 doc_type=DocType.literature, drug="osimertinib",
                 doc_date=date(2024, 1, 1), source_id="PMID:1")])
    return s


def _run():
    cap = EvidenceSynthesis()
    req = CapabilityRequest(capability="evidence_synthesis", decision=_decision())
    return cap.run(req, reasoner=FakeReasoner(), store=_store())


def test_unsupported_claim_filtered_and_score() -> None:
    payload = _run().payload
    brief_texts = [c["text"] for dim in payload["brief"] for c in dim["claims"]]
    assert "PFS benefit was shown" in brief_texts
    assert "Cured the disease entirely" not in brief_texts        # filtered by the gate
    assert any(c["text"] == "Cured the disease entirely"
               for c in payload["filtered_claims"])
    assert payload["groundedness_score"] == 0.5                   # 1 retained / 2 generated


def test_every_retained_claim_has_provenance() -> None:
    payload = _run().payload
    for dim in payload["brief"]:
        for c in dim["claims"]:
            assert c["provenance"]["chunk_ids"] and c["provenance"]["source_ids"]


def test_narrative_references_only_retained_claims() -> None:
    payload = _run().payload
    retained_ids = {c["claim_id"] for dim in payload["brief"] for c in dim["claims"]}
    assert payload["narrative"]                                   # at least one paragraph
    for para in payload["narrative"]:
        assert para["claim_ids"]
        for cid in para["claim_ids"]:
            assert cid in retained_ids                           # no invented facts
    # the invented-id paragraph was dropped
    assert all("Invented" not in p["text"] for p in payload["narrative"])


def test_empty_store_fails_loud() -> None:
    cap = EvidenceSynthesis()
    req = CapabilityRequest(capability="evidence_synthesis", decision=_decision())
    with pytest.raises(ValueError, match="no in-boundary evidence"):
        cap.run(req, reasoner=FakeReasoner(), store=InMemoryStore())
