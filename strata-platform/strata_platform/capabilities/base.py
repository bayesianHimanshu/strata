"""Capability agents are thin: each is a class over the shared substrate (reasoner +
store + boundary) implementing one IEG capability. They are interchangeable behind this
interface, which is what lets the platform add capabilities without touching the spine.
"""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod

from strata_platform.substrate.boundary import RetrievalBoundary
from strata_platform.substrate.contracts import (
    Chunk,
    CapabilityRequest,
    CapabilityResult,
    Provenance,
    VulnCategory,
)
from strata_platform.substrate.reasoner import Reasoner


def parse_categories(text: str) -> set[VulnCategory]:
    allowed = {c.value for c in VulnCategory}
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        data = json.loads(cleaned)
        items = data if isinstance(data, list) else data.get("categories", [])
    except (json.JSONDecodeError, AttributeError):
        items = [c for c in allowed if re.search(rf"\b{re.escape(c)}\b", text, re.I)]
    return {VulnCategory(x) for x in (str(i).strip().lower() for i in items) if x in allowed}


def parse_json_object(text: str) -> dict:
    """Tolerant parse of a model's JSON object reply (handles ```json fences). Returns
    {} on failure rather than raising - the caller decides what an empty extraction means."""
    cleaned = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(),
                     flags=re.MULTILINE).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def evidence_block(chunks: list[Chunk], *, cap_chars: int = 500) -> str:
    """Format retrieved chunks for a grounding prompt, source-tagged (provenance)."""
    return "\n\n".join(f"[{c.source_id}] {c.text[:cap_chars]}" for c in chunks)


def provenance_for(chunks: list[Chunk], boundary: RetrievalBoundary,
                   note: str) -> Provenance:
    return Provenance(chunk_ids=[c.chunk_id for c in chunks],
                      source_ids=sorted({c.source_id for c in chunks}),
                      note=f"{note}; boundary {boundary.policy()}")


def build_boundary(decision, params: dict | None = None, *,
                   sibling_ids=frozenset(), buffer_days: int | None = None
                   ) -> RetrievalBoundary:
    """Build the retrieval boundary for a request, picking backtest vs live from
    ``params['boundary_mode']`` (and optional ``params['as_of']``). Backtest is the
    validation default (cutoff = decision_date − buffer, dossier/siblings excluded); live is
    prospective (cutoff = as_of, no exclusions). A distinct key from HTA's open/closed
    ``mode``. One place, used by every capability."""
    from datetime import date

    from strata_platform.config import get_settings
    from strata_platform.sources.drug_identity import normalize_drug

    params = params or {}
    if params.get("boundary_mode") == "live":
        raw = params.get("as_of")
        as_of = date.fromisoformat(raw) if isinstance(raw, str) else raw
        return RetrievalBoundary.live(normalize_drug(decision.drug or "").molecules,
                                      as_of=as_of, decision_id=decision.decision_id)
    bd = buffer_days if buffer_days is not None else get_settings().retrieval_buffer_days
    return RetrievalBoundary.backtest(decision, buffer_days=bd,
                                      sibling_ids=frozenset(sibling_ids))


class Capability(ABC):
    key: str
    summary: str

    @abstractmethod
    def run(self, request: CapabilityRequest, *, reasoner: Reasoner,
            store) -> CapabilityResult: ...
