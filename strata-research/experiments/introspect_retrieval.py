"""STRATA — retrieval introspection (experiments/introspect_retrieval.py).

Open-book dropped recall 0.79 -> 0.58. Two explanations, opposite conclusions:
  (A) STARVATION — the retrievable corpus is too thin / mistargeted (note: literature
      was MISSING from the build), so the synthesizer had nothing to ground on and
      stayed silent. -> corpus problem, not a finding.
  (B) DISCIPLINE  — relevant evidence WAS available, but it doesn't contain the
      committee's specific reasoning, so a grounded model declines to assert what the
      parametric prior happily guessed. -> a real, sharp finding.

This runs offline against the files already produced and separates the two:
  - corpus composition (and flags missing literature),
  - per-decision boundary-ELIGIBLE pool by doc_type (date<decision−buffer AND not the
    decision's own dossier) — a starvation proxy that needs no embeddings/model,
  - the closed-right / open-missed categories per decision (the recall loss), beside
    the evidence that *was* eligible — so the transcript read is targeted.

    python -m experiments.introspect_retrieval
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

# --------------------------------------------------------------------------- #
# Tolerant field access (corpus schema may vary)
# --------------------------------------------------------------------------- #

def _f(d: dict, *names, default=None):
    for n in names:
        if n in d and d[n] is not None:
            return d[n]
    return default


def chunk_doctype(c): return _f(c, "doc_type", "type", default="?")
def chunk_appraisal(c): return _f(c, "appraisal_id", "ta_id", "ta")
def chunk_drug(c): return _f(c, "drug", "technology", "source_drug")
def chunk_text(c): return _f(c, "text", "content", "chunk_text", "chunk", default="")


def chunk_date(c) -> date | None:
    raw = _f(c, "doc_date", "date", "published_date")
    if raw is None:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def load_jsonl(path: str) -> list[dict]:
    out = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #

def composition(chunks: list[dict]) -> dict:
    by_type = Counter(chunk_doctype(c) for c in chunks)
    dates = [d for d in (chunk_date(c) for c in chunks) if d]
    appraisals = {a for a in (chunk_appraisal(c) for c in chunks) if a}
    drugs = {d for d in (chunk_drug(c) for c in chunks) if d}
    lit = sum(by_type.get(t, 0) for t in ("literature", "pubmed"))
    return {
        "total_chunks": len(chunks),
        "by_doc_type": dict(by_type),
        "literature_chunks": lit,
        "literature_missing": lit == 0,
        "distinct_appraisal_ids": len(appraisals),
        "distinct_drugs": len(drugs),
        "date_range": [min(dates).isoformat(), max(dates).isoformat()] if dates else None,
    }


def eligible_pool(decision: dict, chunks: list[dict], buffer_days: int,
                  *, match_drug: bool) -> list[dict]:
    """Chunks the boundary would admit for this decision: dated before
    (decision_date − buffer) and NOT the decision's own dossier. If chunks carry a
    drug field, optionally restrict to the decision's drug (relevance proxy)."""
    ddate = date.fromisoformat(str(decision["decision_date"])[:10])
    cutoff = ddate - timedelta(days=buffer_days)
    did = decision["decision_id"]
    ddrug = (decision.get("drug") or "").lower()
    out = []
    for c in chunks:
        cdate = chunk_date(c)
        if cdate is None or cdate >= cutoff:
            continue
        if chunk_appraisal(c) == did:                 # dossier-disjointness
            continue
        if match_drug and ddrug and chunk_drug(c) and chunk_drug(c).lower() != ddrug:
            continue
        out.append(c)
    return out


