"""STRATA - closed-book probe (eval/closed_book_probe.py).

The cheapest, highest-information experiment in the project. It runs the synthesizer's
CLOSED-BOOK control - no retrieval, no dossier, parametric knowledge only - over the
post-cutoff (leakage-clean) slice, and scores how many committee-cited vulnerability
CATEGORIES the model recovers from priors alone.

What the numbers mean:
  - closed-book recall on the POST-cutoff slice = the model's genuine parametric
    prior over HTA failure modes (it cannot have trained on these decisions). High
    here means the apparent "HTA Archaeology" skill is largely general knowledge,
    not decision-specific retrieval - the H-A2 reframing.
  - closed-book recall PRE vs POST = contamination signal. If pre >> post, the model
    memorised specific pre-cutoff decisions (it read the post-hoc write-ups).
  - open − closed (per category, on the clean slice) = the signal attributable to
    the retrieval system beyond parametric priors. Reported as a SIGNED vector, not a
    scalar: where closed exceeds open, the model knows it but didn't retrieve it, and
    that shape is itself a finding.

Design: categories are plain strings (the VulnCategory .value set, passed in as the
taxonomy) so the run/scoring logic has no hard dependency on the rest of the package
and is fully unit-testable. The real entrypoint gates on assert_rubric_committed and
snapshots the report (ELEVATE domains 1 & 5); the pure functions do no I/O.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Protocol

from dotenv import load_dotenv

load_dotenv()



# Seams


class Reasoner(Protocol):
    """Matches the injectable seam in agents/synthesizer.py. If the method name
    there differs, adapt with a one-line wrapper rather than changing this."""

    def complete(self, prompt: str, *, system: str | None = None) -> str: ...


@dataclass(frozen=True)
class ProbeDecision:
    decision_id: str
    decision_date: date
    agency: str
    drug: str
    indication: str

    # from core.contracts.Decision: ProbeDecision(d.decision_id, d.decision_date,
    #   d.agency, d.drug, d.indication)


@dataclass(frozen=True)
class RunConfig:
    model: str                       # the REAL model string you will run on
    model_cutoff: date               # its training cutoff - defines the clean slice
    temperature: float = 0.0         # 0 for reproducibility (LLMs still not exact)
    max_tokens: int = 1024



# Prompt (closed-book: parametric only, no context)


CLOSED_BOOK_SYSTEM = (
    "You assess health technology appraisals. Using ONLY general knowledge of HTA "
    "decision-making and the named drug and indication - with NO access to the "
    "appraisal documents, trial data, or the committee's report - predict which "
    "CATEGORIES of evidence vulnerability the committee most likely cited. Stay "
    "strictly within the provided taxonomy. Output strict JSON: a list of category "
    "ids, nothing else - no prose, no markdown."
)


def build_closed_book_prompt(d: ProbeDecision, taxonomy: list[str]) -> str:
    cats = "\n".join(f"  - {c}" for c in taxonomy)
    return (
        f"HTA body: {d.agency}\n"
        f"Technology: {d.drug}\n"
        f"Indication: {d.indication}\n\n"
        f"Taxonomy of vulnerability category ids:\n{cats}\n\n"
        f'Return a JSON list of the category ids you predict the committee cited, '
        f'e.g. ["comparator", "icer_uncertainty"].'
    )


def parse_categories(text: str, taxonomy: list[str]) -> set[str]:
    """Defensively parse a model reply into a set of known category ids."""
    allowed = {c.lower() for c in taxonomy}
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        data = json.loads(cleaned)
        items = data if isinstance(data, list) else data.get("categories", [])
    except (json.JSONDecodeError, AttributeError):
        # fall back to substring matching so a chatty reply still scores
        items = [c for c in taxonomy if re.search(rf"\b{re.escape(c)}\b", text, re.I)]
    return {str(x).strip().lower() for x in items if str(x).strip().lower() in allowed}


def predict_closed_book(reasoner: Reasoner, d: ProbeDecision,
                        taxonomy: list[str]) -> set[str]:
    reply = reasoner.complete(build_closed_book_prompt(d, taxonomy),
                              system=CLOSED_BOOK_SYSTEM)
    return parse_categories(reply, taxonomy)



# Scoring (pure)


def _per_category_recall(
    decision_ids: list[str],
    gold: dict[str, set[str]],
    pred: dict[str, set[str]],
    taxonomy: list[str],
) -> dict:
    """Per-category recall over a slice, plus micro/macro aggregates."""
    per_cat: dict[str, dict] = {}
    hit_total = goldpos_total = 0
    for c in taxonomy:
        goldpos = sum(1 for did in decision_ids if c in gold.get(did, set()))
        hit = sum(1 for did in decision_ids
                  if c in gold.get(did, set()) and c in pred.get(did, set()))
        predpos = sum(1 for did in decision_ids if c in pred.get(did, set()))
        per_cat[c] = {
            "gold_positives": goldpos,
            "predicted": predpos,
            "hits": hit,
            "recall": (hit / goldpos) if goldpos else None,
            "precision": (hit / predpos) if predpos else None,
        }
        hit_total += hit
        goldpos_total += goldpos
    recalls = [v["recall"] for v in per_cat.values() if v["recall"] is not None]
    return {
        "n_decisions": len(decision_ids),
        "micro_recall": (hit_total / goldpos_total) if goldpos_total else None,
        "macro_recall": (sum(recalls) / len(recalls)) if recalls else None,
        "by_category": per_cat,
    }


def _signed_delta(open_slice: dict, closed_slice: dict, taxonomy: list[str]) -> dict:
    """open − closed per-category recall, SIGNED (not floored). Flags categories
    where parametric (closed) exceeds retrieval (open)."""
    out: dict[str, dict] = {}
    for c in taxonomy:
        o = open_slice["by_category"][c]["recall"]
        cl = closed_slice["by_category"][c]["recall"]
        delta = None if (o is None or cl is None) else round(o - cl, 4)
        out[c] = {"open_recall": o, "closed_recall": cl, "delta_open_minus_closed": delta,
                  "parametric_exceeds_retrieval": (delta is not None and delta < 0)}
    return out



# Run (pure orchestration - no I/O)


@dataclass
class ProbeReport:
    metadata: dict
    closed_book: dict           # {"pre": slice, "post": slice}
    contamination: dict         # pre vs post micro recall + per-cat gap
    attributable_signal: dict | None = None   # post-slice open−closed, if open given
    closed_book_predictions: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "metadata": self.metadata,
            "closed_book": self.closed_book,
            "contamination": self.contamination,
            "attributable_signal": self.attributable_signal,
            "closed_book_predictions": {k: sorted(v) for k, v in
                                        self.closed_book_predictions.items()},
        }


def run_probe(
    decisions: list[ProbeDecision],
    gold: dict[str, set[str]],
    reasoner: Reasoner,
    config: RunConfig,
    taxonomy: list[str],
    *,
    open_predictions: dict[str, set[str]] | None = None,
    rubric_hash: str | None = None,
) -> ProbeReport:
    pre = [d for d in decisions if d.decision_date <= config.model_cutoff]
    post = [d for d in decisions if d.decision_date > config.model_cutoff]

    closed_pred: dict[str, set[str]] = {
        d.decision_id: predict_closed_book(reasoner, d, taxonomy) for d in decisions
    }

    pre_ids = [d.decision_id for d in pre]
    post_ids = [d.decision_id for d in post]
    closed = {
        "pre": _per_category_recall(pre_ids, gold, closed_pred, taxonomy),
        "post": _per_category_recall(post_ids, gold, closed_pred, taxonomy),
    }

    # contamination: pre vs post closed-book recall (post is the clean reference)
    contamination = {
        "pre_micro_recall": closed["pre"]["micro_recall"],
        "post_micro_recall": closed["post"]["micro_recall"],
        "pre_minus_post": _safe_sub(closed["pre"]["micro_recall"],
                                    closed["post"]["micro_recall"]),
        "interpretation": (
            "pre >> post suggests parametric contamination (model memorised specific "
            "pre-cutoff decisions); pre ~= post suggests a general prior, not leakage"
        ),
    }

    attributable = None
    if open_predictions is not None:
        open_post = _per_category_recall(post_ids, gold, open_predictions, taxonomy)
        attributable = {
            "slice": "post_cutoff (clean)",
            "open_micro_recall": open_post["micro_recall"],
            "closed_micro_recall": closed["post"]["micro_recall"],
            "by_category": _signed_delta(open_post, closed["post"], taxonomy),
        }

    prompt_hash = hashlib.sha256(
        (CLOSED_BOOK_SYSTEM + "||" + "|".join(taxonomy)).encode()
    ).hexdigest()[:16]
    metadata = {
        "generated_at": datetime.now(UTC).isoformat(),
        "model": config.model,
        "model_cutoff": config.model_cutoff.isoformat(),
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
        "prompt_template_hash": prompt_hash,
        "rubric_hash": rubric_hash,
        "taxonomy": taxonomy,
        "n_total": len(decisions), "n_pre": len(pre), "n_post": len(post),
        "control": "closed-book (no retrieval, no dossier, parametric only)",
    }
    return ProbeReport(metadata, closed, contamination, attributable, closed_pred)


def _safe_sub(a, b):
    return None if (a is None or b is None) else round(a - b, 4)



# Real reasoner (used in your environment; not exercised by the offline tests)


class AnthropicReasoner:
    """Calls the Anthropic Messages API. Inject the REAL model you are measuring on;
    set RunConfig.model_cutoff to that model's training cutoff so the post slice is
    genuinely leakage-clean."""

    def __init__(self, config: RunConfig, api_key: str | None = None,
                 anthropic_version: str = "2023-06-01") -> None:
        import os
        self._cfg = config
        self._key = api_key or os.environ["ANTHROPIC_API_KEY"]
        self._ver = anthropic_version

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        import httpx
        body = {
            "model": self._cfg.model,
            "max_tokens": self._cfg.max_tokens,
            "temperature": self._cfg.temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            body["system"] = system
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": self._key, "anthropic-version": self._ver,
                     "content-type": "application/json"},
            json=body, timeout=120.0,
        )
        r.raise_for_status()
        blocks = r.json().get("content", [])
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")



# Entrypoint (gates + snapshots; thin, all I/O lives here)


def main(
    decisions: list[ProbeDecision],
    gold: dict[str, set[str]],
    config: RunConfig,
    taxonomy: list[str],
    *,
    open_predictions: dict[str, set[str]] | None = None,
) -> dict:
    """Wire the rubric gate + snapshot here. Kept import-light so the pure run logic
    above stays testable without the package installed."""
    from core.provenance import snapshot  # type: ignore
    from eval.rubric import assert_rubric_committed, rubric_hash  # type: ignore

    assert_rubric_committed()                            # invariant #6 - no ungated run
    reasoner = AnthropicReasoner(config)
    report = run_probe(decisions, gold, reasoner, config, taxonomy,
                       open_predictions=open_predictions, rubric_hash=rubric_hash())
    payload = json.dumps(report.to_dict(), indent=2, default=str).encode()
    snapshot(payload, source="probe", source_id="closed_book", url="local://probe")
    return report.to_dict()