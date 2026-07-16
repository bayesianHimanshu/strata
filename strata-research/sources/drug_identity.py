from __future__ import annotations

import re
from dataclasses import dataclass

# The technology is the molecule(s) BEFORE "with"/"plus"; everything after is backbone.
_WITH = re.compile(r"\s+(?:with|plus)\s+", re.IGNORECASE)
# Co-technology separators. NOTE: en/em dash (fixed combos like "trifluridine-tipiracil")
# and " and " split; a plain hyphen does NOT, to preserve hyphenated INNs.
_CO = re.compile(r"\s*(?:\+|/|;|-|-|\band\b)\s*", re.IGNORECASE)

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

# Brand -> INN. NICE mostly uses INNs already; openFDA returns brand names, so a small
# map keeps generic_name matching honest (extend as needed).
_BRAND2INN = {
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
    t = re.sub(r"[-\s]*based$", "", t).strip()  # "platinum-based" -> "platinum"
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

    if not molecules:  # pure-backbone head, or unparseable -> fall back to whole string
        whole = _clean_molecule(display)
        if whole and whole not in molecules:
            molecules.append(whole)

    return DrugIdentity(
        molecules=frozenset(molecules),
        primary=molecules[0] if molecules else "",
        display=display,
    )
