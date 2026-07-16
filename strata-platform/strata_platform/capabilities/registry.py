"""Capability registry - the lookup the API and worker use to dispatch a request to the
right agent. Adding a capability is one line here."""
from __future__ import annotations

from strata_platform.capabilities.base import Capability
from strata_platform.capabilities.endpoint_landscape import EndpointLandscape
from strata_platform.capabilities.evidence_synthesis import EvidenceSynthesis
from strata_platform.capabilities.hta_archaeology import HTAArchaeology
from strata_platform.capabilities.safety_surveillance import SafetySurveillance

_REGISTRY: dict[str, Capability] = {
    c.key: c for c in (
        HTAArchaeology(),
        EndpointLandscape(),
        EvidenceSynthesis(),
        SafetySurveillance(),
    )
}


def get_capability(key: str) -> Capability:
    if key not in _REGISTRY:
        raise KeyError(f"unknown capability '{key}'")
    return _REGISTRY[key]


def list_capabilities() -> list[dict[str, str]]:
    return [{"key": c.key, "summary": c.summary} for c in _REGISTRY.values()]
