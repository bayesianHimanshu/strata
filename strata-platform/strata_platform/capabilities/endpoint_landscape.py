"""Endpoint & Comparator Landscape - indication-centric, structured-first.

For an indication, reconstruct the endpoints and comparators registered trials have used,
to inform trial design before submission. Endpoints/comparators are STRUCTURED fields in
the CT.gov v2 registration, so we aggregate and **count deterministically** and use the
model ONLY to (a) cluster variant names into canonical entries and (b) flag surrogates
(lexicon-first - the model never overrules a known case). Every entry traces to the NCT
ids that produced it; implications cite only existing entries (no new facts).
"""
from __future__ import annotations

from datetime import date

from strata_platform.capabilities.base import Capability, parse_json_object
from strata_platform.sources.clinicaltrials import StructuredTrial, fetch_trials_structured
from strata_platform.substrate.boundary import RetrievalBoundary
from strata_platform.substrate.contracts import (
    CapabilityRequest,
    CapabilityResult,
    ComparatorEntry,
    DesignImplication,
    EndpointEntry,
    HTASignal,
    OutcomeKind,
    Provenance,
)
from strata_platform.substrate.reasoner import Reasoner

# Deterministic surrogate lexicon - the model never overrules these. Returns True
# (surrogate), False (final outcome), or None (unknown -> ask the model).
_SURROGATE_CUES = ("progression-free survival", "progression free survival", "pfs",
                   "objective response", "response rate", "orr", "overall response",
                   "disease-free survival", "disease free survival", "dfs",
                   "event-free survival", "event free survival", "efs",
                   "pathologic complete response", "pathological complete response", "pcr",
                   "time to progression", "ttp", "duration of response", "dor",
                   "minimal residual disease", "mrd")
_FINAL_CUES = ("overall survival", " os ", "quality of life", "qol", "hrqol",
               "patient-reported", "eq-5d", "mortality")


def _lexicon_surrogate(text: str) -> bool | None:
    t = f" {text.lower()} "
    if any(c in t for c in _SURROGATE_CUES):
        return True
    if any(c in t for c in _FINAL_CUES):
        return False
    return None


def _as_of(params: dict) -> date:
    raw = params.get("as_of")
    return date.fromisoformat(raw) if isinstance(raw, str) else (raw or date.today())



# Deterministic aggregate


def _collect(trials: list[StructuredTrial]):
    """Bucket outcomes + comparator interventions -> raw string -> set of nct ids. Counts
    come from HERE, not the model."""
    raw_ep: dict[str, dict] = {}
    raw_cmp: dict[str, set[str]] = {}
    for t in trials:
        nct = t.nct_id
        for o in [*t.primary_outcomes, *t.secondary_outcomes]:
            m = o.measure.strip()
            if not m:
                continue
            e = raw_ep.setdefault(m, {"ncts": set(), "primary": False})
            e["ncts"].add(nct)
            if o.kind == "primary":
                e["primary"] = True
        for arm in t.arms:
            if not arm.is_comparator:
                continue
            for name in arm.intervention_names:
                n = name.strip()
                if n:
                    raw_cmp.setdefault(n, set()).add(nct)
    return raw_ep, raw_cmp



# LLM canonicalisation (the only model step for the entries)


_EP_SYSTEM = (
    "Cluster these raw trial-outcome strings into canonical endpoints. For each cluster "
    "give {canonical, kind, raw_variants, is_surrogate}. Mark surrogates (PFS, ORR, DFS, "
    "EFS, pCR, time-to-progression) is_surrogate=true; final outcomes (overall survival, "
    "quality of life) false. Output strict JSON {\"clusters\": [..]}. Do not invent "
    "endpoints not in the input.")
_CMP_SYSTEM = (
    "Cluster these raw trial comparator/intervention strings into canonical comparators. "
    "For each cluster give {canonical, comparator_class, raw_variants}. Output strict JSON "
    "{\"clusters\": [..]}. Do not invent comparators not in the input.")


