"""Central configuration. One place for cutoffs, thresholds, and source URLs.

Importing this module must not perform any I/O. These are constants the rest of
the system reads; tests pin the invariant-bearing ones.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import httpx

# --------------------------------------------------------------------------- #
# Temporal controls (invariants #2 and #4)
# --------------------------------------------------------------------------- #

# The training cutoff of the model that will actually run the synthesizer.
# Decisions published strictly AFTER this date cannot be in parametric memory and
# form the leakage-clean Arm A test set. Keep in lockstep with the deployed model.
MODEL_CUTOFF = date(2026, 2, 1)

# Invariant #2 buffer. For a decision D, the retrieval corpus admits only documents
# with doc_date < D.decision_date - LEAKAGE_BUFFER. The buffer is a *conservative*
# margin: HTA evidence packages are often released within weeks of the decision,
# and many of our source dates normalize to day-1 of a month (see normalize_date),
# so a sub-month buffer would let same-cycle evidence leak in. 90 days is the PoV
# default; it is a registered research parameter, overridable per run on the filter.
LEAKAGE_BUFFER = timedelta(days=90)

# Minimum post-cutoff negative/restricted oncology decisions for the clean Arm A
# arm to be self-sufficient (else supplement with G-BA + closed-book control).
CLEAN_ARM_MIN_NEGATIVE = 12

# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #

# Content-addressed snapshot root. Reproducibility is an output (invariant #7).
SNAPSHOT_DIR = Path("snapshots")

# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

USER_AGENT = "STRATA/0.1 (research; integrated-evidence-generation)"
TIMEOUT = httpx.Timeout(30.0)

# --------------------------------------------------------------------------- #
# Source endpoints
# --------------------------------------------------------------------------- #

CTGOV_BASE = "https://clinicaltrials.gov/api/v2"
PUBMED_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
OPENFDA_BASE = "https://api.fda.gov"
NICE_CANCER_PAGE = (
    "https://www.nice.org.uk/what-nice-does/our-guidance/about-technology-appraisal-"
    "guidance/technology-appraisal-data-cancer-appraisal-recommendations"
)
NICE_BASE = "https://www.nice.org.uk"
GBA_BASE = "https://www.g-ba.de"
