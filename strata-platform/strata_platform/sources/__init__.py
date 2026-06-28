"""Typed public-data source clients.

Each client returns typed records plus a content-addressed ``SourceRecord`` (provenance).
Pure parsers are unit-tested against committed fixtures; nothing here touches the network
at import time. The hard-won, Phase-0-hardened fixes live in the individual modules:
CT.gov via curl_cffi (Akamai TLS), openFDA 404-as-zero + query by generic_name, PubMed
soft-HEOR + recency fallback + NCBI key, NICE index + per-TA guidance pages, and the
single ``normalize_drug`` (with optional RxNorm resolution) for molecule identity.
"""
from __future__ import annotations

from strata_platform.sources.clinicaltrials import ClinicalTrialsClient, TrialRecord, parse_study
from strata_platform.sources.drug_identity import (
    DrugIdentity,
    RxNormResolver,
    normalize_drug,
)
from strata_platform.sources.nice import NICEClient, classify_recommendation, parse_workbook
from strata_platform.sources.nice_guidance import NICEGuidanceClient, parse_guidance
from strata_platform.sources.nice_index import NiceTaRef, recent_cancer_tas
from strata_platform.sources.openfda import LabelDoc, OpenFDAClient, parse_label_docs
from strata_platform.sources.pubmed import Abstract, PubMedClient, parse_efetch

__all__ = [
    "Abstract",
    "ClinicalTrialsClient",
    "DrugIdentity",
    "LabelDoc",
    "NICEClient",
    "NICEGuidanceClient",
    "NiceTaRef",
    "OpenFDAClient",
    "PubMedClient",
    "RxNormResolver",
    "TrialRecord",
    "classify_recommendation",
    "normalize_drug",
    "parse_efetch",
    "parse_guidance",
    "parse_label_docs",
    "parse_study",
    "parse_workbook",
    "recent_cancer_tas",
]