def _clusters(reasoner: Reasoner, raw_keys: list[str], system: str) -> list[dict]:
    if not raw_keys:
        return []
    prompt = "Raw strings:\n" + "\n".join(f"- {k}" for k in raw_keys) + \
             "\n\nReturn the JSON clusters object."
    return parse_json_object(reasoner.complete(prompt, system=system)).get("clusters", [])


def _canonicalise_endpoints(reasoner: Reasoner, raw_ep: dict) -> list[EndpointEntry]:
    clusters = _clusters(reasoner, list(raw_ep), _EP_SYSTEM)
    entries: list[EndpointEntry] = []
    covered: set[str] = set()

    def _make(canonical: str, variants: list[str], model_surrogate: bool | None):
        ncts: set[str] = set()
        primary = False
        used: list[str] = []
        for v in variants:
            if v in raw_ep:
                ncts |= raw_ep[v]["ncts"]
                primary = primary or raw_ep[v]["primary"]
                used.append(v)
                covered.add(v)
        if not used:
            return None
        # surrogate: deterministic lexicon wins; fall back to the model only if unknown.
        lex = next((s for s in (_lexicon_surrogate(x) for x in [canonical, *used])
                    if s is not None), None)
        is_surrogate = lex if lex is not None else bool(model_surrogate)
        return EndpointEntry(
            canonical=canonical or used[0],
            kind=OutcomeKind.primary if primary else OutcomeKind.secondary,
            is_surrogate=is_surrogate, trial_count=len(ncts), raw_variants=sorted(used),
            provenance=Provenance(source_ids=sorted(ncts), note="trial outcome cluster"))

    for c in clusters:
        variants = [v for v in (c.get("raw_variants") or []) if isinstance(v, str)]
        e = _make(c.get("canonical", ""), variants, c.get("is_surrogate"))
        if e is not None:
            entries.append(e)
    for raw in raw_ep:                       # any uncovered raw string -> identity cluster
        if raw not in covered:
            e = _make(raw, [raw], None)
            if e is not None:
                entries.append(e)
    entries.sort(key=lambda e: e.trial_count, reverse=True)
    return entries


def _canonicalise_comparators(reasoner: Reasoner, raw_cmp: dict) -> list[ComparatorEntry]:
    clusters = _clusters(reasoner, list(raw_cmp), _CMP_SYSTEM)
    entries: list[ComparatorEntry] = []
    covered: set[str] = set()

    def _make(canonical: str, variants: list[str], klass):
        ncts: set[str] = set()
        used: list[str] = []
        for v in variants:
            if v in raw_cmp:
                ncts |= raw_cmp[v]
                used.append(v)
                covered.add(v)
        if not used:
            return None
        return ComparatorEntry(
            canonical=canonical or used[0], comparator_class=klass or None,
            trial_count=len(ncts), raw_variants=sorted(used),
            provenance=Provenance(source_ids=sorted(ncts), note="comparator arm cluster"))

    for c in clusters:
        variants = [v for v in (c.get("raw_variants") or []) if isinstance(v, str)]
        e = _make(c.get("canonical", ""), variants, c.get("comparator_class"))
        if e is not None:
            entries.append(e)
    for raw in raw_cmp:
        if raw not in covered:
            e = _make(raw, [raw], None)
            if e is not None:
                entries.append(e)
    entries.sort(key=lambda e: e.trial_count, reverse=True)
    return entries


def _enrich_hta(comparators: list[ComparatorEntry], indication: str, store) -> None:
    """Best-effort NICE signal from dossier chunks in the store. Leaves ``unknown`` when
    absent - never guesses."""
    try:
        from strata_platform.substrate.contracts import DocType
        boundary = RetrievalBoundary.live(frozenset(), as_of=date.today())
        chunks = [c for c in store.search(indication, boundary, k=24)
                  if c.doc_type == DocType.ta_final_guidance]
    except Exception:  # noqa: BLE001 - enrichment must never break the run
        return
    for cmp in comparators:
        term = cmp.canonical.lower().split()[0] if cmp.canonical else ""
        if not term:
            continue
        for ch in chunks:
            low = ch.text.lower()
            if term in low:
                if "not recommend" in low or "not a relevant compar" in low:
                    cmp.hta_signal = HTASignal.contested
                elif "recommend" in low or "standard of care" in low:
                    cmp.hta_signal = HTASignal.accepted
                break


