"""Demo support: a handful of real sample decisions and a corpus-seed endpoint so the
open-book vs closed-book contrast is live for a showcase. In production the seed is
replaced by live, leakage-controlled ingestion from public sources (CLAUDE.md steps
1–2); the rest of the path is identical.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter

from strata_platform.substrate.contracts import Chunk, Decision, DocType
from strata_platform.substrate.store import get_store

router = APIRouter()

# Real recent NICE oncology decisions from the STRATA study (drug = primary molecule).
SAMPLE_DECISIONS: list[Decision] = [
    Decision(decision_id="TA1156", decision_date=date(2026, 5, 21), drug="osimertinib",
             indication="EGFR-mutated non-small-cell lung cancer", outcome="optimised"),
    Decision(decision_id="TA1147", decision_date=date(2026, 4, 29), drug="vorasidenib",
             indication="IDH-mutant grade 2 glioma", outcome="optimised"),
    Decision(decision_id="TA1133", decision_date=date(2026, 2, 18),
             drug="belantamab mafodotin",
             indication="relapsed/refractory multiple myeloma", outcome="optimised"),
    Decision(decision_id="TA1146", decision_date=date(2026, 4, 28), drug="ripretinib",
             indication="advanced gastrointestinal stromal tumour", outcome="optimised"),
]

# Small, drug-scoped, pre-cutoff evidence so open-book / synthesis has something real to
# ground on. DEMO STAND-IN ONLY — replaced by live, leakage-controlled ingestion
# (BUILD_BRIEF §1-2 / the real-time context subsystem). Each drug spans the evidence
# dimensions (efficacy, comparator, safety, economic, generalizability); snippets are kept
# deliberately generic/illustrative — they do NOT carry invented clinical numbers that
# would read as real citations. Dates sit safely before each decision's leakage cutoff.
_SEED: dict[str, list[tuple[DocType, str, str]]] = {
    "osimertinib": [
        (DocType.literature, "2024-03-01",
         "Efficacy: in EGFR-mutated NSCLC, osimertinib improved progression-free "
         "survival versus comparator therapy; overall survival data remained immature at "
         "the reported analysis, so the surrogate endpoint carried the benefit case."),
        (DocType.literature, "2024-05-01",
         "Comparator: the relevant comparison was platinum-doublet chemotherapy; whether "
         "the trial comparator reflected current UK standard of care was debated."),
        (DocType.label, "2023-06-01",
         "Safety: the osimertinib label notes warnings for interstitial lung disease and "
         "QT-interval prolongation among adverse reactions for EGFR-mutated NSCLC."),
        (DocType.literature, "2024-09-01",
         "Economic: cost-effectiveness modelling showed the ICER was highly uncertain and "
         "sensitive to extrapolation of the survival curve and the chosen comparator."),
        (DocType.literature, "2024-07-01",
         "Generalizability: the trial population's applicability to NHS clinical practice "
         "was questioned given differences in prior treatment and disease stage."),
    ],
    "vorasidenib": [
        (DocType.literature, "2025-06-01",
         "Efficacy: vorasidenib in IDH-mutant grade 2 glioma demonstrated a "
         "progression-free survival benefit; overall survival data were immature, a "
         "surrogate-endpoint concern for the committee."),
        (DocType.literature, "2025-07-01",
         "Comparator: active monitoring / watchful waiting was the relevant comparison in "
         "this indolent disease, complicating the comparator choice."),
        (DocType.label, "2024-09-01",
         "Safety: hepatotoxicity (transaminase elevations) features in the vorasidenib "
         "adverse-event profile and warnings."),
        (DocType.literature, "2025-08-01",
         "Economic: health-economic analysis flagged substantial ICER uncertainty given "
         "the indolent course and limited long-term follow-up."),
        (DocType.literature, "2025-05-01",
         "Generalizability: the trial enrolled a younger, fitter population than typical "
         "NHS practice, raising external-validity questions."),
    ],
    "belantamab mafodotin": [
        (DocType.literature, "2024-05-01",
         "Efficacy: belantamab mafodotin combinations in relapsed/refractory myeloma "
         "improved progression-free survival; overall survival follow-up was short."),
        (DocType.literature, "2024-06-01",
         "Comparator / trial design: open-label assessment and the chosen comparator "
         "regimen drew committee concern about risk of bias."),
        (DocType.label, "2023-01-01",
         "Safety: belantamab mafodotin is a BCMA-targeting antibody-drug conjugate with "
         "characteristic ocular (keratopathy / visual acuity) adverse events."),
        (DocType.literature, "2024-08-01",
         "Economic: the incremental cost-effectiveness ratio was highly uncertain and "
         "above the conventional threshold range in the base case."),
        (DocType.literature, "2024-04-01",
         "Generalizability: applicability of the trial population to UK clinical practice "
         "was uncertain given heavily pre-treated patients."),
    ],
    "ripretinib": [
        (DocType.literature, "2025-09-01",
         "Efficacy: ripretinib in advanced gastrointestinal stromal tumour extended "
         "progression-free survival in later lines of therapy."),
        (DocType.literature, "2025-10-01",
         "Comparator: outcomes versus sunitinib were considered; the relevant comparison "
         "for the positioning was contested."),
        (DocType.label, "2023-03-01",
         "Safety: ripretinib warnings include palmar-plantar erythrodysaesthesia, "
         "hypertension, and cardiac dysfunction."),
        (DocType.literature, "2025-12-01",
         "Economic: the committee's concern centred on ICER uncertainty under different "
         "comparator and survival-extrapolation assumptions."),
        (DocType.literature, "2025-08-01",
         "Generalizability: the trial population's representativeness of NHS GIST patients "
         "(line of therapy, mutation status) was debated."),
    ],
}


@router.get("/decisions/samples")
def sample_decisions() -> dict:
    return {"decisions": [d.model_dump(mode="json") for d in SAMPLE_DECISIONS]}


@router.post("/admin/seed")
def seed_corpus() -> dict:
    """Load the sample evidence into the retrieval store (idempotent for a demo)."""
    store = get_store()
    chunks: list[Chunk] = []
    for drug, items in _SEED.items():
        for doc_type, d, text in items:
            chunks.append(Chunk(text=text, doc_type=doc_type, drug=drug,
                                doc_date=date.fromisoformat(d),
                                source_id=f"{drug}:{d}:{doc_type.value}"))
    store.add(chunks)
    return {"seeded_chunks": len(chunks),
            "drugs": sorted(_SEED.keys()),
            "note": "demo seed; production replaces this with live public-data ingestion"}
