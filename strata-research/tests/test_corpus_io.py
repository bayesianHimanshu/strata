"""Corpus converters, indexing, persistence, and assembly (offline)."""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from core.contracts import Decision, DocType
from experiments.build_retrieval_corpus import (
    abstract_to_doc,
    build_corpus,
    clean_query,
    dossier_to_doc,
    index_doc,
    label_to_doc,
    load_corpus,
    primary_generic,
    save_corpus,
    trial_to_doc,
)
from index.store import InMemoryStore, structure_aware_prefixed_chunks
from sources.clinicaltrials import TrialRecord
from sources.nice_guidance import GuidanceResult, ParsedGuidance
from sources.openfda import LabelDoc
from sources.pubmed import Abstract


def test_converters_tag_doc_type_and_appraisal() -> None:
    t = trial_to_doc(TrialRecord(
        nct_id="NCT1", title="A trial of drugx", conditions=["NSCLC"],
        completion_date=date(2024, 1, 1)))
    assert t.doc_type == DocType.trial_registry and t.appraisal_id is None
    assert t.doc_date == date(2024, 1, 1)

    a = abstract_to_doc(Abstract(
        pmid="9", title="OS immature", abstract="...", pub_date=date(2023, 5, 1)))
    assert a.doc_type == DocType.literature and a.doc_date == date(2023, 5, 1)

    label = label_to_doc(LabelDoc(
        brand="Brandx", generic="drugx", text="Indicated for NSCLC",
        effective_date=date(2022, 1, 1)), drug="drugx")
    assert label.doc_type == DocType.label and label.appraisal_id is None

    gr = GuidanceResult(ta_id="TA7", status="ok", parsed=ParsedGuidance(
        ta_id="TA7", title="Drug for cancer", published_date=date(2025, 1, 1),
        rationale_raw="The ICER was uncertain."))
    d = dossier_to_doc(gr)
    assert d is not None
    assert d.doc_type == DocType.ta_final_guidance and d.appraisal_id == "TA7"


def test_dossier_to_doc_skips_unavailable() -> None:
    assert dossier_to_doc(GuidanceResult(ta_id="TA9", status="unavailable")) is None


def test_clean_query_strips_parser_breaking_chars() -> None:
    # en-dash, parens, colon all 400 the CT.gov/openFDA parsers.
    assert clean_query("Trifluridine–tipiracil with bevacizumab") == (
        "Trifluridine-tipiracil with bevacizumab"
    )
    assert clean_query("Relapsed/refractory myeloma (after 1 line):") == (
        "Relapsed refractory myeloma after 1 line"
    )
    # a clean condition is untouched (the queries that already worked don't change)
    assert clean_query("Untreated advanced renal cell carcinoma") == (
        "Untreated advanced renal cell carcinoma"
    )
    assert clean_query("") == ""


def test_primary_generic_sanitizes_molecule() -> None:
    # primary_generic now just sanitizes a single molecule (normalize_drug already
    # split the regimen upstream): no parser-breaking chars reach openFDA.
    assert primary_generic("belantamab mafodotin") == "belantamab mafodotin"
    assert primary_generic("trifluridine") == "trifluridine"
    assert "–" not in primary_generic("Trifluridine–tipiracil")


def test_index_doc_is_idempotent_and_skips_undated(tmp_path: Path) -> None:
    store = InMemoryStore()
    seen: set[str] = set()
    doc = trial_to_doc(TrialRecord(
        nct_id="NCT2", title="comparator study", conditions=["NSCLC"],
        completion_date=date(2024, 1, 1)))
    n1 = index_doc(store, doc, snapshot_root=tmp_path, seen=seen)
    n2 = index_doc(store, doc, snapshot_root=tmp_path, seen=seen)  # same content
    assert n1 > 0 and n2 == 0  # second is a no-op (content-addressed)

    undated = trial_to_doc(TrialRecord(nct_id="NCT3", title="x"))  # no date
    assert index_doc(store, undated, snapshot_root=tmp_path, seen=seen) == 0


