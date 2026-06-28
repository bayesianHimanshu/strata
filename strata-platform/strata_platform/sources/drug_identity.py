"""Drug identity normalization — the ONE normalizer.

A NICE "technology" string is usually a regimen, e.g. "Belantamab mafodotin with
pomalidomide and dexamethasone". ``normalize_drug`` parses it into the novel molecule(s)
under appraisal (INN keys), dropping the backbone agents that follow "with" (steroids,
chemotherapy) and salt/dosing noise.

This single function is used by BOTH corpus gathering and retrieval scoping (so a chunk's
drug key and a decision's molecule scope are produced identically) and by the sibling map
— which previously matched on the raw string and so never fired.

``normalize_drug`` is pure, deterministic, and offline: brand→INN goes through the static
``_BRAND2INN`` map. ``RxNormResolver`` (RxNav REST) optionally resolves brands/synonyms
the static map doesn't know, at ingestion time only; resolved pairs are merged back into
the runtime map so the normalizer stays the single source of truth. Tests never touch the
network — they exercise the static path.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# The technology is the molecule(s) BEFORE "with"/"plus"; everything after is backbone.
_WITH = re.compile(r"\s+(?:with|plus)\s+", re.IGNORECASE)
# Co-technology separators. NOTE: en/em dash (fixed combos like "trifluridine–tipiracil")
# and " and " split; a plain hyphen does NOT, to preserve hyphenated INNs.
_CO = re.compile(r"\s*(?:\+|/|;|–|—|\band\b)\s*", re.IGNORECASE)

_SALTS = re.compile(
    r"\b(hydrochloride|dihydrochloride|hydrobromide|mesylate|maleate|sulfate|sulphate|"
    r"succinate|tartrate|fumarate|sodium|disodium|acetate|phosphate|hydrate)\b",
    re.IGNORECASE,
)
_DOSING = re.compile(r"\b\d+(?:\.\d+)?\s*(?:mg|mcg|g|ml|%)\b", re.IGNORECASE)

# Backbone / non-novel agents dropped from the molecule scope (they recur across many
# appraisals; scoping on them would re-create the blob).
_BACKBONE = frozenset(
    {
        "dexamethasone", "prednisone", "prednisolone", "methylprednisolone",
        "chemotherapy", "platinum", "platinum-based chemotherapy", "fluoropyrimidine",
        "carboplatin", "cisplatin", "oxaliplatin", "paclitaxel", "nab-paclitaxel",
        "docetaxel", "gemcitabine", "capecitabine", "fluorouracil", "pemetrexed",
        "doxorubicin", "cyclophosphamide", "vincristine", "bendamustine",
        "best supportive care", "chemotherapy regimen",
    }
)

# Brand → INN seed map. NICE mostly uses INNs already; openFDA returns brand names, so a
# small static map keeps generic_name matching honest. RxNormResolver extends it at
# ingest time for brands not listed here.
_BRAND2INN: dict[str, str] = {
    "orserdu": "elacestrant",
    "keytruda": "pembrolizumab",
    "opdivo": "nivolumab",
    "tagrisso": "osimertinib",
    "lonsurf": "trifluridine",
    "lenvima": "lenvatinib",
    "tecentriq": "atezolizumab",
    "imfinzi": "durvalumab",
    "venclyxto": "venetoclax",
    "venclexta": "venetoclax",
}


@dataclass(frozen=True)
class DrugIdentity:
    molecules: frozenset[str]  # normalized INN keys (lowercase) under appraisal
    primary: str  # the first / lead molecule (sibling + scope anchor)
    display: str  # the original technology string

    def __bool__(self) -> bool:
        return bool(self.molecules)


def _clean_molecule(token: str) -> str:
    t = token.strip().lower()
    t = _SALTS.sub(" ", t)
    t = _DOSING.sub(" ", t)
    t = re.sub(r"[^a-z0-9 \-]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"[-\s]*based$", "", t).strip()  # "platinum-based" → "platinum"
    return _BRAND2INN.get(t, t)


def normalize_drug(technology: str) -> DrugIdentity:
    """Parse a NICE technology string into its appraised molecule(s)."""
    display = (technology or "").strip()
    head = _WITH.split(display, maxsplit=1)[0] if display else ""
    parts = [_clean_molecule(p) for p in _CO.split(head)] if head else []

    molecules: list[str] = []
    for m in parts:
        if m and m not in _BACKBONE and m not in molecules:
            molecules.append(m)

    if not molecules:  # pure-backbone head, or unparseable → fall back to whole string
        whole = _clean_molecule(display)
        if whole and whole not in molecules:
            molecules.append(whole)

    return DrugIdentity(
        molecules=frozenset(molecules),
        primary=molecules[0] if molecules else "",
        display=display,
    )


def register_brand(brand: str, inn: str) -> None:
    """Merge a resolved brand→INN pair into the runtime map so ``normalize_drug`` (the
    single source of truth) sees it on subsequent calls."""
    b = brand.strip().lower()
    i = inn.strip().lower()
    if b and i:
        _BRAND2INN[b] = i


class RxNormResolver:
    """Resolve a brand/synonym to its primary ingredient (INN) via the NLM RxNav REST
    API. Network-bound; used only at ingestion to extend the static brand map for
    molecules it doesn't already cover. Falls back to the static map / identity so it is
    always safe to call. The HTTP client is injectable for offline tests.
    """

    def __init__(self, client=None) -> None:
        self._client = client  # lazily built httpx client if None

    def _http(self):
        if self._client is None:
            from strata_platform.sources.base import build_client

            self._client = build_client()
        return self._client

    def resolve(self, name: str) -> str:
        """Return the lowercase ingredient name for ``name``. If RxNav has nothing,
        return the static-map / lowercase identity (never raises)."""
        from strata_platform.sources.endpoints import RXNAV_BASE

        key = (name or "").strip().lower()
        if not key:
            return ""
        if key in _BRAND2INN:
            return _BRAND2INN[key]
        try:
            r = self._http().get(
                f"{RXNAV_BASE}/rxcui.json", params={"name": key, "search": 2}
            )
            r.raise_for_status()
            ids = (r.json().get("idGroup", {}) or {}).get("rxnormId") or []
            if not ids:
                return key
            r2 = self._http().get(
                f"{RXNAV_BASE}/rxcui/{ids[0]}/related.json", params={"tty": "IN"}
            )
            r2.raise_for_status()
            groups = (r2.json().get("relatedGroup", {}) or {}).get("conceptGroup") or []
            for g in groups:
                for c in g.get("conceptProperties") or []:
                    inn = (c.get("name") or "").strip().lower()
                    if inn:
                        register_brand(key, inn)
                        return inn
        except Exception:  # noqa: BLE001 - resolution is best-effort, never fatal
            return key
        return key
