from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

from agents.decision_miner import DecisionMiner
from core.config import MODEL_CUTOFF, USER_AGENT
from core.contracts import Decision, VulnCategory
from sources.nice import NEGATIVE_OUTCOMES
from sources.nice_guidance import NICEGuidanceClient
from sources.nice_index import recent_cancer_tas

DEFAULT_OUT = Path("data/arm_a")

CandidateGold = dict[str, set[VulnCategory]]


def _load_index_bytes(xbytes_or_url: bytes | str) -> bytes:
    if isinstance(xbytes_or_url, bytes):
        return xbytes_or_url
    if xbytes_or_url.startswith("http"):
        r = httpx.get(
            xbytes_or_url,
            follow_redirects=True,
            timeout=30.0,
            headers={"User-Agent": USER_AGENT},
        )
        r.raise_for_status()
        return r.content
    return Path(xbytes_or_url).read_bytes()


def build_arm_a_dataset(
    xbytes_or_url: bytes | str,
    cutoff: date = MODEL_CUTOFF,
    *,
    guidance_client: NICEGuidanceClient | None = None,
    miner: DecisionMiner | None = None,
    out_dir: Path | None = DEFAULT_OUT,
    model_cutoff: date = MODEL_CUTOFF,
) -> tuple[list[Decision], CandidateGold]:
    """Assemble (decisions, candidate_gold) and, unless out_dir is None, persist the
    dataset + manifest. `cutoff` bounds the recent slice; `model_cutoff` stratifies."""
    xbytes = _load_index_bytes(xbytes_or_url)
    refs = recent_cancer_tas(xbytes, cutoff)
    client = guidance_client or NICEGuidanceClient()
    miner = miner or DecisionMiner()

    decisions: list[Decision] = []
    candidate_gold: CandidateGold = {}
    unavailable: list[str] = []
    parse_failed: list[str] = []

    for ref in refs:
        # The fetcher is fail-loud on a reachable-but-unparseable page (missing date /
        # empty rationale). The BATCH records that loudly and moves on, so one odd page
        # never aborts a 100-TA run - the failure surfaces in the manifest, not silently.
        try:
            result = client.fetch(ref.ta_id)
        except ValueError as exc:
            parse_failed.append(ref.ta_id)
            print(f"[build_arm_a] parse failed for {ref.ta_id}: {exc}", file=sys.stderr)
            continue
        if result.status != "ok" or result.parsed is None:
            unavailable.append(ref.ta_id)
            continue
        parsed = result.parsed
        decision = Decision(
            agency="NICE",
            decision_id=ref.ta_id,
            decision_date=parsed.published_date,  # EXACT date overrides index FY
            indication=ref.indication,
            drug=ref.technology,
            outcome=ref.categorisation,
            rationale_raw=parsed.rationale_raw,
            appraisal_id=ref.ta_id,
        )
        decisions.append(decision)
        candidate_gold[ref.ta_id] = {g.category for g in miner.mine(decision)}

    manifest = _manifest(
        refs, decisions, candidate_gold, unavailable, parse_failed, model_cutoff
    )
    if out_dir is not None:
        _persist(out_dir, decisions, candidate_gold, manifest)
    return decisions, candidate_gold


def _manifest(
    refs: list,
    decisions: list[Decision],
    candidate_gold: CandidateGold,
    unavailable: list[str],
    parse_failed: list[str],
    model_cutoff: date,
) -> dict:
    post = [d for d in decisions if d.decision_date > model_cutoff]
    pre = [d for d in decisions if d.decision_date <= model_cutoff]
    post_ids = {d.decision_id for d in post}
    post_restricted = [d for d in post if d.outcome in NEGATIVE_OUTCOMES]

    coverage: Counter = Counter()
    coverage_post: Counter = Counter()
    for ta, cats in candidate_gold.items():
        for c in cats:
            coverage[c.value] += 1
            if ta in post_ids:
                coverage_post[c.value] += 1

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "model_cutoff": model_cutoff.isoformat(),
        "recent_slice_size": len(refs),
        "total_fetched": len(decisions),
        "unavailable": len(unavailable),
        "unavailable_ids": sorted(unavailable),
        "parse_failed": len(parse_failed),
        "parse_failed_ids": sorted(parse_failed),
        "n_pre": len(pre),
        "n_post": len(post),  # the real leakage-clean Arm A size
        "n_post_restricted": len(post_restricted),
        "n_post_restricted_ids": sorted(d.decision_id for d in post_restricted),
        "gold_category_coverage": dict(sorted(coverage.items())),
        "gold_category_coverage_post": dict(sorted(coverage_post.items())),
    }


def _gold_to_json(candidate_gold: CandidateGold) -> dict[str, list[str]]:
    # VulnCategory.value strings, sorted - drops straight into closed_book_probe.
    return {ta: sorted(c.value for c in cats) for ta, cats in candidate_gold.items()}


def _persist(
    out_dir: Path,
    decisions: list[Decision],
    candidate_gold: CandidateGold,
    manifest: dict,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "decisions.json").write_text(
        json.dumps([d.model_dump(mode="json") for d in decisions], indent=2)
    )
    (out_dir / "candidate_gold.json").write_text(
        json.dumps(_gold_to_json(candidate_gold), indent=2)
    )
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))


def _main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Build the Arm A dataset from NICE")
    ap.add_argument("index", help="path/URL to the NICE cancer-recommendations xlsx")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()
    src: bytes | str = args.index
    _, _ = build_arm_a_dataset(src, out_dir=args.out)
    print((args.out / "manifest.json").read_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
