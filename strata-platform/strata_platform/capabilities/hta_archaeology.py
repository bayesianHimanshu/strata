"""HTA Archaeology - the validated capability.

Predicts the evidence-vulnerability categories an HTA committee raised for a decision.
Two modes (the STRATA experiment, now a service):
  - closed_book: parametric prior, no retrieval - ungrounded Vulnerability[].
  - open_book: retrieval under the RetrievalBoundary - grounded Vulnerability[] with
    provenance to the retrieved chunks.
The open−closed contrast is what the eval harness scores.
"""
from __future__ import annotations

from strata_platform.capabilities.base import (
    Capability,
    build_boundary,
    parse_categories,
)
from strata_platform.eval.harness import ground_categories
from strata_platform.sources.drug_identity import normalize_drug
from strata_platform.substrate.contracts import (
    CapabilityRequest,
    CapabilityResult,
    Provenance,
    Vulnerability,
)
from strata_platform.substrate.reasoner import Reasoner

_SYSTEM = (
    "You assess health technology appraisals. Predict which CATEGORIES of evidence "
    "vulnerability the committee cited, strictly within this taxonomy: comparator, "
    "icer_uncertainty, missing_pro, surrogate_endpoint_immaturity, trial_design_bias, "
    "generalizability, budget_impact, other. Output strict JSON: a list of category "
    "ids, nothing else."
)


def _base_prompt(d) -> str:
    """The shared closed/open prompt skeleton - invariant #8: open-book only ADDS the
    retrieved evidence; same model, same prompt skeleton, so the contrast is retrieval."""
    return (f"HTA body: {d.agency}\nTechnology: {d.drug}\n"
            f"Indication: {d.indication}\nReturn the JSON category list.")


def _molecules(drug: str) -> frozenset[str]:
    """Decision molecule scope via the single normalizer (sources.drug_identity)."""
    return normalize_drug(drug or "").molecules


class HTAArchaeology(Capability):
    key = "hta_archaeology"
    summary = "Anticipate the evidence concerns an HTA committee raised (validated)."

    def run(self, request: CapabilityRequest, *, reasoner: Reasoner,
            store) -> CapabilityResult:
        d = request.decision
        if d is None:
            raise ValueError("hta_archaeology requires a decision")
        mode = request.params.get("mode", "open_book")

        if mode == "closed_book":
            cats = parse_categories(reasoner.complete(_base_prompt(d), system=_SYSTEM))
            vulns = [Vulnerability(category=c, grounded=False) for c in cats]
            return CapabilityResult(capability=self.key, tenant_id=request.tenant_id,
                                    vulnerabilities=vulns, payload={"mode": mode})

        # open_book: retrieve under the boundary, predict with the SAME prompt skeleton +
        # the retrieved evidence, then apply the grounding gate - a predicted category is
        # emitted ONLY if a retrieved chunk supports it (the precision mechanism). Every
        # emitted Vulnerability carries the supporting chunk as provenance (invariant #1).
        sibling_ids = frozenset(request.params.get("sibling_ids", ()))
        boundary = build_boundary(d, request.params, sibling_ids=sibling_ids)
        query = f"{d.drug} {d.indication} cost-effectiveness comparator survival endpoint"
        chunks = store.search(query, boundary, k=12)
        evidence = "\n\n".join(f"[{c.source_id}] {c.text[:500]}" for c in chunks)
        prompt = (_base_prompt(d) + "\n\nRetrieved public evidence (predating the "
                  "decision; assert only categories this evidence supports):\n"
                  f"{evidence or '(none)'}")
        predicted = parse_categories(reasoner.complete(prompt, system=_SYSTEM))
        grounded = ground_categories(predicted, chunks)  # category -> supporting chunk

        vulns: list[Vulnerability] = []
        for cat in sorted(grounded, key=lambda c: c.value):
            chunk = grounded[cat]
            prov = Provenance(chunk_ids=[chunk.chunk_id], source_ids=[chunk.source_id],
                              note=f"grounded under boundary {boundary.policy()}")
            vulns.append(Vulnerability(category=cat, grounded=True, provenance=prov))
        return CapabilityResult(
            capability=self.key, tenant_id=request.tenant_id, vulnerabilities=vulns,
            payload={"mode": mode, "boundary": boundary.policy(),
                     "retrieved_chunks": len(chunks),
                     "predicted_pre_grounding": sorted(c.value for c in predicted),
                     "dropped_ungrounded": sorted(
                         c.value for c in predicted if c not in grounded)})
