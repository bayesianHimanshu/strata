"""normalize_drug on the real NICE technology strings (the single normalizer)."""
from __future__ import annotations

from strata_platform.sources.drug_identity import RxNormResolver, normalize_drug


def _mols(s: str) -> set[str]:
    return set(normalize_drug(s).molecules)


def test_single_molecule() -> None:
    di = normalize_drug("Pembrolizumab")
    assert di.molecules == frozenset({"pembrolizumab"})
    assert di.primary == "pembrolizumab"


def test_with_clause_drops_backbone() -> None:
    assert _mols("Cabozantinib with nivolumab") == {"cabozantinib"}
    assert _mols("Selinexor with bortezomib and dexamethasone") == {"selinexor"}
    assert _mols(
        "Pembrolizumab with platinum- and fluoropyrimidine-based chemotherapy"
    ) == {"pembrolizumab"}
    assert _mols("Belantamab mafodotin with pomalidomide and dexamethasone") == {
        "belantamab mafodotin"
    }


def test_fixed_combination_splits_on_dash_and_plus() -> None:
    assert _mols("Trifluridine-tipiracil with bevacizumab") == {
        "trifluridine",
        "tipiracil",
    }
    assert _mols("Encorafenib with binimetinib") == {"encorafenib"}


def test_multiword_inn_preserved() -> None:
    assert _mols("Trastuzumab deruxtecan") == {"trastuzumab deruxtecan"}


def test_brand_maps_to_inn() -> None:
    assert normalize_drug("Orserdu").primary == "elacestrant"


def test_siblings_share_primary_across_different_combinations() -> None:
    a = normalize_drug("Belantamab mafodotin with pomalidomide and dexamethasone")
    b = normalize_drug("Belantamab mafodotin with bortezomib and dexamethasone")
    assert a.primary == b.primary == "belantamab mafodotin"


def test_empty_is_falsy() -> None:
    assert not normalize_drug("")
    assert normalize_drug("").molecules == frozenset()


# --- RxNorm resolver (injected HTTP, no network) ---------------------------- #


class _FakeResp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeRxNav:
    """Minimal RxNav double: name->rxcui, then rxcui->ingredient."""

    def get(self, url: str, params: dict | None = None):
        if url.endswith("/rxcui.json"):
            return _FakeResp({"idGroup": {"rxnormId": ["999"]}})
        return _FakeResp(
            {"relatedGroup": {"conceptGroup": [
                {"tty": "IN", "conceptProperties": [{"name": "Faketinib"}]}]}}
        )


def test_rxnorm_resolver_uses_static_map_without_network() -> None:
    # A brand already in the static map resolves with no HTTP call.
    r = RxNormResolver(client=object())  # would explode if .get were called
    assert r.resolve("Keytruda") == "pembrolizumab"


def test_rxnorm_resolver_queries_and_registers() -> None:
    r = RxNormResolver(client=_FakeRxNav())
    assert r.resolve("Novelbrand") == "faketinib"
    # Resolution is merged back so normalize_drug (single source of truth) now knows it.
    assert normalize_drug("Novelbrand").primary == "faketinib"
