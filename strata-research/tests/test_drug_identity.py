"""normalize_drug on the real NICE technology strings (Corpus rebuild §1)."""
from __future__ import annotations

from sources.drug_identity import normalize_drug


def _mols(s: str) -> set[str]:
    return set(normalize_drug(s).molecules)


def test_single_molecule() -> None:
    di = normalize_drug("Pembrolizumab")
    assert di.molecules == frozenset({"pembrolizumab"})
    assert di.primary == "pembrolizumab"


def test_with_clause_drops_backbone() -> None:
    # everything after "with" is backbone (steroids / chemo / established agents)
    assert _mols("Cabozantinib with nivolumab") == {"cabozantinib"}
    assert _mols("Selinexor with bortezomib and dexamethasone") == {"selinexor"}
    assert _mols(
        "Pembrolizumab with platinum- and fluoropyrimidine-based chemotherapy"
    ) == {"pembrolizumab"}
    assert _mols("Belantamab mafodotin with pomalidomide and dexamethasone") == {
        "belantamab mafodotin"
    }


def test_fixed_combination_splits_on_dash_and_plus() -> None:
    assert _mols("Trifluridine–tipiracil with bevacizumab") == {
        "trifluridine",
        "tipiracil",
    }
    # binimetinib is after "with" → backbone-side, dropped
    assert _mols("Encorafenib with binimetinib") == {"encorafenib"}


def test_multiword_inn_preserved() -> None:
    # a plain hyphen / a two-word INN must NOT be split
    assert _mols("Trastuzumab deruxtecan") == {"trastuzumab deruxtecan"}


def test_brand_maps_to_inn() -> None:
    assert normalize_drug("Orserdu").primary == "elacestrant"


def test_siblings_share_primary_across_different_combinations() -> None:
    # the raw-string sibling bug: these never matched before; same molecule now does
    a = normalize_drug("Belantamab mafodotin with pomalidomide and dexamethasone")
    b = normalize_drug("Belantamab mafodotin with bortezomib and dexamethasone")
    assert a.primary == b.primary == "belantamab mafodotin"


def test_empty_is_falsy() -> None:
    assert not normalize_drug("")
    assert normalize_drug("").molecules == frozenset()
