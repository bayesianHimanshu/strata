"""HashingEmbedder: deterministic, correct dimensionality, normalized (no network)."""
from __future__ import annotations

import math

from strata_platform.substrate.embeddings import HashingEmbedder, get_embedder


def test_deterministic_and_dim() -> None:
    e = HashingEmbedder(dim=1536)
    a = e.embed_one("overall survival was immature")
    b = e.embed_one("overall survival was immature")
    assert a == b
    assert len(a) == 1536


def test_l2_normalized() -> None:
    e = HashingEmbedder(dim=256)
    v = e.embed_one("icer uncertainty comparator")
    assert abs(math.sqrt(sum(x * x for x in v)) - 1.0) < 1e-9


def test_related_text_more_similar_than_unrelated() -> None:
    e = HashingEmbedder(dim=1536)

    def cos(x, y):
        return sum(a * b for a, b in zip(x, y))

    q = e.embed_one("cost-effectiveness ICER uncertainty")
    near = e.embed_one("the ICER cost-effectiveness estimate was uncertain")
    far = e.embed_one("pembrolizumab dosing schedule infusion")
    assert cos(q, near) > cos(q, far)


def test_get_embedder_defaults_to_hashing_offline() -> None:
    # No Azure endpoint configured in tests -> offline hashing embedder.
    assert isinstance(get_embedder(), HashingEmbedder)
