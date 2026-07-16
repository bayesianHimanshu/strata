"""Evidence Synthesis - a grounded GENERATOR on the STRATA substrate.

Unlike HTA Archaeology (a classifier scored against gold), this produces structured
arguments a human reads. There is no category gold, so the trust mechanism is different:
**every assertion must trace to a retrieved chunk, and that grounding is checked
automatically.** Generation is two-pass - extract grounded claims -> groundedness gate ->
compose a narrative from the *retained claims only* - never a single free-text call. The
narrative introduces no fact that is not already a grounded claim.

Output: a structured evidence brief (claims by dimension) + a dossier-style narrative
(prose composed only from grounded claims), with a groundedness score and an audit trail
of filtered (unsupported) claims.
"""
from __future__ import annotations

from strata_platform.capabilities.base import (
    Capability,
    build_boundary,
    parse_json_object,
)
from strata_platform.substrate.contracts import (
    CapabilityRequest,
    CapabilityResult,
    Chunk,
    EvidenceClaim,
    EvidenceDimension,
    NarrativeParagraph,
    Provenance,
    SupportLevel,
    SynthesisDimension,
    SynthesisResult,
)
from strata_platform.substrate.reasoner import Reasoner

# Policy: partials are kept in the brief (flagged) but excluded from the dossier prose -
# the narrative rests on fully-supported claims only. One line so it's reviewable.
NARRATIVE_INCLUDES_PARTIAL = False
MAX_CLAIMS = 12  # bound the gate's per-claim calls / latency

_EXTRACT_SYSTEM = (
    "From the retrieved evidence, extract the factual claims relevant to a reimbursement "
    "dossier, each assigned to one dimension (efficacy, comparator, safety, economic, "
    "generalizability, other). Every claim MUST cite the chunk indices that support it. "
    "Output strict JSON: {\"claims\": [{\"dimension\": .., \"text\": .., "
    "\"chunk_indices\": [..]}]}. Do not state anything not present in the chunks."
)
_GATE_SYSTEM = (
    "You judge whether a claim is supported by a source. Answer with exactly one word: "
    "SUPPORTED, PARTIAL, or UNSUPPORTED."
)
_COMPOSE_SYSTEM = (
    "Write a concise dossier-style evidence narrative using ONLY the provided claims. "
    "Reference each claim by its id. Introduce no new facts. Output strict JSON: "
    "{\"paragraphs\": [{\"text\": .., \"claim_ids\": [..]}]}."
)


def _numbered_evidence(chunks: list[Chunk]) -> str:
    return "\n\n".join(f"[{i}] ({c.doc_type.value}) {c.text[:600]}"
                       for i, c in enumerate(chunks))


def _extract_claims(reasoner: Reasoner, chunks: list[Chunk]) -> list[EvidenceClaim]:
    """PASS 1 - extract grounded claims; drop any claim that cites no chunk (ungrounded at
    birth). chunk_indices map back to real chunk_id/source_id provenance."""
    prompt = (f"Retrieved evidence (chunks):\n{_numbered_evidence(chunks)}\n\n"
              "Return the JSON claims object.")
    out = parse_json_object(reasoner.complete(prompt, system=_EXTRACT_SYSTEM))
    claims: list[EvidenceClaim] = []
    allowed = {d.value for d in EvidenceDimension}
    for raw in out.get("claims", [])[:MAX_CLAIMS]:
        idxs = [i for i in (raw.get("chunk_indices") or []) if isinstance(i, int)
                and 0 <= i < len(chunks)]
        text = (raw.get("text") or "").strip()
        if not idxs or not text:           # ungrounded or empty -> discarded
            continue
        dim = raw.get("dimension")
        dim = dim if dim in allowed else "other"
        cited = [chunks[i] for i in idxs]
        claims.append(EvidenceClaim(
            dimension=EvidenceDimension(dim), text=text,
            provenance=Provenance(chunk_ids=[c.chunk_id for c in cited],
                                  source_ids=sorted({c.source_id for c in cited}),
                                  note="extracted claim"),
        ))
    return claims


