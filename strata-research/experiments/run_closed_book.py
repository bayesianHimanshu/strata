"""STRATA — closed-book probe runner for GPT-5.5 (experiments/run_closed_book.py).

Loads data/arm_a/decisions.json + audit/human_gold.json (the SME-verified answer
key), runs the closed-book control on GPT-5.5, and reports closed-book recall on the
leakage-clean slice plus the pre/post contamination contrast.

GPT-5.5 specifics (verified against OpenAI's model docs, June 2026):
  - knowledge cutoff = 2025-12-01 → that is the clean-slice boundary, NOT the old
    2026-02-01 placeholder. Decisions after it are leakage-clean.
  - it is a reasoning model: `temperature` is unsupported (400s). We omit it, use
    `max_completion_tokens`, and `reasoning_effort`. Forced default sampling means
    runs are not bit-reproducible; we log `seed` and say so.

Because the stated cutoff is a "reliable knowledge" date (trailing months are thin
but not provably empty), we ALSO report a strict sub-slice (decisions well past the
cutoff) as a conservative robustness read.

    OPENAI_API_KEY=...  python -m experiments.run_closed_book \
        --decisions data/arm_a/decisions.json --gold audit/human_gold.json
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from eval.closed_book_probe import (  # the tested probe core
    ProbeDecision,
    RunConfig,
    _per_category_recall,
    run_probe,
)

load_dotenv()

GPT55_CUTOFF = date(2025, 12, 1)              # OpenAI model page, verified June 2026
STRICT_CUTOFF = date(2026, 2, 1)             # conservative: well clear of the cutoff


# --------------------------------------------------------------------------- #
# OpenAI reasoning-model reasoner
# --------------------------------------------------------------------------- #

class OpenAIReasoner:
    """Chat Completions against a GPT-5.x reasoning model. No temperature (rejected
    by reasoning models); uses max_completion_tokens and reasoning_effort."""

    def __init__(self, config: RunConfig, *, reasoning_effort: str = "low",
                 seed: int | None = 7, api_key: str | None = None) -> None:
        self._cfg = config
        self._effort = reasoning_effort
        self._seed = seed
        self._key = api_key or os.environ["OPENAI_API_KEY"]

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        import httpx
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body: dict = {
            "model": self._cfg.model,
            "messages": messages,
            # reasoning tokens count against this budget — keep it generous so the
            # short JSON answer isn't starved by the thinking.
            "max_completion_tokens": self._cfg.max_tokens,
        }
        if self._effort:
            body["reasoning_effort"] = self._effort
        if self._seed is not None:
            body["seed"] = self._seed
        # NOTE: deliberately NO temperature — GPT-5.5 rejects it.
        r = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {self._key}",
                     "content-type": "application/json"},
            json=body, timeout=180.0,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"] or ""


# --------------------------------------------------------------------------- #
# Input assembly (pure, testable)
# --------------------------------------------------------------------------- #

def build_inputs(decisions_path: str, gold_path: str
                 ) -> tuple[list[ProbeDecision], dict[str, set[str]]]:
    raw = json.loads(Path(decisions_path).read_text())
    records = raw.values() if isinstance(raw, dict) else raw
    gold = {k: set(v) for k, v in json.loads(Path(gold_path).read_text()).items()}
    decisions: list[ProbeDecision] = []
    for d in records:
        did = d["decision_id"]
        if did not in gold:                  # only score what the SME labelled
            continue
        decisions.append(ProbeDecision(
            decision_id=did,
            decision_date=date.fromisoformat(str(d["decision_date"])[:10]),
            agency=d.get("agency", "NICE"),
            drug=d.get("drug", ""),
            indication=d.get("indication", ""),
        ))
    return decisions, gold


def strict_slice_recall(decisions, gold, predictions, taxonomy, strict_cutoff):
    ids = [d.decision_id for d in decisions if d.decision_date > strict_cutoff]
    return _per_category_recall(ids, gold, predictions, taxonomy)


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description="closed-book probe on GPT-5.5")
    ap.add_argument("--decisions", default="data/arm_a/decisions.json")
    ap.add_argument("--gold", default="audit/human_gold.json")
    ap.add_argument("--model", default="gpt-5.5")
    ap.add_argument("--max-tokens", type=int, default=2048)
    ap.add_argument("--reasoning-effort", default="low")
    ap.add_argument("--out", default="data/arm_a/closed_book_gpt55.json")
    args = ap.parse_args()

    from core.contracts import VulnCategory  # type: ignore
    from core.provenance import snapshot  # type: ignore
    from eval.rubric import assert_rubric_committed, rubric_hash  # type: ignore
    taxonomy = [c.value for c in VulnCategory]

    assert_rubric_committed()                         # invariant #6 — gate every run

    decisions, gold = build_inputs(args.decisions, args.gold)
    cfg = RunConfig(model=args.model, model_cutoff=GPT55_CUTOFF,
                    max_tokens=args.max_tokens)
    reasoner = OpenAIReasoner(cfg, reasoning_effort=args.reasoning_effort)

    report = run_probe(decisions, gold, reasoner, cfg, taxonomy,
                       rubric_hash=rubric_hash())
    out = report.to_dict()

    # correct the metadata for a reasoning model + add the strict robustness read
    out["metadata"]["provider"] = "openai"
    out["metadata"]["reasoning_effort"] = args.reasoning_effort
    out["metadata"]["temperature"] = "n/a (GPT-5.5 reasoning model — default sampling)"
    out["metadata"]["reproducibility_note"] = (
        "default sampling forced; seed logged but bit-reproducibility not guaranteed"
    )
    out["strict_post_slice"] = {
        "cutoff": STRICT_CUTOFF.isoformat(),
        "note": "decisions well clear of the stated cutoff — conservative clean read",
        **strict_slice_recall(decisions, gold, report.closed_book_predictions,
                              taxonomy, STRICT_CUTOFF),
    }

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2, default=str))
    snapshot(json.dumps(out, default=str).encode(),
             source="probe", source_id="closed_book_gpt55", url="local://probe")

    md = out["metadata"]
    print(f"model={md['model']} cutoff={md['model_cutoff']} "
          f"n_pre={md['n_pre']} n_post={md['n_post']}")
    print("closed-book POST micro recall:", out["closed_book"]["post"]["micro_recall"])
    print(f"strict POST (>{STRICT_CUTOFF}) micro recall:",
          out["strict_post_slice"]["micro_recall"])
    print("contamination pre_minus_post:", out["contamination"]["pre_minus_post"])
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())