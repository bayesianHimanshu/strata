"""structure_aware_prefixed chunking (pure, no I/O)."""
from __future__ import annotations

from datetime import date

from strata_platform.substrate.chunking import structure_aware_prefixed_chunks
from strata_platform.substrate.contracts import DocType


def test_breadcrumb_prefix_and_metadata_propagation() -> None:
    text = "Recommendations:\nThe ICER was highly uncertain and OS data immature."
    chunks = structure_aware_prefixed_chunks(
        text, source_id="TA1:guidance", doc_date=date(2025, 1, 1),
        doc_title="Drug X for cancer", doc_type=DocType.ta_final_guidance,
        appraisal_id="TA1", drug="drugx",
    )
    assert chunks
    c = chunks[0]
    assert c.text.startswith("[TA1:guidance › Drug X for cancer › Recommendations]")
    assert "ICER" in c.text
    assert c.doc_type == DocType.ta_final_guidance
    assert c.appraisal_id == "TA1" and c.drug == "drugx"
    assert c.doc_date == date(2025, 1, 1) and c.source_id == "TA1:guidance"


def test_long_section_is_windowed_with_overlap() -> None:
    body = "alpha " * 600  # ~3600 chars, well over max_chars
    chunks = structure_aware_prefixed_chunks(
        body, source_id="s", doc_type=DocType.literature, max_chars=1200, overlap=150
    )
    assert len(chunks) >= 3  # windowed
    # consecutive windows overlap (step = max_chars - overlap)
    assert all("alpha" in c.text for c in chunks)


def test_empty_text_yields_no_chunks() -> None:
    assert structure_aware_prefixed_chunks("   ", source_id="s",
                                           doc_type=DocType.literature) == []
