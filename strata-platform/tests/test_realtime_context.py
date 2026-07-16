"""Real-time context subsystem (no network): dedup/embed-once, boundary modes, external
add, per-connector progress, capability-after-ingest, and SSRF rejection."""
from __future__ import annotations

from datetime import date

import pytest

from strata_platform.context.connectors import (
    ContextQuery,
    ContextRecord,
    GenericConnector,
    UrlNotAllowed,
)
from strata_platform.context.ingest import IngestionService
from strata_platform.substrate.boundary import RetrievalBoundary
from strata_platform.substrate.contracts import Chunk, Decision, DocType, SourceRecord
from strata_platform.substrate.store import InMemoryStore


class FakeEmbedder:
    dim = 8

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [[float(len(t) % 7)] * self.dim for t in texts]

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]


def _record(text: str, *, drug="osimertinib", d=date(2024, 1, 1),
            source_id="PMID:1") -> ContextRecord:
    content = text.encode()
    rec = SourceRecord(source="pubmed", source_id=source_id, doc_type=DocType.literature,
                       drug=drug, doc_date=d, content_sha256=SourceRecord.hash_content(content))
    chunk = Chunk(text=text, doc_type=DocType.literature, drug=drug, doc_date=d,
                  source_id=source_id)
    return ContextRecord(source_record=rec, chunks=[chunk])


class FakeConnector:
    def __init__(self, key, records) -> None:
        self.key = key
        self._records = records

    def fetch(self, q: ContextQuery) -> list[ContextRecord]:
        return list(self._records)


def _query() -> ContextQuery:
    return ContextQuery(drug="osimertinib", indication="nsclc", as_of=date(2025, 1, 1))


def test_ingest_dedups_and_embeds_once() -> None:
    store, emb = InMemoryStore(), FakeEmbedder()
    rec = _record("overall survival immature")
    svc = IngestionService(store, emb, {"pubmed": FakeConnector("pubmed", [rec])},
                           freshness_ttl_hours=0)  # TTL 0 -> never short-circuit on freshness
    s1 = svc.ingest(_query(), connectors=["pubmed"])
    s2 = svc.ingest(_query(), connectors=["pubmed"])      # same record again
    assert s1.total_new == 1
    assert s2.total_new == 0                              # dedup: nothing new
    assert emb.calls == 1                                 # embed-once across both ingests
    assert len(store.chunks) == 1


def test_freshness_ttl_serves_from_cache() -> None:
    store, emb = InMemoryStore(), FakeEmbedder()
    svc = IngestionService(store, emb, {"pubmed": FakeConnector("pubmed", [_record("x")])},
                           freshness_ttl_hours=24)
    svc.ingest(_query(), connectors=["pubmed"])
    s2 = svc.ingest(_query(), connectors=["pubmed"])
    assert s2.per_connector["pubmed"]["cached"] == 1     # within TTL -> cached, no re-fetch


def test_progress_callback_fires_per_connector() -> None:
    store, emb = InMemoryStore(), FakeEmbedder()
    svc = IngestionService(store, emb, {"pubmed": FakeConnector("pubmed", [_record("x")])},
                           freshness_ttl_hours=0)
    events: list[dict] = []
    svc.ingest(_query(), connectors=["pubmed"], progress=lambda e: events.append(dict(e)))
    states = [e["state"] for e in events if e["connector"] == "pubmed"]
    assert "fetching" in states and "done" in states


def test_connector_error_is_fail_soft() -> None:
    class Boom:
        def fetch(self, q):
            raise RuntimeError("api down")
    store, emb = InMemoryStore(), FakeEmbedder()
    svc = IngestionService(store, emb, {"pubmed": Boom()}, freshness_ttl_hours=0)
    s = svc.ingest(_query(), connectors=["pubmed"])
    assert s.per_connector["pubmed"]["errors"] == 1      # recorded, not raised


def test_live_excludes_future_backtest_excludes_buffer() -> None:
    future = Chunk(text="t", doc_type=DocType.literature, drug="osimertinib",
                   doc_date=date(2025, 6, 1), source_id="s")
    inside = Chunk(text="t", doc_type=DocType.literature, drug="osimertinib",
                   doc_date=date(2026, 4, 1), source_id="s")  # within backtest buffer
    eligible = Chunk(text="t", doc_type=DocType.literature, drug="osimertinib",
                     doc_date=date(2024, 1, 1), source_id="s")
    live = RetrievalBoundary.live(frozenset({"osimertinib"}), as_of=date(2025, 1, 1))
    assert live.admits(future) is False        # dated after as_of
    assert live.admits(eligible) is True
    d = Decision(decision_id="TA1156", decision_date=date(2026, 5, 21),
                 drug="osimertinib", indication="nsclc")
    bt = RetrievalBoundary.backtest(d, buffer_days=90)
    assert bt.admits(inside) is False          # within the 90d leakage buffer
    assert bt.admits(eligible) is True


def test_add_external_text_is_retrievable_and_tagged() -> None:
    store, emb = InMemoryStore(), FakeEmbedder()
    svc = IngestionService(store, emb, {})
    rec = svc.add_external(kind="text", value="A new external trial readout: PFS benefit.",
                           drug="osimertinib", indication="nsclc", doc_date=date(2024, 6, 1))
    assert rec.source_record.doc_type == DocType.external
    assert rec.source_record.drug == "osimertinib"
    b = RetrievalBoundary.live(frozenset({"osimertinib"}), as_of=date(2025, 1, 1))
    hits = store.search("external trial PFS", b, k=5)
    assert any(h.doc_type == DocType.external for h in hits)   # retrievable for that drug


