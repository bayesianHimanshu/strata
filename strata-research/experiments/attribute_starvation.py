from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

# reuse the tolerant accessors from the introspection script
from experiments.introspect_retrieval import (
    chunk_appraisal,
    chunk_date,
    chunk_doctype,
    chunk_drug,
    load_jsonl,
)

BUFFER_SWEEP = [0, 14, 30, 60, 90, 180]



# Drug matching (the artifact fix)



def _fallback_molecules(technology: str) -> set[str]:
    """Used only if sources.drug_identity.normalize_drug isn't importable. Splits a
    combination string into molecule tokens."""
    t = technology.lower()
    for sep in (" with ", " plus ", " and ", ","):
        t = t.replace(sep, "|")
    return {tok.strip() for tok in t.split("|") if tok.strip()}


def molecules_for(technology: str, normalize) -> set[str]:
    if normalize is not None:
        try:
            return {m.lower() for m in normalize(technology).molecules}
        except Exception:  # noqa: BLE001
            pass
    return _fallback_molecules(technology)


def matches(chunk, molecules: set[str]) -> bool:
    cd = (chunk_drug(chunk) or "").lower()
    if not cd:
        return False
    return any(m == cd or m in cd or cd in m for m in molecules)


def raw_matches(chunk, technology: str) -> bool:
    return (chunk_drug(chunk) or "").lower() == technology.lower()



# Attribution



def attribute(decision, chunks, normalize, *, current_buffer=90, thin=5):
    did = decision["decision_id"]
    ddate = date.fromisoformat(str(decision["decision_date"])[:10])
    mols = molecules_for(decision.get("drug", ""), normalize)

    gathered = [c for c in chunks if chunk_appraisal(c) != did and matches(c, mols)]
    raw_gathered = [
        c
        for c in chunks
        if chunk_appraisal(c) != did and raw_matches(c, decision.get("drug", ""))
    ]

    def eligible(buf):
        cut = ddate - timedelta(days=buf)
        return [c for c in gathered if (cd := chunk_date(c)) and cd < cut]

    sweep = {b: len(eligible(b)) for b in BUFFER_SWEEP}
    g_by_type = Counter(chunk_doctype(c) for c in gathered)
    elig_now = sweep[current_buffer]
    relaxed = max(sweep[0], sweep[14])  # what a near-zero buffer would admit

    # attribution (ratio-based: small pools make absolute thresholds useless)
    if len(gathered) == 0:
        cause = "no_evidence_for_molecule"  # query too narrow OR nothing public
    elif elig_now >= thin:
        cause = "healthy"
    elif (relaxed - elig_now) >= max(1, 0.5 * len(gathered)):
        cause = "buffer_too_aggressive"  # relaxing recovers most of the pool
    else:
        cause = "genuinely_sparse"  # gathered, but even relaxed yields little

    artifact = len(gathered) > 0 and len(raw_gathered) == 0  # normalized recovered it

    return {
        "decision_id": did,
        "drug": decision.get("drug", ""),
        "molecules": sorted(mols),
        "gathered_total": len(gathered),
        "gathered_by_type": dict(g_by_type),
        "eligible_by_buffer": sweep,
        "eligible_current": elig_now,
        "cause": cause,
        "matching_artifact_in_old_introspect": artifact,
    }


def run(decisions, chunks, normalize, current_buffer=90):
    rows = [
        attribute(d, chunks, normalize, current_buffer=current_buffer)
        for d in decisions
    ]
    causes = Counter(r["cause"] for r in rows)
    artifacts = [
        r["decision_id"] for r in rows if r["matching_artifact_in_old_introspect"]
    ]
    # aggregate buffer sweep: total eligible across decisions at each buffer
    agg_sweep = {b: sum(r["eligible_by_buffer"][b] for r in rows) for b in BUFFER_SWEEP}
    return rows, {
        "n_decisions": len(rows),
        "causes": dict(causes),
        "matching_artifacts(combos recovered by normalization)": artifacts,
        "aggregate_eligible_by_buffer": agg_sweep,
    }



# Entrypoint



def main() -> int:
    ap = argparse.ArgumentParser(
        description="attribute starvation: artifact vs query vs buffer"
    )
    ap.add_argument("--decisions", default="data/arm_a/decisions.json")
    ap.add_argument("--open", default="data/arm_a/open_book_gpt55.json")
    ap.add_argument("--corpus", default="data/arm_a/corpus.jsonl")
    ap.add_argument("--buffer-days", type=int, default=90)
    args = ap.parse_args()

    raw = json.loads(Path(args.decisions).read_text())
    decisions = list(raw.values()) if isinstance(raw, dict) else raw
    open_ = json.loads(Path(args.open).read_text())["open_predictions"]
    decisions = [d for d in decisions if d["decision_id"] in open_]  # post slice
    chunks = load_jsonl(args.corpus)

    try:
        from sources.drug_identity import normalize_drug  # type: ignore

        normalize = normalize_drug
    except Exception:  # noqa: BLE001
        normalize = None
        print(
            ">>> NOTE: sources.drug_identity.normalize_drug not importable - using "
            "fallback molecule splitter (less accurate)."
        )

    rows, summary = run(decisions, chunks, normalize, current_buffer=args.buffer_days)

    print("=" * 70)
    print("PER-DECISION ATTRIBUTION")
    for r in rows:
        flag = (
            "  [combo recovered by normalization]"
            if r["matching_artifact_in_old_introspect"]
            else ""
        )
        print(f"\n{r['decision_id']}  {r['drug'][:48]}{flag}")
        print(f"  molecules: {r['molecules']}")
        print(f"  gathered: {r['gathered_total']}  {r['gathered_by_type']}")
        print(f"  eligible by buffer(days): {r['eligible_by_buffer']}")
        print(f"  -> CAUSE: {r['cause']}")

    print("=" * 70)
    print("SUMMARY")
    print(json.dumps(summary, indent=2))
    print("=" * 70)
    print("READ:")
    print("- matching_artifacts: those combos were NOT starved - the old introspect")
    print("  under-counted them. Real eligibility is the 'gathered/eligible' here.")
    print("- aggregate_eligible_by_buffer: if it jumps sharply from 90 -> 30/14, the")
    print("  buffer is the dominant cost and relaxing it recovers most evidence.")
    print("- causes=query_too_narrow concentrated in trial_registry -> widen the trial")
    print(
        "  query. causes=genuinely_sparse -> public data really lacks it (a finding)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