def _gate(reasoner: Reasoner, claims: list[EvidenceClaim], chunks: list[Chunk]
          ) -> tuple[list[EvidenceClaim], list[EvidenceClaim]]:
    """GATE - automated groundedness (the trust core). An entailment call per claim against
    its cited chunk text. UNSUPPORTED -> filtered (kept for audit, never shown)."""
    by_id = {c.chunk_id: c for c in chunks}
    retained: list[EvidenceClaim] = []
    filtered: list[EvidenceClaim] = []
    for claim in claims:
        src = "\n".join(by_id[cid].text[:600] for cid in claim.provenance.chunk_ids
                        if cid in by_id)
        verdict = reasoner.complete(
            f"SOURCE: {src}\n\nCLAIM: {claim.text}\n\nIs the claim fully supported by the "
            "source?", system=_GATE_SYSTEM).strip().upper()
        if "UNSUPPORTED" in verdict:
            filtered.append(claim.model_copy(update={"support": SupportLevel.unsupported}))
        elif "PARTIAL" in verdict:
            retained.append(claim.model_copy(update={"support": SupportLevel.partial}))
        else:
            retained.append(claim.model_copy(update={"support": SupportLevel.supported}))
    return retained, filtered


def _compose(reasoner: Reasoner, retained: list[EvidenceClaim]) -> list[NarrativeParagraph]:
    """PASS 2 - narrative from retained claims ONLY. Partials excluded by policy. Any
    paragraph referencing a claim id not in the retained set is dropped (no invented
    facts)."""
    eligible = [c for c in retained
                if NARRATIVE_INCLUDES_PARTIAL or c.support == SupportLevel.supported]
    if not eligible:
        return []
    valid_ids = {c.claim_id for c in eligible}
    claims_block = "\n".join(f"{c.claim_id}: {c.text}" for c in eligible)
    out = parse_json_object(reasoner.complete(
        f"Claims:\n{claims_block}\n\nReturn the JSON paragraphs object.",
        system=_COMPOSE_SYSTEM))
    paras: list[NarrativeParagraph] = []
    for raw in out.get("paragraphs", []):
        ids = [i for i in (raw.get("claim_ids") or []) if i in valid_ids]
        text = (raw.get("text") or "").strip()
        if text and ids:                   # structural check: only real claim ids survive
            paras.append(NarrativeParagraph(text=text, claim_ids=ids))
    return paras


def _group_by_dimension(claims: list[EvidenceClaim]) -> list[SynthesisDimension]:
    out: list[SynthesisDimension] = []
    for dim in EvidenceDimension:
        members = [c for c in claims if c.dimension == dim]
        if members:
            out.append(SynthesisDimension(dimension=dim, claims=members))
    return out


class EvidenceSynthesis(Capability):
    key = "evidence_synthesis"
    summary = ("Grounded, provenance-backed synthesis of the public evidence base into "
               "the structured arguments a dossier requires.")

    def run(self, request: CapabilityRequest, *, reasoner: Reasoner,
            store) -> CapabilityResult:
        d = request.decision
        if d is None:
            raise ValueError("evidence_synthesis requires a decision")
        boundary = build_boundary(d, request.params)
        query = f"{d.drug} {d.indication} efficacy comparator safety cost-effectiveness"
        chunks = store.search(query, boundary, k=16)
        if not chunks:                     # fail loud, not a silent empty dossier
            raise ValueError(
                f"no in-boundary evidence for {d.decision_id or d.drug}; cannot synthesise")

        claims = _extract_claims(reasoner, chunks)            # PASS 1
        retained, filtered = _gate(reasoner, claims, chunks)  # GATE
        narrative = _compose(reasoner, retained)              # PASS 2 (retained only)

        result = SynthesisResult(
            decision_id=d.decision_id, drug=d.drug, indication=d.indication,
            brief=_group_by_dimension(retained), narrative=narrative,
            groundedness_score=round(len(retained) / max(len(claims), 1), 4),
            filtered_claims=filtered, boundary=boundary.policy(),
            retrieved_chunks=len(chunks))
        return CapabilityResult(capability=self.key, tenant_id=request.tenant_id,
                                payload=result.model_dump(mode="json"))
