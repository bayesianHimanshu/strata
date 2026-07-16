from __future__ import annotations

import argparse
import json
import random
import re
from collections import Counter
from pathlib import Path

# In the repo: TAXONOMY = [c.value for c in VulnCategory]; NEGATIVE_OUTCOMES from
# sources.nice. Kept as data here so the logic is testable without the package.
DEFAULT_NEGATIVE = {
    "not_recommended",
    "optimised",
    "non_submission",
    "only_in_research",
}

_BOX = re.compile(r"^- \[([ xX])\]\s+(\w+)", re.MULTILINE)
_HEAD = re.compile(r"^## (\S+)", re.MULTILINE)


# Calibration diagnostics


def calibration_diagnostics(
    decisions: list[dict],
    gold: dict[str, set[str]],
    taxonomy: list[str],
    negative: set[str] = DEFAULT_NEGATIVE,
) -> dict:
    """Per-category fire rate on restricted vs recommended decisions. A category
    that fires heavily on 'recommended' is matching boilerplate, not committee
    reasoning - that is the icer/budget_impact problem in measurable form."""
    restricted = [d for d in decisions if d["outcome"] in negative]
    recommended = [d for d in decisions if d["outcome"] == "recommended"]
    out: dict[str, dict] = {}
    for c in taxonomy:
        r_fire = sum(1 for d in restricted if c in gold.get(d["decision_id"], set()))
        a_fire = sum(1 for d in recommended if c in gold.get(d["decision_id"], set()))
        rr = (r_fire / len(restricted)) if restricted else None
        ar = (a_fire / len(recommended)) if recommended else None
        out[c] = {
            "fire_rate_restricted": rr,
            "fire_rate_recommended": ar,
            # boilerplate: fires on a majority of APPROVALS -> not a real problem cue
            "boilerplate_suspect": (ar is not None and ar >= 0.5),
        }
    return out


# Sampling (deterministic, stratified)


