"""Public-source endpoints and HTTP constants. No I/O at import time.

These are the validated endpoints from the STRATA research repo; the per-source
fixes (Akamai TLS on CT.gov, 404-as-zero on openFDA, the NICE asset host vs page) live
in the individual client modules.
"""
from __future__ import annotations

USER_AGENT = "STRATA/0.1 (regulated-evidence platform; integrated-evidence-generation)"
# A real browser UA for hosts behind a WAF (NICE guidance pages, escalation path).
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
TIMEOUT_SECONDS = 30.0

CTGOV_BASE = "https://clinicaltrials.gov/api/v2"
PUBMED_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
OPENFDA_BASE = "https://api.fda.gov"
RXNAV_BASE = "https://rxnav.nlm.nih.gov/REST"
NICE_BASE = "https://www.nice.org.uk"
NICE_CANCER_PAGE = (
    "https://www.nice.org.uk/what-nice-does/our-guidance/about-technology-appraisal-"
    "guidance/technology-appraisal-data-cancer-appraisal-recommendations"
)
