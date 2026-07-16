"""Ingestion: pure converters, query hygiene, sibling map, build orchestration, and the
fail-loud corpus health gate (no network)."""
from __future__ import annotations

from datetime import date

from strata_platform.ingest.corpus import (
    abstract_to_doc,
    build_corpus,
    clean_query,
    compute_sibling_map,
    doc_to_chunks,
    label_to_doc,
    primary_generic,
    pubmed_query,
    trial_to_doc,
)
from strata_platform.ingest.health import CorpusHealthError, assert_corpus_healthy
from strata_platform.sources.openfda import LabelDoc
from strata_platform.sources.pubmed import Abstract, SearchResult
from strata_platform.sources.clinicaltrials import TrialRecord
from strata_platform.substrate.contracts import Chunk, Decision, DocType


# --- query hygiene ---------------------------------------------------------- #

def test_clean_query_strips_specials_and_dashes() -> None:
    assert clean_query("trifluridine-tipiracil (combo): 90mg") == "trifluridine-tipiracil combo 90mg"
    assert primary_generic("nab-paclitaxel") == "nab paclitaxel"


def test_pubmed_query_soft_boost_and_indication() -> None:
    q = pubmed_query("osimertinib", "NSCLC", with_indication=True)
    assert "osimertinib" in q and "NSCLC" in q and "cost-effectiveness" in q
    q2 = pubmed_query("osimertinib", "", with_indication=True)
    assert "NSCLC" not in q2


# --- converters ------------------------------------------------------------- #

def test_converters_tag_drug_doctype_and_chunk() -> None:
    t = TrialRecord(nct_id="NCT1", title="Osi trial", conditions=["NSCLC"],
                    completion_date=date(2024, 1, 1))
    doc = trial_to_doc(t, drug="osimertinib", indication="NSCLC", decision_id="TA1")
    assert doc.doc_type == DocType.trial_registry and doc.drug == "osimertinib"
    chunks = doc_to_chunks(doc)
    assert chunks and all(c.drug == "osimertinib" for c in chunks)
    assert all(c.doc_type == DocType.trial_registry for c in chunks)

    a = abstract_to_doc(Abstract(pmid="9", title="OS immature", abstract="comparator",
                                 pub_date=date(2024, 2, 1)), drug="osimertinib")
    assert abstract_to_doc.__name__  # imported
    assert a.doc_type == DocType.literature

    lab = label_to_doc(LabelDoc(brand="Tagrisso", generic="osimertinib",
                                text="indications", effective_date=date(2023, 1, 1)),
                       drug="osimertinib")
    assert lab.doc_type == DocType.label


def test_doc_to_chunks_skips_undated() -> None:
    a = abstract_to_doc(Abstract(pmid="9", title="t", abstract="x", pub_date=None),
                        drug="d")
    assert doc_to_chunks(a) == []


# --- sibling map ------------------------------------------------------------ #

def test_compute_sibling_map_groups_by_primary_molecule() -> None:
    decisions = [
        Decision(decision_id="TA1", decision_date=date(2026, 1, 1),
                 drug="Belantamab mafodotin with pomalidomide", indication="mm"),
        Decision(decision_id="TA2", decision_date=date(2026, 2, 1),
                 drug="Belantamab mafodotin with bortezomib", indication="mm"),
        Decision(decision_id="TA3", decision_date=date(2026, 3, 1),
                 drug="osimertinib", indication="nsclc"),
    ]
    sib = compute_sibling_map(decisions)
    assert sib["TA1"] == frozenset({"TA2"})
    assert sib["TA2"] == frozenset({"TA1"})
    assert sib["TA3"] == frozenset()


# --- build orchestration (fake clients, no network) ------------------------- #

class _FakeCT:
    def search(self, condition="", *, intervention=None, status=None,
               page_size=50, page_token=None):
        t = TrialRecord(nct_id=f"NCT-{intervention}", title=f"{intervention} trial",
                        conditions=[condition], completion_date=date(2024, 1, 1))
        return [t], None, None