def sample_decisions(
    decisions: list[dict],
    *,
    cutoff_iso: str,
    negative: set[str] = DEFAULT_NEGATIVE,
    seed: int = 7,
    contrast: int = 8,
) -> list[dict]:
    """Audit ALL post-cutoff restricted decisions (the Arm A gold - and there are
    few), plus a contrast set of recommended decisions (to expose boilerplate),
    plus a few pre-cutoff for breadth. Deterministic."""
    rng = random.Random(seed)
    post = [d for d in decisions if d["decision_date"] > cutoff_iso]
    post_restricted = sorted(
        (d for d in post if d["outcome"] in negative), key=lambda d: d["decision_id"]
    )
    post_recommended = sorted(
        (d for d in post if d["outcome"] == "recommended"),
        key=lambda d: d["decision_id"],
    )
    pre = sorted(
        (d for d in decisions if d["decision_date"] <= cutoff_iso),
        key=lambda d: d["decision_id"],
    )

    chosen = list(post_restricted)  # all of them
    rng.shuffle(post_recommended)
    chosen += post_recommended[:contrast]  # boilerplate contrast
    rng.shuffle(pre)
    chosen += pre[: max(0, contrast // 2)]  # breadth
    seen, ordered = set(), []
    for d in chosen:
        if d["decision_id"] not in seen:
            seen.add(d["decision_id"])
            ordered.append(d)
    return ordered


# Sheet emit / parse


def build_sheet(
    sampled: list[dict],
    gold: dict[str, set[str]],
    taxonomy: list[str],
    diagnostics: dict,
    *,
    rationale_cap: int = 4000,
) -> str:
    lines: list[str] = [
        "# STRATA - gold audit sheet",
        "",
        "Mark `[x]` where the committee cited the category **as a problem** with the "
        "evidence. Leave `[ ]` otherwise. The `(miner: ...)` tag is the automatic "
        "extraction - your marks are the human vector we score it against.",
        "",
        "## Calibration diagnostics (read first)",
        "",
        "| category | fires on restricted | fires on recommended | boilerplate? |",
        "|---|---|---|---|",
    ]
    for c in taxonomy:
        d = diagnostics[c]
        _rr, _ar = d["fire_rate_restricted"], d["fire_rate_recommended"]
        rr = "-" if _rr is None else f"{_rr:.0%}"
        ar = "-" if _ar is None else f"{_ar:.0%}"
        flag = "⚠️ yes" if d["boilerplate_suspect"] else ""
        lines.append(f"| {c} | {rr} | {ar} | {flag} |")
    lines += [
        "",
        "A category firing on a majority of *recommended* decisions is "
        "matching boilerplate, not committee reasoning.",
        "",
        "---",
        "",
    ]

    for d in sampled:
        did = d["decision_id"]
        miner = gold.get(did, set())
        rat = (d.get("rationale_raw") or "").strip()
        if len(rat) > rationale_cap:
            rat = rat[:rationale_cap] + "\n…[truncated]"
        lines += [
            f"## {did}  -  {d.get('drug','?')} for {d.get('indication','?')}",
            f"- agency: {d.get('agency','?')} | date: {d.get('decision_date','?')} "
            f"| outcome: {d.get('outcome','?')}",
            "",
            "### Committee rationale",
            rat or "_(empty - flag this; should not have passed the fetcher)_",
            "",
            "### Categories",
        ]
        for c in taxonomy:
            box = "x" if c in miner else " "
            tag = "YES" if c in miner else "no"
            lines.append(f"- [{box}] {c}   (miner: {tag})")
        lines += ["", "### Notes", "_()_", "", "---", ""]
    return "\n".join(lines)


def parse_sheet(md: str, taxonomy: list[str]) -> dict[str, set[str]]:
    """Read a marked-up sheet back into {decision_id: set(checked categories)}."""
    allowed = set(taxonomy)
    human: dict[str, set[str]] = {}
    blocks = re.split(r"^## ", md, flags=re.MULTILINE)
    for blk in blocks:
        m = re.match(r"(\S+)", blk)
        if not m or "### Categories" not in blk:  # real decision blocks only
            continue
        did = m.group(1)
        checked = {
            tok
            for state, tok in _BOX.findall(blk)
            if state.lower() == "x" and tok in allowed
        }
        human[did] = checked
    return human


# Agreement


def cohen_kappa(a: list[int], b: list[int]) -> float | None:
    """Cohen's kappa for two equal-length label sequences (binary here)."""
    n = len(a)
    if n == 0:
        return None
    po = sum(1 for x, y in zip(a, b, strict=False) if x == y) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum((ca[k] / n) * (cb[k] / n) for k in set(ca) | set(cb))
    if pe == 1:
        return 1.0 if po == 1 else 0.0
    return (po - pe) / (1 - pe)


def agreement(
    decision_ids: list[str],
    miner: dict[str, set[str]],
    human: dict[str, set[str]],
    taxonomy: list[str],
) -> dict:
    """Per-category and pooled miner-vs-human kappa, plus a disagreement list."""
    per_cat: dict[str, float | None] = {}
    pooled_a, pooled_b = [], []
    for c in taxonomy:
        av = [1 if c in miner.get(d, set()) else 0 for d in decision_ids]
        bv = [1 if c in human.get(d, set()) else 0 for d in decision_ids]
        per_cat[c] = cohen_kappa(av, bv)
        pooled_a += av
        pooled_b += bv
    disagreements = []
    for d in decision_ids:
        m, h = miner.get(d, set()), human.get(d, set())
        if m != h:
            disagreements.append(
                {
                    "decision_id": d,
                    "miner_only": sorted(m - h),
                    "human_only": sorted(h - m),
                }
            )
    return {
        "n_decisions": len(decision_ids),
        "kappa_overall_pooled": cohen_kappa(pooled_a, pooled_b),
        "kappa_by_category": per_cat,
        "disagreements": disagreements,
    }


# Entrypoints


def _load(path: str) -> dict:
    return json.loads(Path(path).read_text())


def main() -> int:
    ap = argparse.ArgumentParser(description="STRATA gold audit / annotation harness")
    sub = ap.add_subparsers(dest="mode", required=True)

    e = sub.add_parser("emit", help="render the audit/annotation sheet")
    e.add_argument("--decisions", default="data/arm_a/decisions.json")
    e.add_argument("--gold", default="data/arm_a/candidate_gold.json")
    e.add_argument("--out", default="audit/sheet.md")
    e.add_argument("--seed", type=int, default=7)
    e.add_argument("--contrast", type=int, default=8)

    s = sub.add_parser("score", help="parse a filled sheet -> kappa + human gold")
    s.add_argument("--sheet", required=True)
    s.add_argument("--gold", default="data/arm_a/candidate_gold.json")
    s.add_argument("--out", default="audit/human_gold.json")

    args = ap.parse_args()

    # taxonomy + cutoff come from the package in the real run
    from core.config import MODEL_CUTOFF  # type: ignore
    from core.contracts import VulnCategory  # type: ignore

    taxonomy = [c.value for c in VulnCategory]

    if args.mode == "emit":
        decisions = _load(args.decisions)
        gold = {k: set(v) for k, v in _load(args.gold).items()}
        diag = calibration_diagnostics(decisions, gold, taxonomy)
        sampled = sample_decisions(
            decisions,
            cutoff_iso=MODEL_CUTOFF.isoformat(),
            seed=args.seed,
            contrast=args.contrast,
        )
        sheet = build_sheet(sampled, gold, taxonomy, diag)
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(sheet)
        print(f"wrote {args.out} - {len(sampled)} decisions to review")
        print(
            "boilerplate-suspect categories:",
            [c for c in taxonomy if diag[c]["boilerplate_suspect"]],
        )
        return 0

    # score
    miner = {k: set(v) for k, v in _load(args.gold).items()}
    human = parse_sheet(Path(args.sheet).read_text(), taxonomy)
    rep = agreement(sorted(human), miner, human, taxonomy)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(
        json.dumps({k: sorted(v) for k, v in human.items()}, indent=2)
    )
    print(
        json.dumps(
            {
                "kappa_overall": rep["kappa_overall_pooled"],
                "kappa_by_category": rep["kappa_by_category"],
                "n_disagreements": len(rep["disagreements"]),
            },
            indent=2,
        )
    )
    print(f"wrote human gold -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