def test_url_connector_rejects_non_allowlisted_and_private() -> None:
    g = GenericConnector()
    # default-closed: empty allowlist rejects any URL
    with pytest.raises(UrlNotAllowed):
        g.add(kind="url", value="https://example.com/x", drug="d", indication=None,
              doc_date=date(2024, 1, 1))
    with pytest.raises(UrlNotAllowed):
        g.add(kind="url", value="http://169.254.169.254/latest/meta-data/", drug="d",
              indication=None, doc_date=date(2024, 1, 1))


def test_nice_connector_index_per_ta_crawl() -> None:
    """The NICE live-horizon crawl: index xlsx -> recent slice -> molecule match -> per-TA
    guidance -> dossier ContextRecord, all offline (in-memory xlsx + fake guidance)."""
    import io

    import openpyxl

    from strata_platform.context.connectors import NICEConnector
    from strata_platform.sources.nice_guidance import GuidanceResult, ParsedGuidance

    header = ["TA ID", "Year of Publication", "Type", "Technology", "Route",
              "Indication", "Categorisation"]
    rows = [
        ["TA1000", "2025/26", "single", "Osimertinib", "po", "EGFR NSCLC", "Optimised"],
        ["TA1001", "2025/26", "single", "Nivolumab", "iv", "Melanoma", "Recommended"],
        ["TA1002", "2024/25", "single", "Pembrolizumab", "iv", "NSCLC", "Recommended"],
        ["TA1003", "2026/27", "single", "Olaparib", "po", "Ovarian", "Optimised"],
        ["TA1004", "2024/25", "multi", "Lenvatinib", "po", "RCC", "Recommended"],
        ["TA1005", "2025/26", "single", "Atezolizumab", "iv", "Bladder", "Recommended"],
        ["TA003", "2018/19", "single", "Osimertinib", "po", "Old NSCLC", "Recommended"],
    ]
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Cancer recommendations"
    ws.append(header)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)

    class FakeGuidance:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def fetch(self, ta_id: str) -> GuidanceResult:
            self.calls.append(ta_id)
            return GuidanceResult(
                ta_id=ta_id, status="ok",
                parsed=ParsedGuidance(
                    ta_id=ta_id, title=f"{ta_id} osimertinib",
                    published_date=date(2025, 9, 1),
                    rationale_raw="The ICER was highly uncertain; OS data immature. " * 4))

    fg = FakeGuidance()
    conn = NICEConnector(index_bytes=buf.getvalue(), guidance_client=fg)
    q = ContextQuery(drug="osimertinib", indication="nsclc", as_of=date(2026, 6, 1))
    records = conn.fetch(q)

    # only the recent osimertinib TA is crawled (TA1001 wrong drug, TA003 too old)
    assert fg.calls == ["TA1000"]
    assert len(records) == 1
    rec = records[0]
    assert rec.source_record.doc_type == DocType.ta_final_guidance
    assert rec.source_record.appraisal_id == "TA1000"
    assert rec.chunks and all(c.drug == "osimertinib" for c in rec.chunks)


def test_context_api_add_and_status_roundtrip() -> None:
    from fastapi.testclient import TestClient

    from strata_platform.api.main import app

    c = TestClient(app)
    r = c.post("/context/add", json={
        "kind": "text", "value": "External readout: durable response observed.",
        "drug": "pembrolizumab", "indication": "nsclc", "doc_date": "2024-02-01"})
    assert r.status_code == 200
    body = r.json()
    assert body["doc_type"] == "external" and body["chunks"] >= 1
    st = c.get("/context/status", params={"drug": "pembrolizumab"}).json()
    assert st["indexed_chunks"] >= 1
    assert "external" in st["by_source"]


def test_context_api_rejects_bad_url() -> None:
    from fastapi.testclient import TestClient

    from strata_platform.api.main import app

    c = TestClient(app)
    r = c.post("/context/add", json={"kind": "url", "value": "https://example.com",
                                     "drug": "d", "doc_date": "2024-01-01"})
    assert r.status_code == 400
    assert "url rejected" in r.json()["detail"]


def test_capability_runs_against_ingested_context() -> None:
    from strata_platform.capabilities.hta_archaeology import HTAArchaeology
    from strata_platform.substrate.contracts import CapabilityRequest
    from strata_platform.substrate.reasoner import KeywordReasoner

    store, emb = InMemoryStore(), FakeEmbedder()
    svc = IngestionService(store, emb, {}, freshness_ttl_hours=0)
    svc.add_external(kind="text",
                     value="The ICER was highly uncertain; overall survival data immature.",
                     drug="osimertinib", indication="nsclc", doc_date=date(2024, 1, 1))
    d = Decision(decision_id="TA1156", decision_date=date(2026, 5, 21),
                 drug="osimertinib", indication="nsclc")
    req = CapabilityRequest(capability="hta_archaeology", decision=d,
                            params={"mode": "backtest"})
    res = HTAArchaeology().run(req, reasoner=KeywordReasoner(), store=store)
    cats = {v.category.value for v in res.vulnerabilities}
    assert "icer_uncertainty" in cats          # grounded in the freshly-added context
