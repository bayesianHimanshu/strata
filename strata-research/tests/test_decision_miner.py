"""DecisionMiner gold path + isolation invariant (#5)."""
from __future__ import annotations

import ast
import inspect
from datetime import date

from agents import decision_miner as dm_mod
from agents.decision_miner import DecisionMiner, LexiconExtractor, split_sentences
from core.contracts import Decision, VulnCategory

RATIONALE = (
    "The committee concluded that the overall survival data were immature. "
    "The chosen comparator did not reflect NHS clinical practice. "
    "No health-related quality of life evidence was presented. "
    "The ICER was therefore highly uncertain and above the range normally "
    "considered cost effective."
)


def _decision() -> Decision:
    return Decision(
        agency="NICE",
        decision_id="TA1042",
        decision_date=date(2026, 3, 1),
        indication="drug for cancer",
        outcome="not_recommended",
        rationale_raw=RATIONALE,
        appraisal_id="TA1042",
    )


def test_lexicon_extractor_maps_to_taxonomy() -> None:
    cats = {c for c, _ in LexiconExtractor().extract(RATIONALE)}
    assert VulnCategory.surrogate_endpoint_immaturity in cats
    assert VulnCategory.comparator in cats
    assert VulnCategory.missing_pro in cats
    assert VulnCategory.icer_uncertainty in cats
    assert VulnCategory.other not in cats  # never emitted by cue


def test_evidence_spans_are_verbatim_sentences() -> None:
    spans = dict(LexiconExtractor().extract(RATIONALE))
    assert "immature" in spans[VulnCategory.surrogate_endpoint_immaturity]
    # the span is a substring of the rationale (verbatim committee text)
    assert spans[VulnCategory.comparator] in RATIONALE


def test_miner_builds_golditems_one_per_category() -> None:
    gold = DecisionMiner().mine(_decision())
    assert all(g.decision_id == "TA1042" for g in gold)
    assert all(g.annotator == "miner:lexicon" for g in gold)
    cats = [g.category for g in gold]
    assert len(cats) == len(set(cats))  # one per category


def test_no_cue_no_golditem() -> None:
    d = _decision().model_copy(update={"rationale_raw": "The drug was approved."})
    assert DecisionMiner().mine(d) == []


def test_split_sentences() -> None:
    assert split_sentences("A immature. B comparator; C") == [
        "A immature.",
        "B comparator;",
        "C",
    ]


def test_isolation_miner_does_not_import_synthesizer() -> None:
    # Invariant #5: the gold path shares no code/state with the prediction path.
    # Check actual import statements (not prose), so the docstring may still explain
    # the invariant without tripping it.
    tree = ast.parse(inspect.getsource(dm_mod))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert not any("synthesizer" in m for m in imported)
    # and no synthesizer symbol leaked into the miner module namespace
    assert not [name for name in vars(dm_mod) if "synthesizer" in name.lower()]
