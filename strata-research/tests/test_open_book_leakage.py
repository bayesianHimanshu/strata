"""Phase 2 Task 3 — open-book retrieval leakage correctness (offline, fixtures).

The four binding checks: dossier-disjointness, date cutoff, the INVERSE check (a
legitimate public ICER doc IS retrieved — so we can tell "boundary excludes the
dossier" apart from "boundary excludes the category"), and the sibling policy toggle.
We inspect the EXACT chunks the synthesizer's retrieval surfaced via a recording
reasoner, so these test the real retrieval path the GPT adapter uses.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from agents.synthesizer import EvidenceGapSynthesizer
from core.contracts import Claim, Decision, DocType
from experiments.open_book_synth import OpenAIOpenBookReasoner, make_synthesizer
from index.store import Chunk, InMemoryStore, structure_aware_prefixed_chunks

TARGET = Decision(
    agency="NICE",
    decision_id="TA100",
    decision_date=date(2026, 6, 1),  # buffer 90d → cutoff 2026-03-03
    indication="lung cancer",
    outcome="not_recommended",
    drug="drugx",
    appraisal_id="TA100",
)


def _add(
    store: InMemoryStore, *, sid, appraisal_id, doc_type, doc_date, text, drug="drugx"
) -> None:
    store.add(
        structure_aware_prefixed_chunks(
            text,
            source_id=sid,
            doc_date=doc_date,
            doc_type=doc_type,
            appraisal_id=appraisal_id,
            drug=drug,
        )
    )


def _store() -> InMemoryStore:
    s = InMemoryStore()
    # TA100's OWN dossier — gold-bearing, pre-cutoff date would otherwise admit it.
    _add(s, sid="TA100:guidance", appraisal_id="TA100",
         doc_type=DocType.ta_final_guidance, doc_date=date(2026, 1, 1),
         text="The committee found the ICER highly uncertain and the comparator did "
              "not reflect NHS practice.")
    # A same-drug SIBLING appraisal dossier (registered sibling-policy target).
    _add(s, sid="TA050:guidance", appraisal_id="TA050",
         doc_type=DocType.ta_final_guidance, doc_date=date(2025, 1, 1),
         text="For the same drug the ICER was uncertain and the comparator disputed.")
    # Legitimate PUBLIC literature, pre-cutoff: discusses ICER (inverse check).
    _add(s, sid="PMID:1", appraisal_id=None, doc_type=DocType.literature,
         doc_date=date(2025, 6, 1),
         text="The cost-effectiveness estimate (ICER) was highly uncertain here.")
    # Public literature discussing the comparator, pre-cutoff.
    _add(s, sid="PMID:3", appraisal_id=None, doc_type=DocType.literature,
         doc_date=date(2025, 7, 1),
         text="The comparator did not reflect NHS clinical practice in this study.")
    # Public doc dated AFTER the cutoff — must never be retrieved.
    _add(s, sid="PMID:2", appraisal_id=None, doc_type=DocType.literature,
         doc_date=date(2026, 5, 1),
         text="A later analysis of the ICER and the comparator.")
    return s


class RecordingReasoner:
    """Captures the exact claims retrieval handed the reasoner."""

    def __init__(self) -> None:
        self.claims: list[Claim] = []

    def open_book(self, decision, claims):
        self.claims = list(claims)
        return []

    def closed_book(self, decision):
        return []


def _retrieved_chunks(
    store: InMemoryStore,
    *,
    exclude_siblings: bool = True,
    siblings: tuple[str, ...] = (),
    buffer_days: int = 90,
) -> list[Chunk]:
    rec = RecordingReasoner()
    synth = EvidenceGapSynthesizer(
        store=store, reasoner=rec, exclude_siblings=exclude_siblings
    )
    synth.synthesize(
        TARGET,
        mode="open",
        sibling_appraisal_ids=siblings,
        buffer=timedelta(days=buffer_days),
    )
    by_key = {(c.source_id, c.span.start, c.span.end): c for c in store.chunks}
    return [by_key[(cl.source_id, cl.span.start, cl.span.end)] for cl in rec.claims]


# --- the four leakage checks ------------------------------------------------ #


def test_own_dossier_chunk_never_retrieved() -> None:
    chunks = _retrieved_chunks(_store())
    assert chunks, "retrieval should surface the public evidence"
    assert all(c.appraisal_id != "TA100" for c in chunks)
    assert "TA100:guidance" not in {c.source_id for c in chunks}


def test_every_retrieved_chunk_predates_cutoff() -> None:
    cutoff = TARGET.decision_date - timedelta(days=90)  # 2026-03-03
    chunks = _retrieved_chunks(_store())
    assert all(c.doc_date is not None and c.doc_date < cutoff for c in chunks)
    assert "PMID:2" not in {c.source_id for c in chunks}  # post-cutoff excluded


def test_inverse_nondossier_icer_doc_is_retrieved() -> None:
    # Distinguishes "boundary excludes the dossier" from "excludes the ICER category":
    # the legitimate public ICER doc MUST come through.
    chunks = _retrieved_chunks(_store())
    assert "PMID:1" in {c.source_id for c in chunks}


def test_sibling_policy_toggles_retrieval() -> None:
    on = {c.source_id for c in _retrieved_chunks(
        _store(), exclude_siblings=True, siblings=("TA050",))}
    off = {c.source_id for c in _retrieved_chunks(
        _store(), exclude_siblings=False, siblings=("TA050",))}
    assert "TA050:guidance" not in on  # excluded by the registered policy
    assert "TA050:guidance" in off  # admitted when the policy is off


# --- predict_open_book wiring (grounding = precision filter) ----------------- #


class FakeComplete:
    def __init__(self, reply: str) -> None:
        self.reply = reply

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        return self.reply


def _decisions_file(tmp_path: Path) -> str:
    rows = [
        {"agency": "NICE", "decision_id": "TA100", "decision_date": "2026-06-01",
         "indication": "lung cancer", "outcome": "not_recommended",
         "drug": "drugx", "appraisal_id": "TA100"},
        {"agency": "NICE", "decision_id": "TA050", "decision_date": "2025-01-01",
         "indication": "lung cancer", "outcome": "optimised",
         "drug": "drugx", "appraisal_id": "TA050"},  # same drug → sibling of TA100
    ]
    path = tmp_path / "decisions.json"
    path.write_text(json.dumps(rows))
    return str(path)


def test_predict_open_book_emits_only_grounded_categories(tmp_path: Path) -> None:
    # GPT "predicts" three categories; only those a retrieved chunk supports survive.
    reply = '["icer_uncertainty", "comparator", "missing_pro"]'
    synth = make_synthesizer(
        store=_store(),
        reasoner=OpenAIOpenBookReasoner(FakeComplete(reply)),
        decisions_path=_decisions_file(tmp_path),
    )
    from eval.closed_book_probe import ProbeDecision

    probe = ProbeDecision(
        decision_id="TA100", decision_date=date(2026, 6, 1),
        agency="NICE", drug="drugx", indication="lung cancer",
    )
    cats = synth.predict_open_book(probe, buffer_days=90)
    assert "icer_uncertainty" in cats  # grounded by PMID:1
    assert "comparator" in cats  # grounded by PMID:3
    assert "missing_pro" not in cats  # no retrieved chunk supports it → dropped
