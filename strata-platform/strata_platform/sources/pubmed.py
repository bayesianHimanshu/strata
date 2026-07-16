"""PubMed / NCBI E-utilities client (literature surveillance + retrieval corpus).

esearch for ids/counts; efetch for abstracts (the retrieval corpus needs the abstract
text + a publication date for leakage filtering). Parsers are pure and unit-tested
against fixture payloads; the network methods are thin wrappers.

Two hard-won lessons carried over:
  (a) a hard ``AND (cost-effectiveness OR HTA OR …)`` clause returns ZERO for newer
      drugs with no HEOR papers yet - so the HTA/HEOR terms are a *soft boost* and the
      corpus builder relaxes to ``molecule`` alone (recency-capped) on an empty result.
      Here we surface a zero count; swallowing it silently is forbidden.
  (b) an NCBI API key raises throughput from ~3 to ~10 req/s; it is sent as ``api_key``
      when configured.
"""
from __future__ import annotations

import time
from datetime import date
from xml.etree import ElementTree as ET

import httpx
from pydantic import BaseModel

from strata_platform.config import get_settings
from strata_platform.sources.base import build_client
from strata_platform.sources.endpoints import PUBMED_BASE
from strata_platform.substrate.contracts import DocType, SourceRecord
from strata_platform.substrate.provenance import snapshot

_MONTH_ABBR = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun",
         "jul", "aug", "sep", "oct", "nov", "dec"],
        start=1,
    )
}


class SearchResult(BaseModel):
    model_config = {"frozen": True}

    term: str
    count: int | None
    pmids: list[str] = []


class Abstract(BaseModel):
    model_config = {"frozen": True}

    pmid: str
    title: str
    abstract: str
    pub_date: date | None = None


def parse_esearch(term: str, payload: dict) -> SearchResult:
    """Pure parse of an esearch JSON body."""
    res = payload.get("esearchresult", {})
    try:
        count = int(res["count"])
    except (KeyError, ValueError, TypeError):
        count = None
    return SearchResult(term=term, count=count, pmids=list(res.get("idlist") or []))


def _month(raw: str | None) -> int:
    if not raw:
        return 1
    raw = raw.strip().lower()
    if raw[:3] in _MONTH_ABBR:
        return _MONTH_ABBR[raw[:3]]
    try:
        return max(1, min(12, int(raw)))
    except ValueError:
        return 1


def _pub_date(pubdate: ET.Element | None) -> date | None:
    """A PubDate element -> date. Handles Year/Month/Day and MedlineDate ('2024 Mar')."""
    if pubdate is None:
        return None
    year = pubdate.findtext("Year")
    if year and year.isdigit():
        month = _month(pubdate.findtext("Month"))
        day_txt = pubdate.findtext("Day")
        day = int(day_txt) if day_txt and day_txt.isdigit() else 1
        try:
            return date(int(year), month, day)
        except ValueError:
            return None
    medline = (pubdate.findtext("MedlineDate") or "").strip()  # e.g. "2024 Mar-Apr"
    if len(medline) >= 4 and medline[:4].isdigit():
        return date(int(medline[:4]), _month(medline[5:8]) if len(medline) > 4 else 1, 1)
    return None


def parse_efetch(xml: str) -> list[Abstract]:
    """Pure parse of an efetch PubmedArticleSet XML into Abstract records."""
    out: list[Abstract] = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return out
    for art in root.iter("PubmedArticle"):
        pmid = art.findtext("./MedlineCitation/PMID") or ""
        article = art.find("./MedlineCitation/Article")
        if article is None:
            continue
        title = article.findtext("ArticleTitle") or ""
        # AbstractText may be split into labelled sections; join them in order.
        parts = [
            (e.text or "").strip()
            for e in article.findall("./Abstract/AbstractText")
        ]
        abstract = " ".join(p for p in parts if p)
        pubdate = article.find("./Journal/JournalIssue/PubDate")
        out.append(
            Abstract(
                pmid=pmid.strip(),
                title=title.strip(),
                abstract=abstract,
                pub_date=_pub_date(pubdate),
            )
        )
    return out


class PubMedClient:
    def __init__(self, client: httpx.Client | None = None,
                 api_key: str | None = None) -> None:
        self._client = client or build_client()
        # Explicit api_key wins; else fall back to configured NCBI key (Key Vault/.env).
        self._api_key = api_key if api_key is not None else get_settings().ncbi_api_key
        # Respect NCBI rate limits (~3 req/s keyless, ~10 with a key) so a multi-decision
        # ingest does not 429 itself. Throttle client-side with a small margin.
        self._min_interval = 0.12 if self._api_key else 0.40
        self._last = 0.0

    def _throttle(self) -> None:
        wait = self._min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def _with_key(self, params: dict) -> dict:
        if self._api_key:
            params = {**params, "api_key": self._api_key}
        return params

    def search(
        self, term: str, *, retmax: int = 0, sort: str | None = None,
        mindate: str | None = None, maxdate: str | None = None,
    ) -> tuple[SearchResult, SourceRecord]:
        params: dict = {"db": "pubmed", "term": term, "retmode": "json", "retmax": retmax}
        if sort:  # e.g. "pub_date" - newest first, for the recency-capped fallback
            params["sort"] = sort
        if mindate and maxdate:  # EDAT leakage window
            params.update({"datetype": "edat", "mindate": mindate, "maxdate": maxdate})
        self._throttle()
        resp = self._client.get(
            f"{PUBMED_BASE}/esearch.fcgi", params=self._with_key(params)
        )
        resp.raise_for_status()
        result = parse_esearch(term, resp.json())
        rec = snapshot(
            resp.content,
            source="pubmed",
            source_id=f"esearch:{term}",
            doc_type=DocType.literature,
            url=str(resp.url),
        )
        return result, rec

    def fetch_abstracts(
        self, pmids: list[str]
    ) -> tuple[list[Abstract], SourceRecord]:
        """efetch the given PMIDs -> Abstract records + a content-addressed snapshot."""
        if not pmids:
            raise ValueError("fetch_abstracts requires at least one PMID")
        self._throttle()
        resp = self._client.get(
            f"{PUBMED_BASE}/efetch.fcgi",
            params=self._with_key(
                {"db": "pubmed", "id": ",".join(pmids), "retmode": "xml"}
            ),
        )
        resp.raise_for_status()
        abstracts = parse_efetch(resp.text)
        rec = snapshot(
            resp.content,
            source="pubmed",
            source_id=f"efetch:{','.join(pmids)}",
            doc_type=DocType.literature,
            url=str(resp.url),
        )
        return abstracts, rec
