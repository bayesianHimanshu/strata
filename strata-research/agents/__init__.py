"""STRATA agents: small, single-responsibility, Pydantic-typed I/O.

The orchestrator wires these with explicit state and deterministic control flow. The
gold path (decision_miner) and the prediction path (synthesizer) share NO state
(invariant #5): they do not import each other, and the only thing they have in common
is the read-only pre-registered rubric.
"""