def test_corpus_jsonl_roundtrip(tmp_path: Path) -> None:
    chunks = structure_aware_prefixed_chunks(
        "Background:\nThe ICER was uncertain.\n",
        source_id="PMID:1", doc_date=date(2025, 1, 1),
        doc_type=DocType.literature, appraisal_id=None)
    path = tmp_path / "corpus.jsonl"
    save_corpus(chunks, path)
    store = load_corpus(path)
    assert len(store.chunks) == len(chunks)
    c = store.chunks[0]
    assert c.source_id == "PMID:1"
    assert c.doc_type == DocType.literature
    assert c.doc_date == date(2025, 1, 1)
    assert c.raw_text == chunks[0].raw_text


# --- build_corpus with fake clients (no network) ---------------------------- #


class _FakeCT:
    def __init__(self, trials): self._t = trials
    def search(self, condition="", *, intervention=None, status=None, page_size=20):
        return self._t, None, None


class _FakePub:
    def __init__(self, abstracts): self._a = abstracts
    def search(self, term, *, retmax=0, sort=None):
        from sources.pubmed import SearchResult
        return SearchResult(term=term, count=len(self._a),
                            pmids=[a.pmid for a in self._a]), None
    def fetch_abstracts(self, pmids):
        return self._a, None


class _FakeFDA:
    def __init__(self, labels): self._l = labels
    def fetch_label_docs(self, search, *, limit=1):
        return self._l, None


def _nice_cache(tmp_path: Path) -> Path:
    cache = tmp_path / "nice_guidance"
    cache.mkdir()
    gr = GuidanceResult(ta_id="TA999", status="ok", parsed=ParsedGuidance(
        ta_id="TA999", title="Other drug", published_date=date(2025, 1, 1),
        rationale_raw="The comparator was disputed and the ICER uncertain."))
    (cache / "TA999.json").write_text(gr.model_dump_json())
    return cache


def test_build_corpus_filters_dates_and_tags(tmp_path: Path) -> None:
    decision = Decision(
        agency="NICE", decision_id="TA200", decision_date=date(2026, 6, 1),
        indication="NSCLC", outcome="not_recommended", drug="drugx",
        appraisal_id="TA200")
    ct = _FakeCT([TrialRecord(nct_id="NCT9", title="drugx trial",
                              conditions=["NSCLC"], completion_date=date(2025, 1, 1))])
    pub = _FakePub([Abstract(pmid="5", title="late abstract", abstract="ICER",
                             pub_date=date(2026, 5, 1))])  # POST cutoff → filtered out
    fda = _FakeFDA([LabelDoc(brand="Bx", generic="drugx", text="Indicated ...",
                             effective_date=date(2024, 1, 1))])

    chunks = build_corpus(
        [decision], ct=ct, pubmed=pub, fda=fda,
        nice_cache_dir=_nice_cache(tmp_path), snapshot_root=tmp_path / "snap",
        buffer=timedelta(days=90))

    sources = {c.source_id for c in chunks}
    doctypes = {c.doc_type for c in chunks}
    assert "NCT9" in sources  # pre-cutoff trial included
    assert "TA999:guidance" in sources  # global NICE dossier included
    assert "PMID:5" not in sources  # post-cutoff abstract date-filtered out
    assert DocType.trial_registry in doctypes
    assert DocType.ta_final_guidance in doctypes
    # the trial chunk is molecule-tagged + decision-tagged (the whole point)
    trial = next(c for c in chunks if c.source_id == "NCT9")
    assert trial.drug == "drugx" and trial.decision_id == "TA200"
    # the dossier carries its appraisal_id so the boundary can exclude it for TA999
    dossier = next(c for c in chunks if c.source_id == "TA999:guidance")
    assert dossier.appraisal_id == "TA999"
