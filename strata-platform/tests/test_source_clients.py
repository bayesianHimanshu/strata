"""Source-client wiring with an injected HTTP double (no network).

Covers the hard-won fixes at the call site: openFDA 404->0, label-by-generic_name,
PubMed api_key passthrough, and that every fetch produces a content-addressed
SourceRecord with the right doc_type.
"""
from __future__ import annotations

from strata_platform.sources.clinicaltrials import ClinicalTrialsClient
from strata_platform.sources.openfda import OpenFDAClient
from strata_platform.sources.pubmed import PubMedClient
from strata_platform.substrate.contracts import DocType


class _Resp:
    def __init__(self, *, status_code=200, payload=None, text="", url="https://x"):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text
        self.url = url
        self.content = (text or str(self._payload)).encode()

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"unexpected raise_for_status {self.status_code}")

    def json(self):
        return self._payload


class _Client:
    """Records the last (url, params) and returns a scripted response."""

    def __init__(self, resp: _Resp):
        self._resp = resp
        self.last_url = None
        self.last_params = None

    def get(self, url, params=None, headers=None):
        self.last_url = url
        self.last_params = params
        return self._resp


def test_openfda_404_is_zero_not_error():
    client = _Client(_Resp(status_code=404, text=""))
    fda = OpenFDAClient(client=client, api_key=None)
    docs, rec = fda.fetch_label_docs('openfda.generic_name:"nonexistent"')
    assert docs == []
    assert rec.doc_type == DocType.label
    assert rec.source == "openfda"
    assert len(rec.content_sha256) == 64


def test_openfda_label_query_by_generic_name():
    payload = {
        "meta": {"results": {"total": 1}},
        "results": [{
            "openfda": {"brand_name": ["Tagrisso"], "generic_name": ["osimertinib"]},
            "indications_and_usage": ["For EGFR-mutated NSCLC."],
            "warnings_and_cautions": ["ILD/pneumonitis."],
            "effective_time": "20230601",
        }],
    }
    client = _Client(_Resp(payload=payload))
    fda = OpenFDAClient(client=client, api_key=None)
    docs, _ = fda.fetch_label_docs('openfda.generic_name:"osimertinib"')
    assert len(docs) == 1 and docs[0].generic == "osimertinib"
    assert "EGFR" in docs[0].text
    assert client.last_params["search"] == 'openfda.generic_name:"osimertinib"'


def test_pubmed_api_key_passthrough():
    payload = {"esearchresult": {"count": "3", "idlist": ["1", "2", "3"]}}
    client = _Client(_Resp(payload=payload))
    pm = PubMedClient(client=client, api_key="SECRET")
    result, rec = pm.search("osimertinib")
    assert result.count == 3
    assert client.last_params["api_key"] == "SECRET"
    assert rec.doc_type == DocType.literature


def test_ctgov_search_snapshots_with_trial_doc_type():
    payload = {"studies": [{
        "protocolSection": {
            "identificationModule": {"nctId": "NCT99", "briefTitle": "T"},
            "statusModule": {"overallStatus": "COMPLETED"},
        }
    }], "nextPageToken": None}
    client = _Client(_Resp(payload=payload))
    ct = ClinicalTrialsClient(client=client)
    trials, rec, token = ct.search("nsclc", intervention="osimertinib")
    assert [t.nct_id for t in trials] == ["NCT99"]
    assert token is None
    assert rec.doc_type == DocType.trial_registry
    assert client.last_params["query.intr"] == "osimertinib"
    assert client.last_params["query.cond"] == "nsclc"
