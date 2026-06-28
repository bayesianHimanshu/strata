"""Pure parsers for the JSON sources (no network)."""
from __future__ import annotations

from datetime import date

from strata_platform.sources.clinicaltrials import parse_study
from strata_platform.sources.openfda import parse_meta
from strata_platform.sources.pubmed import parse_esearch

CTGOV_SAMPLE = {
    "protocolSection": {
        "identificationModule": {"nctId": "NCT01234567", "briefTitle": "A Trial"},
        "statusModule": {
            "overallStatus": "COMPLETED",
            "startDateStruct": {"date": "2019-03"},
            "completionDateStruct": {"date": "2022-11-15"},
        },
        "conditionsModule": {"conditions": ["Lung Cancer"]},
        "designModule": {"phases": ["PHASE3"]},
        "outcomesModule": {"primaryOutcomes": [{"measure": "Overall Survival"}]},
    }
}


def test_parse_study_extracts_typed_fields() -> None:
    t = parse_study(CTGOV_SAMPLE)
    assert t.nct_id == "NCT01234567"
    assert t.conditions == ["Lung Cancer"]
    assert t.phase == "PHASE3"
    assert t.start_date == date(2019, 3, 1)
    assert t.completion_date == date(2022, 11, 15)
    assert t.primary_outcomes == ["Overall Survival"]


def test_parse_study_tolerates_missing_sections() -> None:
    t = parse_study({})
    assert t.nct_id == ""
    assert t.conditions == []
    assert t.start_date is None


def test_parse_esearch_counts_and_ids() -> None:
    payload = {"esearchresult": {"count": "42", "idlist": ["1", "2"]}}
    r = parse_esearch("neoplasms", payload)
    assert r.count == 42
    assert r.pmids == ["1", "2"]
    assert parse_esearch("x", {}).count is None


def test_parse_meta_reads_total() -> None:
    payload = {"meta": {"results": {"total": 999}}, "results": [{}, {}]}
    r = parse_meta("antineoplastic", payload)
    assert r.total == 999
    assert r.n_results == 2
