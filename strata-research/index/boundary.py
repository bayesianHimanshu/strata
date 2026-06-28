"""Phase 2 Task 1 — the corpus-composition boundary.

A date cutoff alone does not stop the leak. NICE's own appraisal documents (ERG/EAG
report, committee papers, ACD, final guidance) carry the committee's reasoning and
are dated *before* the final decision — they pass the temporal buffer yet hand the
synthesizer the answer. The binding guard is **source-type exclusion**, composed on
top of the temporal LeakageFilter.

`RetrievalBoundary` is the single object retrieval is gated by. It admits a chunk iff
ALL hold:

  1. temporal:   the chunk's date clears the LeakageFilter (invariant #2);
  2. dossier:    the chunk is NOT a gold-bearing dossier doc of the *target* appraisal;
  3. sibling:    the chunk is NOT a gold-bearing dossier doc of a registered same-drug
                 *sibling* appraisal (a research parameter, default ON, never silent).

Pure and total. `index.store.search` takes a RetrievalBoundary and re-asserts it on
every returned hit, so a leaky query is unrepresentable.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta

from core.config import LEAKAGE_BUFFER
from core.contracts import DOSSIER_DOC_TYPES, Decision
from index.leakage import LeakageError, LeakageFilter
from sources.drug_identity import normalize_drug


class CorpusBoundaryError(LeakageError):
    """Raised when a source-type-excluded document reaches a guarded path.

    Subclasses LeakageError: every corpus-composition violation is a leakage event.
    """


def compute_sibling_map(decisions: Iterable[Decision]) -> dict[str, frozenset[str]]:
    """Map each appraisal_id -> the set of OTHER appraisal_ids sharing its primary
    molecule (per normalize_drug — NOT the raw technology string, which never matched
    because combinations differ). This is the input to the same-drug sibling policy.
    """
    by_drug: dict[str, set[str]] = {}
    for d in decisions:
        key = normalize_drug(d.drug or "").primary
        aid = d.appraisal_id or d.decision_id
        if not key or aid is None:
            continue
        by_drug.setdefault(key, set()).add(aid)
    out: dict[str, frozenset[str]] = {}
    for ids in by_drug.values():
        for aid in ids:
            out[aid] = frozenset(ids - {aid})
    return out


@dataclass(frozen=True)
class RetrievalBoundary:
    """The admissibility test for one target decision's retrieval corpus."""

    leakage: LeakageFilter
    target_appraisal_id: str | None = None
    sibling_appraisal_ids: frozenset[str] = frozenset()
    exclude_siblings: bool = True  # registered research parameter (default ON)
    # Molecule scope (Corpus rebuild §3): an ADDED filter on top of the three leakage
    # predicates — a chunk is in scope only if its drug is one of the decision's
    # molecules. Empty = scoping OFF (back-compat; the boundary then admits all drugs).
    molecules: frozenset[str] = frozenset()

    @classmethod
    def for_decision(
        cls,
        decision: Decision,
        *,
        buffer: timedelta = LEAKAGE_BUFFER,
        sibling_appraisal_ids: Iterable[str] = (),
        exclude_siblings: bool = True,
        molecules: Iterable[str] = (),
    ) -> RetrievalBoundary:
        return cls(
            leakage=LeakageFilter(decision.decision_date, buffer),
            target_appraisal_id=decision.appraisal_id or decision.decision_id,
            sibling_appraisal_ids=frozenset(sibling_appraisal_ids),
            exclude_siblings=exclude_siblings,
            molecules=frozenset(molecules),
        )

    # -- the three leakage predicates (unchanged) ----------------------------- #

    def _is_dossier(self, chunk) -> bool:
        return chunk.doc_type in DOSSIER_DOC_TYPES

    def _dossier_conflict(self, chunk) -> bool:
        return (
            chunk.appraisal_id is not None
            and chunk.appraisal_id == self.target_appraisal_id
            and self._is_dossier(chunk)
        )

    def _sibling_conflict(self, chunk) -> bool:
        return (
            self.exclude_siblings
            and chunk.appraisal_id is not None
            and chunk.appraisal_id in self.sibling_appraisal_ids
            and self._is_dossier(chunk)
        )

    # -- the added molecule-scope predicate ----------------------------------- #

    def _out_of_scope(self, chunk) -> bool:
        """True if molecule scoping is on and this chunk's drug is not in scope. An
        untagged (drug=None) chunk is out of scope once scoping is active."""
        if not self.molecules:
            return False
        return getattr(chunk, "drug", None) not in self.molecules

    # -- the gate ------------------------------------------------------------- #

    def admits(self, chunk) -> bool:
        return (
            self.leakage.admits(chunk.doc_date)
            and not self._dossier_conflict(chunk)
            and not self._sibling_conflict(chunk)
            and not self._out_of_scope(chunk)
        )

    def assert_admits(self, chunk) -> None:
        """Defense in depth. Raises the most specific reason a chunk is inadmissible."""
        if self._dossier_conflict(chunk):
            raise CorpusBoundaryError(
                f"dossier leakage: appraisal {chunk.appraisal_id!r} doc_type "
                f"{chunk.doc_type!r} is gold-bearing for target "
                f"{self.target_appraisal_id!r}"
            )
        if self._sibling_conflict(chunk):
            raise CorpusBoundaryError(
                f"sibling leakage: appraisal {chunk.appraisal_id!r} is a registered "
                f"same-drug sibling of target {self.target_appraisal_id!r}"
            )
        if self._out_of_scope(chunk):
            raise CorpusBoundaryError(
                f"out of molecule scope: chunk drug {getattr(chunk, 'drug', None)!r} "
                f"not in target molecules {sorted(self.molecules)}"
            )
        # temporal last: its message is the date bound (raises LeakageError)
        self.leakage.assert_admits(chunk)

    def filter(self, chunks: Iterable) -> list:
        return [c for c in chunks if self.admits(c)]

    def policy(self) -> dict:
        """The boundary's configuration, for logging (never a silent default)."""
        return {
            "target_appraisal_id": self.target_appraisal_id,
            "cutoff": self.leakage.cutoff.isoformat(),
            "buffer_days": self.leakage.buffer.days,
            "exclude_same_drug_siblings": self.exclude_siblings,
            "n_sibling_appraisals": len(self.sibling_appraisal_ids),
            "excluded_dossier_doc_types": sorted(t.value for t in DOSSIER_DOC_TYPES),
            "molecule_scope": sorted(self.molecules) if self.molecules else "off",
        }
