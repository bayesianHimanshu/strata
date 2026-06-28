"""The leakage boundary — the trust-critical core, carried over from STRATA.

A retrieval over the store MUST be parameterised by a boundary. The boundary composes a
date cutoff, dossier-disjointness, a sibling policy, and drug scoping into one predicate;
``admits()`` is their conjunction. There is deliberately no way to search the store without
a boundary.

**One boundary, two modes** (real-time context spec):
  - ``backtest`` (validation): ``as_of = decision_date``, cutoff = as_of − buffer, the
    decision's own dossier and same-drug siblings excluded. This is the experiment.
  - ``live`` (the product): ``as_of = today`` (or a chosen submission date), cutoff = as_of
    (inclusive), no dossier/sibling exclusion — there is no future decision to leak from.

Same predicate machinery; only the cutoff and the exclusions differ. Build via the
``.backtest()`` / ``.live()`` factories; the original keyword construction still works
(mode defaults to backtest, as_of falls back to decision_date).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from strata_platform.substrate.contracts import Chunk, DocType

_DOSSIER = {DocType.ta_final_guidance, DocType.ta_committee_papers,
            DocType.ta_erg_report, DocType.ta_acd}


def _molecules(drug: str) -> frozenset[str]:
    """Molecule scope via the single normalizer (lazy import keeps substrate base-light)."""
    from strata_platform.sources.drug_identity import normalize_drug

    return normalize_drug(drug or "").molecules


@dataclass(frozen=True)
class RetrievalBoundary:
    decision_id: str = ""
    decision_date: date | None = None
    molecules: frozenset[str] = field(default_factory=frozenset)
    buffer_days: int = 90
    exclude_siblings: bool = True
    sibling_ids: frozenset[str] = field(default_factory=frozenset)
    mode: str = "backtest"                 # "backtest" | "live"
    as_of: date | None = None

    @property
    def _anchor(self) -> date:
        base = self.as_of or self.decision_date
        if base is None:
            raise ValueError("boundary requires as_of or decision_date")
        return base

    @property
    def cutoff(self) -> date:
        if self.mode == "live":
            return self._anchor
        return self._anchor - timedelta(days=self.buffer_days)

    def admits(self, c: Chunk) -> bool:
        # date
        if c.doc_date is None:
            return False
        if self.mode == "backtest" and c.doc_date >= self.cutoff:
            return False
        if self.mode == "live" and c.doc_date > self.cutoff:   # inclusive up to as_of
            return False
        # dossier-disjointness + sibling policy apply only in backtest (validation)
        if self.mode == "backtest":
            if c.appraisal_id == self.decision_id and c.doc_type in _DOSSIER:
                return False
            if self.exclude_siblings and c.appraisal_id in self.sibling_ids:
                return False
        # drug scoping applies in both modes
        if self.molecules and c.drug:
            cd = c.drug.lower()
            if not any(m == cd or m in cd or cd in m for m in self.molecules):
                return False
        return True

    def policy(self) -> dict:
        """Human-readable record of the boundary for the audit trail."""
        return {
            "decision_id": self.decision_id or None,
            "mode": self.mode,
            "as_of": self._anchor.isoformat(),
            "cutoff": self.cutoff.isoformat(),
            "buffer_days": self.buffer_days,
            "exclude_siblings": self.exclude_siblings if self.mode == "backtest" else False,
            "molecules": sorted(self.molecules),
        }

    # --- factories ---------------------------------------------------------- #

    @classmethod
    def backtest(cls, decision, *, buffer_days: int = 90,
                 sibling_ids: frozenset[str] = frozenset(),
                 exclude_siblings: bool = True) -> RetrievalBoundary:
        return cls(decision_id=decision.decision_id, decision_date=decision.decision_date,
                   molecules=_molecules(decision.drug), buffer_days=buffer_days,
                   exclude_siblings=exclude_siblings, sibling_ids=sibling_ids,
                   mode="backtest")

    @classmethod
    def live(cls, molecules, *, as_of: date | None = None,
             decision_id: str = "") -> RetrievalBoundary:
        return cls(decision_id=decision_id, molecules=frozenset(molecules),
                   as_of=as_of or date.today(), mode="live", buffer_days=0,
                   exclude_siblings=False)
