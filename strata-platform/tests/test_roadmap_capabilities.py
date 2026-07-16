"""Registry completeness - all four capabilities reachable only via the registry."""
from __future__ import annotations

from strata_platform.capabilities.registry import list_capabilities


def test_all_four_capabilities_registered() -> None:
    assert {c["key"] for c in list_capabilities()} == {
        "hta_archaeology", "endpoint_landscape", "evidence_synthesis",
        "safety_surveillance"}
