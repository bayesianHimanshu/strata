"""Endpoint & Comparator Landscape: structured-first, deterministic counts, lexicon-first
surrogate flags, grounded implications (no network)."""
from __future__ import annotations

import json
from datetime import date

import pytest

import strata_platform.capabilities.endpoint_landscape as el
from strata_platform.capabilities.endpoint_landscape import (
    EndpointLandscape,
    _canonicalise_endpoints,
    _collect,
)
from strata_platform.sources.clinicaltrials import Arm, Outcome, StructuredTrial, parse_structured
from strata_platform.substrate.contracts import CapabilityRequest
from strata_platform.substrate.store import InMemoryStore


def _trial(nct, primaries, *, comparator_arm=None) -> StructuredTrial:
    arms = []
    if comparator_arm:
        arms = [Arm(label="control", intervention_names=[comparator_arm], is_comparator=True)]
    return StructuredTrial(
        nct_id=nct, title=f"{nct} trial",
        primary_outcomes=[Outcome(measure=m, kind="primary") for m in primaries],
        arms=arms, start_date=date(2023, 1, 1))


TRIALS = [
    _trial("NCT1", ["Progression-Free Survival"], comparator_arm="Chemotherapy"),
    _trial("NCT2", ["Progression-free survival (PFS)"], comparator_arm="Chemotherapy"),
    _trial("NCT3", ["Overall Survival"], comparator_arm="Placebo"),
]


class FakeReasoner:
    """Clusters the two PFS variants together and (wrongly) says PFS is NOT a surrogate —
    the lexicon must override that. Also returns one valid + one invalid implication ref."""

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        if "canonical endpoints" in (system or ""):
            return json.dumps({"clusters": [
                {"canonical": "Progression-Free Survival", "kind": "primary",
                 "raw_variants": ["Progression-Free Survival",
                                  "Progression-free survival (PFS)"],
                 "is_surrogate": False},   # wrong on purpose
            ]})
        if "canonical comparators" in (system or ""):
            return json.dumps({"clusters": [
                {"canonical": "Chemotherapy", "comparator_class": "cytotoxic",
                 "raw_variants": ["Chemotherapy"]}]})
        if "design implications" in (system or ""):
            # one real entry_id (parsed from the prompt) + one invented
            first_id = prompt.split("Entries:\n", 1)[1].split(":", 1)[0].strip()
            return json.dumps({"implications": [
                {"text": "PFS dominates; expect OS-maturity pressure.", "refs": [first_id]},
                {"text": "Invented.", "refs": ["nope1234"]}]})
        return "{}"


def test_parse_structured_reads_outcomes_and_arms() -> None:
    study = {"protocolSection": {
        "identificationModule": {"nctId": "NCT9", "briefTitle": "T"},
        "outcomesModule": {"primaryOutcomes": [{"measure": "PFS"}],
                           "secondaryOutcomes": [{"measure": "OS"}]},
        "armsInterventionsModule": {"armGroups": [
            {"label": "exp", "type": "EXPERIMENTAL", "interventionNames": ["Drug: X"]},
            {"label": "ctrl", "type": "ACTIVE_COMPARATOR", "interventionNames": ["Drug: Chemo"]}]}}}
    t = parse_structured(study)
    assert [o.measure for o in t.primary_outcomes] == ["PFS"]
    assert [o.measure for o in t.secondary_outcomes] == ["OS"]
    assert t.arms[0].is_comparator is False and t.arms[1].is_comparator is True
    assert t.arms[1].intervention_names == ["Chemo"]   # "Drug: " prefix stripped


def test_collect_counts_deterministically() -> None:
    raw_ep, raw_cmp = _collect(TRIALS)
    # the two PFS variants are distinct raw strings, each with its own nct
    assert raw_ep["Progression-Free Survival"]["ncts"] == {"NCT1"}
    assert raw_ep["Progression-free survival (PFS)"]["ncts"] == {"NCT2"}
    assert raw_ep["Overall Survival"]["ncts"] == {"NCT3"}
    assert raw_cmp["Chemotherapy"] == {"NCT1", "NCT2"}      # counted from arms, not a model


def test_lexicon_overrides_model_and_counts_cluster() -> None:
    raw_ep, _ = _collect(TRIALS)
    eps = _canonicalise_endpoints(FakeReasoner(), raw_ep)
    pfs = next(e for e in eps if "Progression-Free" in e.canonical)
    assert pfs.trial_count == 2 and set(pfs.provenance.source_ids) == {"NCT1", "NCT2"}
    assert pfs.is_surrogate is True            # lexicon wins over the model's False
    os = next(e for e in eps if e.canonical == "Overall Survival")
    assert os.is_surrogate is False
    assert all(e.provenance.source_ids for e in eps)   # every entry has provenance


def test_capability_end_to_end_and_implication_refs(monkeypatch) -> None:
    monkeypatch.setattr(el, "fetch_trials_structured",
                        lambda indication, *, as_of, drug=None: TRIALS)
    req = CapabilityRequest(capability="endpoint_landscape",
                            params={"indication": "nsclc"})
    res = EndpointLandscape().run(req, reasoner=FakeReasoner(), store=InMemoryStore())
    p = res.payload
    assert p["trials_analysed"] == 3
    assert any(e["canonical"] == "Chemotherapy" and e["trial_count"] == 2
               for e in p["comparators"])
    valid = {e["entry_id"] for e in p["endpoints"]} | {e["entry_id"] for e in p["comparators"]}
    valid |= {n for e in p["endpoints"] for n in e["provenance"]["source_ids"]}
    assert p["implications"]                   # at least one survived
    for imp in p["implications"]:
        assert imp["refs"] and all(r in valid for r in imp["refs"])  # no invented refs


def test_empty_trial_set_fails_loud(monkeypatch) -> None:
    monkeypatch.setattr(el, "fetch_trials_structured",
                        lambda indication, *, as_of, drug=None: [])
    req = CapabilityRequest(capability="endpoint_landscape",
                            params={"indication": "nsclc"})
    with pytest.raises(ValueError, match="no trials found"):
        EndpointLandscape().run(req, reasoner=FakeReasoner(), store=InMemoryStore())


def test_requires_indication() -> None:
    with pytest.raises(ValueError, match="requires params.indication"):
        EndpointLandscape().run(CapabilityRequest(capability="endpoint_landscape"),
                                reasoner=FakeReasoner(), store=InMemoryStore())
