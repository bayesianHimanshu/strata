"""STRATA — open-book probe runner (experiments/run_open_book.py).

Counterpart to the closed-book run. Same decisions, same SME gold, same clean
slice — but predictions come from the synthesizer's RETRIEVAL-grounded mode under
the Phase 2 RetrievalBoundary (date cutoff + dossier-disjointness + sibling policy),
so every predicted category is backed by a retrieved chunk, not parametric prior.

The closed-book run already showed recall is saturated by priors (~0.79) while
precision is poor (the model predicts almost every category). So the question for
open-book is NOT "does recall go up" — it's **does retrieval raise PRECISION without
losing the recall the model already has**. This runner computes the per-category
open−closed delta on both, and the headline is the precision delta.

PREREQUISITE (must exist before a real run): a populated retrieval corpus — public
docs (ClinicalTrials.gov, PubMed, labels, FAERS) for each decision's drug/indication,
date-stamped and tagged doc_type/appraisal_id, indexed in the store; plus the NICE
dossier docs tagged appraisal_id=TA so the boundary excludes them for their own
decision. The synthesizer retrieves over that store. See the companion build spec.

    OPENAI_API_KEY=...  python -m experiments.run_open_book \
        --decisions data/arm_a/decisions.json --gold audit/human_gold.json \
        --closed data/arm_a/closed_book_gpt55.json
"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Protocol

from eval.closed_book_probe import ProbeDecision, _per_category_recall
from experiments.introspect_retrieval import eligible_pool, load_jsonl
from experiments.run_closed_book import GPT55_CUTOFF, build_inputs
from sources.drug_identity import normalize_drug

# --------------------------------------------------------------------------- #
# Synthesizer seam
# --------------------------------------------------------------------------- #

class OpenBookSynthesizer(Protocol):
    """Adapter over agents/synthesizer.py::EvidenceGapSynthesizer. The real impl
    builds a RetrievalBoundary for the decision (date = decision_date − buffer,
    appraisal_id excluded, sibling policy), runs the synthesizer's open-book mode,
    and returns the set of predicted VulnCategory values. One-line wrapper:
        vulns = synth.synthesize(decision, boundary)        # grounded Vulnerability[]
        return {v.category.value for v in vulns}
    """

    def predict_open_book(
        self, decision: ProbeDecision, *, buffer_days: int
    ) -> set[str]: ...


# --------------------------------------------------------------------------- #
# Scoring (pure)
# --------------------------------------------------------------------------- #

def _micro_precision(score: dict) -> float | None:
    hits = sum(v["hits"] for v in score["by_category"].values())
    pred = sum(v["predicted"] for v in score["by_category"].values())
    return (hits / pred) if pred else None


def _delta(a, b):
    return None if (a is None or b is None) else round(a - b, 4)


def evidence_bearing_ids(decisions, chunks, buffer_days, min_eligible=1):
    """Decision ids whose boundary-eligible pool holds ≥ min_eligible chunks of the
    decision's OWN molecule. The honest sub-slice: where retrieval actually had
    evidence to ground on (reuses introspect.eligible_pool + the one normalizer)."""
    out = []
    for d in decisions:
        mols = {m.lower() for m in normalize_drug(d.get("drug", "")).molecules}
        elig = [
            c
            for c in eligible_pool(d, chunks, buffer_days, match_drug=False)
            if (cd := (c.get("drug") or "").lower())
            and any(m == cd or m in cd or cd in m for m in mols)
        ]
        if len(elig) >= min_eligible:
            out.append(d["decision_id"])
    return out


def _micro(ids, gold, open_pred, closed_pred, taxonomy):
    """The micro delta block for a set of decision ids (no model calls)."""
    open_s = _per_category_recall(ids, gold, open_pred, taxonomy)
    closed_s = _per_category_recall(ids, gold, closed_pred, taxonomy)
    op, cp = _micro_precision(open_s), _micro_precision(closed_s)
    orr, crr = open_s["micro_recall"], closed_s["micro_recall"]
    block = {
        "n_decisions": len(ids),
        "recall_open": orr, "recall_closed": crr, "delta_recall": _delta(orr, crr),
        "precision_open": op, "precision_closed": cp, "delta_precision": _delta(op, cp),
    }
    return block, open_s, closed_s


def _headline(micro: dict) -> str:
    d_prec, d_rec = micro["delta_precision"], micro["delta_recall"]
    if d_prec is not None and d_prec > 0 and (d_rec is None or d_rec >= -0.05):
        return ("retrieval RAISES precision without losing recall — the retrieval "
                "system earns its place over parametric priors")
    if d_prec is not None and d_prec <= 0:
        return ("retrieval does NOT improve precision over parametric priors — "
                "open-book adds little; the prior already saturates the signal")
    return "inconclusive — check per-category deltas and slice size"


def run_open_book(
    decisions: list[ProbeDecision],
    gold: dict[str, set[str]],
    synth: OpenBookSynthesizer,
    closed_predictions: dict[str, set[str]],
    taxonomy: list[str],
    cutoff: date,
    *,
    buffer_days: int = 90,
    chunks: list[dict] | None = None,
    min_eligible: int = 1,
) -> dict:
    post = [d for d in decisions if d.decision_date > cutoff]
    post_ids = [d.decision_id for d in post]

    # Predict ONCE; both slices score these same predictions (no extra model calls).
    open_pred = {d.decision_id: synth.predict_open_book(d, buffer_days=buffer_days)
                 for d in post}

    micro, open_s, closed_s = _micro(
        post_ids, gold, open_pred, closed_predictions, taxonomy)
    by_cat: dict[str, dict] = {}
    for c in taxonomy:
        o, cl = open_s["by_category"][c], closed_s["by_category"][c]
        by_cat[c] = {
            "gold_positives": o["gold_positives"],
            "recall_open": o["recall"], "recall_closed": cl["recall"],
            "precision_open": o["precision"], "precision_closed": cl["precision"],
            "delta_recall": _delta(o["recall"], cl["recall"]),
            "delta_precision": _delta(o["precision"], cl["precision"]),
        }

    report = {
        "slice": "post_cutoff (clean)",
        "cutoff": cutoff.isoformat(),
        "buffer_days": buffer_days,
        "n_decisions": len(post_ids),
        "micro": micro,
        "by_category": by_cat,
        "headline": _headline(micro),
        "open_predictions": {k: sorted(v) for k, v in open_pred.items()},
    }

    # Evidence-bearing sub-slice: same predictions, restricted to decisions where the
    # boundary actually had own-molecule evidence. The honest interim headline.
    if chunks is not None:
        post_dicts = [
            {"decision_id": d.decision_id,
             "decision_date": d.decision_date.isoformat(),
             "drug": d.drug}
            for d in post
        ]
        eb = set(evidence_bearing_ids(post_dicts, chunks, buffer_days, min_eligible))
        eb_ids = [i for i in post_ids if i in eb]
        eb_micro, _, _ = _micro(eb_ids, gold, open_pred, closed_predictions, taxonomy)
        report["micro_evidence_bearing"] = eb_micro
        report["evidence_bearing_ids"] = eb_ids
        report["min_eligible"] = min_eligible
        report["headline_evidence_bearing"] = _headline(eb_micro)

    return report


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #

def _load_closed(path: str) -> dict[str, set[str]]:
    data = json.loads(Path(path).read_text())
    return {k: set(v) for k, v in data["closed_book_predictions"].items()}


def main() -> int:
    ap = argparse.ArgumentParser(description="open-book probe (retrieval-grounded)")
    ap.add_argument("--decisions", default="data/arm_a/decisions.json")
    ap.add_argument("--gold", default="audit/human_gold.json")
    ap.add_argument("--closed", default="data/arm_a/closed_book_gpt55.json")
    ap.add_argument("--buffer-days", type=int, default=90)
    ap.add_argument("--corpus", default="data/arm_a/corpus.jsonl")
    ap.add_argument("--min-eligible", type=int, default=1)
    ap.add_argument("--out", default="data/arm_a/open_book_gpt55.json")
    args = ap.parse_args()

    from core.contracts import VulnCategory  # type: ignore
    from core.provenance import snapshot  # type: ignore
    from eval.rubric import assert_rubric_committed, rubric_hash  # type: ignore
    taxonomy = [c.value for c in VulnCategory]

    assert_rubric_committed()                                 # gate every run

    decisions, gold = build_inputs(args.decisions, args.gold)
    closed = _load_closed(args.closed)

    # build the real synthesizer adapter over a POPULATED store (see build spec)
    from experiments.open_book_synth import make_synthesizer  # type: ignore
    synth = make_synthesizer()

    chunks = load_jsonl(args.corpus) if Path(args.corpus).exists() else None
    report = run_open_book(decisions, gold, synth, closed, taxonomy, GPT55_CUTOFF,
                           buffer_days=args.buffer_days, chunks=chunks,
                           min_eligible=args.min_eligible)
    report["rubric_hash"] = rubric_hash()
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2, default=str))
    snapshot(json.dumps(report, default=str).encode(),
             source="probe", source_id="open_book_gpt55", url="local://probe")

    def _show(label: str, m: dict) -> None:
        print(f"\n[{label}]  n={m['n_decisions']}")
        print(f"  recall    open={m['recall_open']} closed={m['recall_closed']} "
              f"Δ={m['delta_recall']}")
        print(f"  precision open={m['precision_open']} closed={m['precision_closed']} "
              f"Δ={m['delta_precision']}")

    print(f"buffer={args.buffer_days}d")
    _show("full post slice", report["micro"])
    if "micro_evidence_bearing" in report:
        _show("evidence-bearing (headline)", report["micro_evidence_bearing"])
        print("→", report["headline_evidence_bearing"])
    else:
        print("→", report["headline"])
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())