class _FakePubMed:
    def search(self, term, *, retmax=0, sort=None, mindate=None, maxdate=None):
        return SearchResult(term=term, count=1, pmids=["111"]), None

    def fetch_abstracts(self, pmids):
        # within the leakage buffer -> must be dropped by build_corpus
        return [Abstract(pmid="111", title="late", abstract="x",
                         pub_date=date(2026, 4, 15))], None


class _FakeFDA:
    def fetch_label_docs(self, search, *, limit=1):
        return [LabelDoc(brand="B", generic="g", text="indications and warnings",
                         effective_date=date(2023, 1, 1))], None


def test_build_corpus_scopes_and_leakage_filters() -> None:
    d = Decision(decision_id="TA9", decision_date=date(2026, 5, 1), drug="osimertinib",
                 indication="NSCLC")
    chunks = build_corpus([d], ct=_FakeCT(), pubmed=_FakePubMed(), fda=_FakeFDA(),
                          buffer_days=90)
    kinds = {c.doc_type for c in chunks}
    assert DocType.trial_registry in kinds and DocType.label in kinds
    # the abstract dated 2026-04-15 is within the 90d buffer of a 2026-05-01 decision
    assert DocType.literature not in kinds
    assert all(c.drug == "osimertinib" for c in chunks)


# --- fail-loud health gate -------------------------------------------------- #

def _lit(drug: str, did: str = "") -> Chunk:
    return Chunk(text=f"{drug} overall survival immature comparator ICER",
                 doc_type=DocType.literature, drug=drug, doc_date=date(2024, 1, 1),
                 source_id=f"PMID:{drug}", appraisal_id=None)


def _decisions() -> list[Decision]:
    return [
        Decision(decision_id="TA1", decision_date=date(2026, 5, 1), drug="osimertinib",
                 indication="nsclc"),
        Decision(decision_id="TA2", decision_date=date(2026, 5, 1), drug="vorasidenib",
                 indication="glioma"),
        Decision(decision_id="TA3", decision_date=date(2026, 5, 1), drug="ripretinib",
                 indication="gist"),
    ]


def test_health_gate_passes_on_clean_corpus() -> None:
    chunks = [_lit("osimertinib"), _lit("vorasidenib"), _lit("ripretinib")]
    report = assert_corpus_healthy(_decisions(), chunks, min_literature_ratio=0.5)
    assert report["distinct_drugs"] == 3
    assert report["retrieved_drug_match_rate"] == 1.0


def test_health_gate_raises_on_blob() -> None:
    chunks = [_lit("osimertinib"), _lit("osimertinib"), _lit("osimertinib")]
    try:
        assert_corpus_healthy(_decisions(), chunks)
        raise AssertionError("expected CorpusHealthError")
    except CorpusHealthError as e:
        assert "blob" in str(e)


def test_health_gate_raises_on_missing_literature() -> None:
    chunks = [
        Chunk(text="label text", doc_type=DocType.label, drug=d,
              doc_date=date(2024, 1, 1), source_id=f"label:{d}")
        for d in ("osimertinib", "vorasidenib", "ripretinib")
    ]
    try:
        assert_corpus_healthy(_decisions(), chunks)
        raise AssertionError("expected CorpusHealthError")
    except CorpusHealthError as e:
        assert "literature" in str(e)


def test_health_gate_raises_on_wrong_drug_routing() -> None:
    # Clean base + a pile of UNTAGGED literature (drug=None) admitted for every decision:
    # retrieval can't route to the molecule -> drug-match collapses below threshold.
    chunks = [_lit("osimertinib"), _lit("vorasidenib"), _lit("ripretinib")]
    for i in range(8):
        chunks.append(Chunk(text="overall survival comparator ICER untagged",
                            doc_type=DocType.literature, drug=None,
                            doc_date=date(2024, 1, 1), source_id=f"untagged:{i}"))
    try:
        assert_corpus_healthy(_decisions(), chunks)
        raise AssertionError("expected CorpusHealthError")
    except CorpusHealthError as e:
        assert "wrong-drug" in str(e)
