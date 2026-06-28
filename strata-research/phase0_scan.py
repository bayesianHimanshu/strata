"""
STRATA Phase 0 — data-availability scan.

Purpose: de-risk the PoV before any agent is built. Answers one question per source:
"is there enough extractable, date-stamped public evidence to power the three arms?"
and specifically: "how many oncology HTA decisions sit AFTER the model cutoff
(the leakage-clean test set for Arm A)?"

This is a probe, but it is NOT throwaway: the provenance primitives (SourceRecord,
content-addressed snapshots, date normalization) are the Phase 1 foundation.

Run in an open-network environment (the sources below are not reachable from a
locked sandbox):

    pip install -r requirements.txt
    python phase0_scan.py                 # full scan -> feasibility_report.json
    python phase0_scan.py --selftest      # offline checks, no network

Note on ClinicalTrials.gov: it sits behind Akamai Bot Manager, which blocks on the
TLS handshake fingerprint (JA3/JA4) — so a browser User-Agent and a US egress are
not enough. We fetch that one source with curl_cffi impersonating Chrome's TLS
fingerprint. Everything else uses httpx.

No API keys required. Be polite to the endpoints (rate limits are modest).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
from curl_cffi import requests as cffi  # browser-TLS-impersonating client (CT.gov)

# Phase 1 promoted these primitives into core/ and sources/. Phase 0 now imports the
# canonical implementations rather than carrying its own copies (single source of
# truth — an auditability principle). The probes, verdict, and selftest stay here.
from core.config import (
    CLEAN_ARM_MIN_NEGATIVE,
    CTGOV_BASE,
    MODEL_CUTOFF,
    NICE_CANCER_PAGE,
    OPENFDA_BASE,
    PUBMED_BASE,
    TIMEOUT,
)
from core.provenance import normalize_date, snapshot
from sources.nice import classify_recommendation
from sources.nice import find_xlsx_url as _find_xlsx_url
from sources.nice_feasibility import nice_feasibility

REPORT_PATH = Path("feasibility_report.json")


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #


BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT, follow_redirects=True,
                        headers={"User-Agent": BROWSER_UA, "Accept": "application/json"})


def _get_json(client: httpx.Client, url: str, params: dict[str, Any] | None = None,
              retries: int = 4) -> dict[str, Any]:
    for attempt in range(retries):
        r = client.get(url, params=params)
        if r.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return r.json()


# --------------------------------------------------------------------------- #
# Source probes
# --------------------------------------------------------------------------- #

ONCOLOGY_CONDITIONS = ["cancer", "lung cancer", "breast cancer", "prostate cancer",
                       "melanoma", "lymphoma", "multiple myeloma", "leukemia"]


def _ctgov_get_json(session, url: str, params: dict[str, Any] | None = None,
                    retries: int = 4) -> dict[str, Any]:
    for attempt in range(retries):
        r = session.get(url, params=params)
        if r.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return r.json()


def probe_clinicaltrials(_client: httpx.Client) -> dict[str, Any]:
    # Akamai fingerprints the TLS handshake; impersonate Chrome so the request is
    # admitted. The httpx client passed in is intentionally ignored for this source.
    session = cffi.Session(impersonate="chrome", timeout=30)
    version = _ctgov_get_json(session, f"{CTGOV_BASE}/version")
    out: dict[str, Any] = {"api_version": version.get("apiVersion"),
                           "data_timestamp": version.get("dataTimestamp"),
                           "counts_by_condition": {}}
    for cond in ONCOLOGY_CONDITIONS:
        data = _ctgov_get_json(session, f"{CTGOV_BASE}/studies", params={
            "query.cond": cond, "filter.overallStatus": "COMPLETED",
            "pageSize": 1, "countTotal": "true", "format": "json"})
        out["counts_by_condition"][cond] = data.get("totalCount")
    # one sample record to confirm structure / date fields exist
    sample = _ctgov_get_json(session, f"{CTGOV_BASE}/studies", params={
        "query.cond": "lung cancer", "pageSize": 1, "format": "json"})
    studies = sample.get("studies", [])
    out["sample_has_protocol_section"] = bool(
        studies and "protocolSection" in studies[0])
    return out


def probe_pubmed(client: httpx.Client) -> dict[str, Any]:
    def count(term: str) -> int | None:
        data = _get_json(client, f"{PUBMED_BASE}/esearch.fcgi", params={
            "db": "pubmed", "term": term, "retmode": "json", "retmax": 0})
        try:
            return int(data["esearchresult"]["count"])
        except (KeyError, ValueError, TypeError):
            return None
    return {
        "oncology_rwe": count("(neoplasms[mesh]) AND (real-world OR observational)"),
        "oncology_hta": count("(neoplasms[mesh]) AND (cost-effectiveness OR HTA)"),
        "oncology_recent": count("neoplasms[mesh] AND 2026[dp]"),
    }


def _results_total(client, url, params) -> int:
    r = client.get(url, params=params)
    if r.status_code == 404:                       # openFDA: "No matches found"
        return 0
    r.raise_for_status()
    return r.json().get("meta", {}).get("results", {}).get("total", 0)


ONCOLOGY_DRUGS = ["pembrolizumab", "nivolumab", "osimertinib", "trastuzumab",
                  "lenalidomide"]


def probe_openfda(client) -> dict:
    faers = {d: _results_total(client, f"{OPENFDA_BASE}/drug/event.json",
             {"search": f'patient.drug.openfda.generic_name:"{d}"', "limit": 1})
             for d in ONCOLOGY_DRUGS}
    labels = {d: _results_total(client, f"{OPENFDA_BASE}/drug/label.json",
              {"search": f'openfda.generic_name:"{d}"', "limit": 1})
              for d in ONCOLOGY_DRUGS}
    return {"faers_by_drug": faers, "faers_total": sum(faers.values()),
            "labels_by_drug": labels, "labels_total": sum(labels.values())}


# NICE: no clean JSON API. Locate the cancer-recommendations spreadsheet on the
# page and parse it. classify_recommendation / find_xlsx_url now live in
# sources.nice (imported above); the probe keeps its own pre/post-cutoff tallying.

def _looks_like_xlsx(b: bytes) -> bool:
    return b[:4] == b"PK\x03\x04"


def probe_nice(client, xlsx_url_override=None):
    if xlsx_url_override:
        xlsx_url = xlsx_url_override
    else:
        page = client.get(NICE_CANCER_PAGE, headers={"Accept": "text/html"})
        page.raise_for_status()
        xlsx_url = _find_xlsx_url(page.text, NICE_CANCER_PAGE)
        if not xlsx_url:
            return {"error": "no .xlsx link found", "hint": "pass --nice-xlsx-url"}
    resp = client.get(xlsx_url, headers={"Accept": "application/octet-stream"})
    resp.raise_for_status()
    xbytes = resp.content
    rec = snapshot(xbytes, source="nice", source_id="cancer_recommendations",
                   url=xlsx_url)
    return {"xlsx_url": xlsx_url, "snapshot_sha256": rec.content_sha256,
            **nice_feasibility(xbytes, MODEL_CUTOFF)}


# --------------------------------------------------------------------------- #
# Orchestration + verdict
# --------------------------------------------------------------------------- #


def verdict(nice: dict[str, Any]) -> dict[str, Any]:
    post_neg = nice.get("post_cutoff_negative_or_restricted")
    if post_neg is None:
        return {"arm_a_clean": "unknown", "reason": "NICE parse failed"}
    clean_ok = post_neg >= CLEAN_ARM_MIN_NEGATIVE
    return {
        "model_cutoff": MODEL_CUTOFF.isoformat(),
        "clean_arm_min_negative": CLEAN_ARM_MIN_NEGATIVE,
        "post_cutoff_negative_observed": post_neg,
        "arm_a_clean_self_sufficient": clean_ok,
        "recommendation": (
            "Proceed; NICE post-cutoff slice powers the clean Arm A arm."
            if clean_ok else
            "Proceed but supplement: add G-BA/IQWiG and rely on the closed-book "
            "control to compensate for a thin post-cutoff NICE slice."),
    }


def run_scan(nice_xlsx_url: str | None = None) -> dict[str, Any]:
    report: dict[str, Any] = {"generated_at": datetime.now(UTC).isoformat(),
                              "sources": {}}
    with _client() as client:
        probes = [("clinicaltrials", probe_clinicaltrials),
                  ("pubmed", probe_pubmed),
                  ("openfda", probe_openfda),
                  ("nice", lambda c: probe_nice(c, nice_xlsx_url))]
        for name, fn in probes:
            try:
                report["sources"][name] = fn(client)
            except Exception as e:  # noqa: BLE001 - probe must not abort the run
                report["sources"][name] = {"error": f"{type(e).__name__}: {e}"}
    report["verdict"] = verdict(report["sources"].get("nice", {}))
    return report


# --------------------------------------------------------------------------- #
# Offline self-test (runs without network; verifies the invariants)
# --------------------------------------------------------------------------- #


def selftest() -> int:
    failures: list[str] = []

    def check(name: str, cond: bool) -> None:
        if not cond:
            failures.append(name)

    # date normalization
    check("iso", normalize_date("2026-03-14") == date(2026, 3, 14))
    check("iso_ym", normalize_date("2026-03") == date(2026, 3, 1))
    check("long1", normalize_date("January 2024") == date(2024, 1, 1))
    check("long2", normalize_date("15 January 2024") == date(2024, 1, 15))
    check("long3", normalize_date("January 15, 2024") == date(2024, 1, 15))
    check("bad", normalize_date("not a date") is None)
    check("empty", normalize_date(None) is None)

    # recommendation classifier
    check("cls_neg", classify_recommendation("Not recommended") == "not_recommended")
    check("cls_cdf",
          classify_recommendation("Recommended for the Cancer Drugs Fund") == "cdf")
    check("cls_opt", classify_recommendation("Optimised") == "optimised")
    check("cls_ns", classify_recommendation("Non-submission") == "non_submission")
    check("cls_pos", classify_recommendation("Recommended") == "recommended")

    # xlsx magic-byte guard (the bug that fed HTML into openpyxl)
    check("xlsx_magic_ok", _looks_like_xlsx(b"PK\x03\x04zipdata") is True)
    check("xlsx_magic_html", _looks_like_xlsx(b"<!DOCTYPE html>") is False)

    # provenance: content addressing + idempotency + frozen
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        r1 = snapshot(b"hello", source="t", source_id="1", url="u", root=root)
        r2 = snapshot(b"hello", source="t", source_id="1", url="u", root=root)
        check("content_addr", r1.content_sha256 == r2.content_sha256)
        check("snap_written", (root / r1.content_sha256[:2] / r1.content_sha256).exists())
        try:
            r1.source = "x"  # type: ignore[misc]
            check("frozen", False)
        except Exception:
            check("frozen", True)

    # verdict thresholding
    v = verdict({"post_cutoff_negative_or_restricted": CLEAN_ARM_MIN_NEGATIVE})
    check("verdict_ok", v["arm_a_clean_self_sufficient"] is True)
    v2 = verdict({"post_cutoff_negative_or_restricted": 0})
    check("verdict_thin", v2["arm_a_clean_self_sufficient"] is False)

    # xlsx link extraction
    html = '<a href="/media/cancer-recs.xlsx">download</a>'
    check("xlsx_find", _find_xlsx_url(html, NICE_CANCER_PAGE) ==
          "https://www.nice.org.uk/media/cancer-recs.xlsx")

    if failures:
        print("SELFTEST FAILED:", ", ".join(failures))
        return 1
    print("SELFTEST PASSED:", "all invariants hold")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="STRATA Phase 0 data-availability scan")
    ap.add_argument("--selftest", action="store_true",
                    help="run offline invariant checks, no network")
    ap.add_argument("--nice-xlsx-url", default=None,
                    help="direct URL to the NICE cancer-recommendations .xlsx "
                         "(bypasses link sniffing; copy it from the NICE page)")
    ap.add_argument("--out", type=Path, default=REPORT_PATH)
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    report = run_scan(nice_xlsx_url=args.nice_xlsx_url)
    args.out.write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report, indent=2, default=str))
    print(f"\nWrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())