_IMPLY_SYSTEM = (
    "You advise on trial design from an endpoint/comparator landscape. Write 3-6 concise "
    "design implications using ONLY the provided entries; each cites the entry_ids (or nct "
    "ids) it rests on. Introduce no new facts. Output strict JSON "
    "{\"implications\": [{\"text\": .., \"refs\": [..]}]}.")


def _imply(reasoner: Reasoner, endpoints: list[EndpointEntry],
           comparators: list[ComparatorEntry]) -> list[DesignImplication]:
    valid = {e.entry_id for e in endpoints} | {c.entry_id for c in comparators}
    valid |= {n for e in endpoints for n in e.provenance.source_ids}
    valid |= {n for c in comparators for n in c.provenance.source_ids}
    lines = [f"{e.entry_id}: endpoint {e.canonical} ({e.kind.value}, "
             f"surrogate={e.is_surrogate}, {e.trial_count} trials)" for e in endpoints]
    lines += [f"{c.entry_id}: comparator {c.canonical} ({c.trial_count} trials, "
              f"hta={c.hta_signal.value})" for c in comparators]
    out = parse_json_object(reasoner.complete(
        "Entries:\n" + "\n".join(lines) + "\n\nReturn the JSON implications object.",
        system=_IMPLY_SYSTEM))
    imps: list[DesignImplication] = []
    for raw in out.get("implications", []):
        refs = [r for r in (raw.get("refs") or []) if r in valid]
        text = (raw.get("text") or "").strip()
        if text and refs:                    # no-new-facts: refs must be real entries
            imps.append(DesignImplication(text=text, refs=refs))
    if not imps and endpoints:               # deterministic fallback grounded in the data
        top = endpoints[0]
        if top.is_surrogate and top.kind == OutcomeKind.primary:
            imps.append(DesignImplication(
                text=(f"{top.canonical} dominates as a primary endpoint "
                      f"({top.trial_count} trials) but is a surrogate - expect HTA pressure "
                      "on overall-survival maturity."),
                refs=[top.entry_id]))
    return imps


class EndpointLandscape(Capability):
    key = "endpoint_landscape"
    summary = ("Reconstruct the endpoints and comparators trials/appraisals used for an "
               "indication, to inform trial design before submission.")

    def run(self, request: CapabilityRequest, *, reasoner: Reasoner,
            store) -> CapabilityResult:
        indication = (request.params.get("indication") or "").strip()
        if not indication:
            raise ValueError("endpoint_landscape requires params.indication")
        as_of = _as_of(request.params)
        trials = fetch_trials_structured(indication, as_of=as_of,
                                         drug=request.params.get("drug"))
        if not trials:
            raise ValueError(f"no trials found for '{indication}' as-of {as_of}")

        raw_ep, raw_cmp = _collect(trials)
        endpoints = _canonicalise_endpoints(reasoner, raw_ep)
        comparators = _canonicalise_comparators(reasoner, raw_cmp)
        _enrich_hta(comparators, indication, store)
        implications = _imply(reasoner, endpoints, comparators)

        from strata_platform.substrate.contracts import LandscapeResult
        result = LandscapeResult(
            indication=indication, as_of=as_of, trials_analysed=len(trials),
            endpoints=endpoints, comparators=comparators, implications=implications,
            boundary=RetrievalBoundary.live(frozenset(), as_of=as_of).policy())
        return CapabilityResult(capability=self.key, tenant_id=request.tenant_id,
                                payload=result.model_dump(mode="json"))
