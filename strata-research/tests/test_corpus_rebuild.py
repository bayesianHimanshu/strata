"""Corpus rebuild: molecule scoping, label resolution, PubMed wiring, build gate."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from core.contracts import Decision, DocType
from experiments.build_retrieval_corpus import (
    CorpusHealthError,
    assert_corpus_healthy,
    fetch_literature,
    fetch_molecule_docs,
    pubmed_query,
)
from index.boundary import RetrievalBoundary
from index.store import InMemoryStore, structure_aware_prefixed_chunks
from sources.openfda import LabelDoc, parse_label_docs
from sources.pubmed import Abstract, SearchResult

LIT = "The comparator and overall survival endpoint were uncertain.\n"


def _decision(ta: str, drug: str) -> Decision:
    return Decision(
        agency="NICE", decision_id=ta, decision_date=date(2026, 6, 1),
        indication="cancer", outcome="not_recommended", drug=drug, appraisal_id=ta)


def _lit_chunks(sid: str, drug: str, *, ddate=date(2024, 1, 1)):
    return structure_aware_prefixed_chunks(
        LIT, source_id=sid, doc_date=ddate, doc_type=DocType.literature, drug=drug)


# --- §3 scoping: drug-A chunk not retrievable for a drug-B decision --------- #


def test_scoping_isolates_molecules() -> None:
    store = InMemoryStore()
    store.add(_lit_chunks("PMID:A", "osimertinib"))
    store.add(_lit_chunks("PMID:B", "vorasidenib"))

    b_decision = _decision("TA2", "Vorasidenib")
    boundary = RetrievalBoundary.for_decision(
        b_decision, buffer=timedelta(days=90), molecules={"vorasidenib"})

    admitted = {c.source_id for c in boundary.filter(store.chunks)}
    assert admitted == {"PMID:B"}  # the osimertinib chunk is out of scope
    hits = store.search("comparator overall survival", boundary=boundary, k=10)
    assert all(h.chunk.drug == "vorasidenib" for h in hits)


def test_scoping_off_when_no_molecules() -> None:
    # back-compat: empty molecules → scoping disabled, both admitted
    store = InMemoryStore()
    store.add(_lit_chunks("PMID:A", "osimertinib"))
    store.add(_lit_chunks("PMID:B", "vorasidenib"))
    boundary = RetrievalBoundary.for_decision(
        _decision("TA2", "x"), buffer=timedelta(days=90))
    assert len(boundary.filter(store.chunks)) == 2


# --- label resolution: right molecule or none, never a default ------------- #


def test_parse_label_no_match_returns_empty() -> None:
    assert parse_label_docs({"results": []}) == []


class _FDA:
    def __init__(self, labels): self._l = labels
    def fetch_label_docs(self, search, *, limit=1):
        return self._l, None


class _NoPub:
    def search(self, term, *, retmax=0, sort=None):
        return SearchResult(term=term, count=0, pmids=[]), None
    def fetch_abstracts(self, pmids):
        return [], None


class _NoCT:
    def search(self, condition="", *, intervention=None, status=None, page_size=20):
        return [], None, None


def test_label_tagged_to_queried_molecule() -> None:
    fda = _FDA([LabelDoc(brand="Bx", generic="osimertinib", text="Indicated for NSCLC",
                         effective_date=date(2020, 1, 1))])
    docs = fetch_molecule_docs(
        "osimertinib", "NSCLC", "TA1", ct=_NoCT(), pubmed=_NoPub(), fda=fda)
    labels = [d for d in docs if d.doc_type == DocType.label]
    assert len(labels) == 1 and labels[0].drug == "osimertinib"


def test_no_label_fallback_when_no_match() -> None:
    # the ORSERDU bug: an empty label result must yield NO label, not a default doc
    docs = fetch_molecule_docs(
        "osimertinib", "NSCLC", "TA1", ct=_NoCT(), pubmed=_NoPub(), fda=_FDA([]))
    assert [d for d in docs if d.doc_type == DocType.label] == []


# --- PubMed wiring: query construction + retmax is SET (the silent-zero bug) -- #


def test_pubmed_query_includes_molecule_indication_hta() -> None:
    q = pubmed_query("pembrolizumab", "Hodgkin lymphoma", with_indication=True)
    assert "pembrolizumab" in q and "Hodgkin lymphoma" in q
    assert "cost-effectiveness" in q and "overall survival" in q
    # molecule-only fallback drops the indication
    assert "Hodgkin" not in pubmed_query("pembrolizumab", "x", with_indication=False)


class _RecPub:
    def __init__(self): self.retmax = None
    def search(self, term, *, retmax=0, sort=None):
        self.retmax = retmax  # the fix: must NOT be 0, or no PMIDs come back
        return SearchResult(term=term, count=42, pmids=["1", "2"]), None
    def fetch_abstracts(self, pmids):
        return [Abstract(pmid=p, title="t", abstract="a", pub_date=date(2024, 1, 1))
                for p in pmids], None


def test_fetch_literature_sets_retmax_and_lands() -> None:
    pub = _RecPub()
    out = fetch_literature("pembrolizumab", "NSCLC", pubmed=pub, max_abstracts=20,
                           log=lambda *a, **k: None)
    assert pub.retmax == 20  # not the buggy default 0
    assert len(out) == 2


class _ScriptedPub:
    """Returns 0 for any query containing the HTA clause, hits on molecule-alone —
    reproducing talazoparib/epcoritamab (no HEOR papers, plenty of clinical lit)."""

    def __init__(self):
        self.terms: list[str] = []
        self.sorts: list = []

    def search(self, term, *, retmax=0, sort=None):
        self.terms.append(term)
        self.sorts.append(sort)
        zero = "cost-effectiveness" in term or " AND (" in term  # HTA or ind-AND clause
        pmids = [] if zero else ["7", "8", "9"]
        return SearchResult(term=term, count=len(pmids), pmids=pmids), None

    def fetch_abstracts(self, pmids):
        return [Abstract(pmid=p, title="OS endpoint", abstract="survival",
                         pub_date=date(2023, 1, 1)) for p in pmids], None


def test_literature_falls_back_to_molecule_alone() -> None:
    counts: list[tuple] = []
    pub = _ScriptedPub()
    out = fetch_literature(
        "talazoparib", "Breast cancer", pubmed=pub, max_abstracts=20,
        log=lambda mol, term, count, warn=False: counts.append((count, warn)))
    # HTA + indication attempts zeroed; molecule-alone landed, with recency sort
    assert len(out) == 3
    assert pub.terms[-1] == "(talazoparib)"  # the molecule-alone fallback
    assert pub.sorts[-1] == "pub_date"  # recency-capped
    assert (0, False) in counts  # per-attempt counts were logged (incl. the zeros)
    assert (0, True) not in counts  # it landed, so NO zero-literature warning


def test_literature_warns_when_all_attempts_zero() -> None:
    warned: list[bool] = []
    fetch_literature(
        "nonexistentol", "nowhere", pubmed=_NoPub(), max_abstracts=20,
        log=lambda mol, term, count, warn=False: warned.append(warn))
    assert warned[-1] is True  # final WARNING emitted on total zero


# --- §4 build gate fails loud on blob / wrong-drug / empty literature ------- #


# Molecule keys are lowercase (normalize_drug output); the chunk drug tag must match.
_TRIO = [("TA1", "osimertinib"), ("TA2", "vorasidenib"), ("TA3", "futibatinib")]


def _healthy_chunks():
    chunks = []
    for ta, drug in _TRIO:
        chunks += _lit_chunks(f"PMID:{ta}", drug, ddate=date(2020, 1, 1))
    return chunks


_DECISIONS = [_decision(ta, drug) for ta, drug in _TRIO]


def test_health_gate_passes_on_decision_specific_corpus() -> None:
    report = assert_corpus_healthy(
        _DECISIONS, _healthy_chunks(), buffer_days=90, min_distinct_ratio=0.4)
    assert report["distinct_drugs"] == 3
    assert report["distinct_eligible_pools"] == 3
    assert report["decisions_with_literature"] == 3
    assert report["retrieved_drug_match_rate"] == 1.0


def test_health_gate_raises_on_blob_no_drug() -> None:
    # every chunk drug=None → distinct_drugs 0 → blob
    blob = []
    for i in range(3):
        blob += structure_aware_prefixed_chunks(
            LIT, source_id=f"X{i}", doc_date=date(2020, 1, 1),
            doc_type=DocType.literature, drug=None)
    with pytest.raises(CorpusHealthError, match="blob"):
        assert_corpus_healthy(_DECISIONS, blob, buffer_days=90)


def test_health_gate_raises_on_zero_literature() -> None:
    # distinct drugs but no literature doc_type
    chunks = []
    for _ta, drug in _TRIO:
        chunks += structure_aware_prefixed_chunks(
            "Indications text.\n", source_id=f"label:{drug}", doc_date=date(2020, 1, 1),
            doc_type=DocType.label, drug=drug)
    with pytest.raises(CorpusHealthError, match="literature"):
        assert_corpus_healthy(_DECISIONS, chunks, buffer_days=90)