def run(decisions, gold, closed, open_, chunks, buffer_days, focus_snippets):
    comp = composition(chunks)
    has_drug = comp["distinct_drugs"] > 0

    rows = []
    recall_loss_by_cat: Counter = Counter()
    for d in decisions:
        did = d["decision_id"]
        g = set(gold.get(did, []))
        cp = set(closed.get(did, []))
        op = set(open_.get(did, []))
        closed_correct = g & cp
        open_correct = g & op
        missed_by_open = closed_correct - open_correct      # the recall loss
        for cat in missed_by_open:
            recall_loss_by_cat[cat] += 1

        elig = eligible_pool(d, chunks, buffer_days, match_drug=has_drug)
        elig_by_type = Counter(chunk_doctype(c) for c in elig)
        rows.append({
            "decision_id": did,
            "date": str(d["decision_date"])[:10],
            "drug": d.get("drug", ""),
            "eligible_total": len(elig),
            "eligible_by_type": dict(elig_by_type),
            "gold": sorted(g),
            "closed_pred_n": len(cp),
            "open_pred_n": len(op),
            "open_missed_vs_closed": sorted(missed_by_open),
            "_eligible_objs": elig,                          # internal, for snippets
        })

    # starvation verdict heuristic
    pools = [r["eligible_total"] for r in rows]
    median_pool = sorted(pools)[len(pools) // 2] if pools else 0
    starved = [r["decision_id"] for r in rows if r["eligible_total"] < 5]
    focus = [r for r in rows if r["open_missed_vs_closed"]]

    return comp, rows, {
        "median_eligible_pool": median_pool,
        "decisions_with_thin_pool(<5)": starved,
        "recall_loss_by_category": dict(recall_loss_by_cat),
        "n_focus_decisions(open_missed)": len(focus),
    }, focus, has_drug, focus_snippets


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description="retrieval introspection (offline)")
    ap.add_argument("--decisions", default="data/arm_a/decisions.json")
    ap.add_argument("--gold", default="audit/human_gold.json")
    ap.add_argument("--closed", default="data/arm_a/closed_book_gpt55.json")
    ap.add_argument("--open", default="data/arm_a/open_book_gpt55.json")
    ap.add_argument("--corpus", default="data/arm_a/corpus.jsonl")
    ap.add_argument("--buffer-days", type=int, default=90)
    ap.add_argument("--snippets", type=int, default=2,
                    help="evidence snippets per focus decision")
    args = ap.parse_args()

    raw = json.loads(Path(args.decisions).read_text())
    decisions = list(raw.values()) if isinstance(raw, dict) else raw
    gold = json.loads(Path(args.gold).read_text())
    closed = json.loads(Path(args.closed).read_text())["closed_book_predictions"]
    open_ = json.loads(Path(args.open).read_text())["open_predictions"]
    decisions = [d for d in decisions if d["decision_id"] in open_]   # post slice
    chunks = load_jsonl(args.corpus)

    comp, rows, summary, focus, has_drug, n_snip = run(
        decisions, gold, closed, open_, chunks, args.buffer_days, args.snippets)

    print("=" * 70)
    print("CORPUS COMPOSITION")
    print(json.dumps(comp, indent=2))
    if comp["literature_missing"]:
        print(">>> WARNING: zero literature chunks — the PubMed arm did not land. "
              "Open-book is grounding on trials/labels only.")
    if not has_drug:
        print(">>> NOTE: chunks carry no drug field — eligible pool is date+dossier "
              "only, not drug-relevance-filtered, so pools are upper bounds.")

    print("=" * 70)
    print("PER-DECISION ELIGIBLE POOL + RECALL LOSS")
    for r in rows:
        print(f"\n{r['decision_id']}  {r['date']}  {r['drug']}")
        print(f"  eligible: {r['eligible_total']}  {r['eligible_by_type']}")
        print(f"  gold={r['gold']}")
        print(f"  open missed (closed got these, open lost): "
              f"{r['open_missed_vs_closed'] or '—'}")

    print("=" * 70)
    print("SUMMARY")
    print(json.dumps(summary, indent=2))

    print("=" * 70)
    print("FOCUS — read these: open lost recall here. Was the evidence even available?")
    for r in focus:
        print(f"\n### {r['decision_id']}  ({r['drug']})  "
              f"lost: {r['open_missed_vs_closed']}")
        print(f"    eligible pool: {r['eligible_total']}  {r['eligible_by_type']}")
        # show a few eligible evidence snippets to judge starvation vs discipline
        ev = [c for c in r["_eligible_objs"]
              if chunk_doctype(c) in ("literature", "pubmed", "label")][:n_snip]
        if not ev:
            ev = r["_eligible_objs"][:n_snip]
        for c in ev:
            txt = " ".join(chunk_text(c).split())[:240]
            print(f"    [{chunk_doctype(c)} {chunk_date(c)}] {txt}…")
        if not r["_eligible_objs"]:
            print("    (no eligible evidence at all — STARVATION for this decision)")

    print("\n" + "=" * 70)
    print("READ:")
    print("- If focus decisions show thin/empty pools or no literature/label evidence")
    print("  bearing on the lost category -> STARVATION (fix the corpus, esp. PubMed).")
    print("- If relevant evidence WAS eligible but open-book still stayed silent ->")
    print("  DISCIPLINE: public data lacks the committee's reasoning; "
          "that's the finding.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())