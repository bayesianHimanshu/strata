"""Context connectors: public-source fetchers + a generic user-supplied-content adder.

Each connector returns ``ContextRecord``s — a content-addressed ``SourceRecord`` plus the
``Chunk``s extracted from it, tagged with drug / doc_date / doc_type / source_id. Public
connectors wrap the hardened source clients (don't re-derive). The ``GenericConnector`` is
the heart of "add external context in real time": a direct add of a URL, pasted text, or an
uploaded file — the URL path is SSRF-guarded and DEFAULT-CLOSED.
"""
from __future__ import annotations

import ipaddress
import socket
from abc import ABC, abstractmethod
from datetime import date
from urllib.parse import urlparse

from pydantic import BaseModel

from strata_platform.config import get_settings
from strata_platform.ingest.corpus import (
    abstract_to_doc,
    clean_query,
    doc_to_chunks,
    dossier_to_doc,
    fetch_literature,
    label_to_doc,
    primary_generic,
    trial_to_doc,
)
from strata_platform.substrate.chunking import structure_aware_prefixed_chunks
from strata_platform.substrate.contracts import Chunk, DocType, SourceRecord
from strata_platform.substrate.provenance import snapshot


class ContextQuery(BaseModel):
    drug: str
    indication: str | None = None
    as_of: date
    max_per_source: int = 25


class ContextRecord(BaseModel):
    source_record: SourceRecord          # snapshotted, provenance-bearing
    chunks: list[Chunk]


class ContextConnector(ABC):
    key: str

    @abstractmethod
    def fetch(self, q: ContextQuery) -> list[ContextRecord]: ...


def _record_from_doc(doc) -> ContextRecord | None:
    """Snapshot a RetrievableDoc's content + chunk it into a ContextRecord."""
    if not doc.text.strip() or doc.doc_date is None:
        return None
    rec = snapshot(doc.content, source=doc.source, source_id=doc.source_id,
                   doc_type=doc.doc_type, url=doc.url, doc_date=doc.doc_date,
                   appraisal_id=doc.appraisal_id, drug=doc.drug)
    return ContextRecord(source_record=rec, chunks=doc_to_chunks(doc))


# --------------------------------------------------------------------------- #
# Public connectors (live fetch from the hardened source clients)
# --------------------------------------------------------------------------- #

class ClinicalTrialsConnector(ContextConnector):
    key = "clinicaltrials"

    def __init__(self, client=None) -> None:
        from strata_platform.sources.clinicaltrials import ClinicalTrialsClient
        self._ct = ClinicalTrialsClient(client=client) if client else ClinicalTrialsClient()

    def fetch(self, q: ContextQuery) -> list[ContextRecord]:
        trials, _, _ = self._ct.search(condition=clean_query(q.indication or ""),
                                       intervention=clean_query(q.drug), status=None,
                                       page_size=q.max_per_source)
        tag = {"drug": q.drug, "indication": q.indication}
        recs = [_record_from_doc(trial_to_doc(t, **tag)) for t in trials]
        return [r for r in recs if r]


class PubMedConnector(ContextConnector):
    key = "pubmed"

    def __init__(self, client=None) -> None:
        from strata_platform.sources.pubmed import PubMedClient
        self._pm = PubMedClient(client=client) if client else PubMedClient()

    def fetch(self, q: ContextQuery) -> list[ContextRecord]:
        abstracts = fetch_literature(q.drug, q.indication or "", pubmed=self._pm,
                                     max_abstracts=q.max_per_source)
        tag = {"drug": q.drug, "indication": q.indication}
        recs = [_record_from_doc(abstract_to_doc(a, **tag)) for a in abstracts]
        return [r for r in recs if r]


class OpenFDAConnector(ContextConnector):
    key = "openfda"

    def __init__(self, client=None) -> None:
        from strata_platform.sources.openfda import OpenFDAClient
        self._fda = OpenFDAClient(client=client) if client else OpenFDAClient()

    def fetch(self, q: ContextQuery) -> list[ContextRecord]:
        labels, _ = self._fda.fetch_label_docs(
            f'openfda.generic_name:"{primary_generic(q.drug)}"')
        recs = [_record_from_doc(label_to_doc(lab, drug=q.drug, indication=q.indication))
                for lab in labels]
        return [r for r in recs if r]


class NICEConnector(ContextConnector):
    """NICE live-horizon connector: index → per-TA crawl.

    Downloads the cancer-recommendations index xlsx, takes the recent TA slice (FY within
    two years of ``as_of``), keeps the TAs whose technology shares the query molecule, and
    fetches each one's per-TA guidance page (exact date + committee rationale). Each becomes
    a dossier ``ContextRecord`` tagged with the molecule. In **live** mode these are
    admitted as horizon evidence; in **backtest** mode the boundary excludes a decision's
    own dossier. The index bytes and the guidance client are injectable for offline tests;
    the default path hits the network (storyblok asset host + per-TA pages with WAF retry).
    """
    key = "nice"

    def __init__(self, *, index_bytes: bytes | None = None, guidance_client=None,
                 xlsx_url_override: str | None = None) -> None:
        self._index_bytes = index_bytes
        self._guidance = guidance_client
        self._xlsx_url = xlsx_url_override

    def _download_index(self) -> bytes:  # pragma: no cover - network
        from strata_platform.sources.nice import NICEClient
        url = self._xlsx_url or get_settings().context_nice_xlsx_url
        xbytes, _ = NICEClient().download_index(xlsx_url_override=url)
        return xbytes

    def fetch(self, q: ContextQuery) -> list[ContextRecord]:
        from strata_platform.sources.drug_identity import normalize_drug
        from strata_platform.sources.nice_guidance import NICEGuidanceClient
        from strata_platform.sources.nice_index import recent_cancer_tas

        xbytes = self._index_bytes if self._index_bytes is not None else self._download_index()
        target = normalize_drug(q.drug).molecules
        refs = recent_cancer_tas(xbytes, cutoff=q.as_of)
        matched = [r for r in refs
                   if normalize_drug(r.technology).molecules & target]
        matched.sort(key=lambda r: r.fiscal_year_start, reverse=True)
        matched = matched[: q.max_per_source]

        guidance = self._guidance or NICEGuidanceClient()
        records: list[ContextRecord] = []
        for r in matched:
            try:
                gr = guidance.fetch(r.ta_id)
            except Exception:  # noqa: BLE001 - one dead TA must not abort the crawl
                continue
            if gr.status != "ok" or gr.parsed is None:
                continue
            di = normalize_drug(r.technology)
            doc = dossier_to_doc(gr, drug=di.primary or q.drug, indication=r.indication)
            rec = _record_from_doc(doc) if doc is not None else None
            if rec is not None:
                records.append(rec)
        return records


PUBLIC_CONNECTORS: dict[str, type[ContextConnector]] = {
    "clinicaltrials": ClinicalTrialsConnector,
    "pubmed": PubMedConnector,
    "openfda": OpenFDAConnector,
    "nice": NICEConnector,
}


# --------------------------------------------------------------------------- #
# Generic connector — add a URL / pasted text / uploaded file as context
# --------------------------------------------------------------------------- #

class UrlNotAllowed(ValueError):
    """Raised when a user-supplied URL fails the SSRF allowlist / safety checks."""


def _assert_url_allowed(url: str) -> None:
    """SSRF guard, DEFAULT-CLOSED: http(s) only, host must match the configured domain
    allowlist (suffix match), and must resolve only to public IPs (blocks private,
    loopback, link-local, and the cloud metadata address 169.254.169.254)."""
    s = get_settings()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UrlNotAllowed(f"scheme not allowed: {parsed.scheme!r} (http/https only)")
    host = parsed.hostname or ""
    if not host:
        raise UrlNotAllowed("no host in URL")
    allow = [d.lower().lstrip(".") for d in s.context_url_allowlist]
    if not allow:
        raise UrlNotAllowed("URL fetch is default-closed; no domains are allowlisted")
    hl = host.lower()
    if not any(hl == d or hl.endswith("." + d) for d in allow):
        raise UrlNotAllowed(f"host {host!r} is not on the allowlist")
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise UrlNotAllowed(f"cannot resolve host {host!r}: {exc}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast):
            raise UrlNotAllowed(f"host {host!r} resolves to non-public IP {ip}")


def _html_to_text(html: str) -> str:
    import re
    from html import unescape
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.I | re.S)
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html))).strip()


def _extract_file_text(data: bytes, filename: str | None) -> str:
    name = (filename or "").lower()
    if name.endswith(".txt") or not name:
        return data.decode("utf-8", "replace")
    if name.endswith(".pdf"):
        try:
            import io

            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            return "\n".join((p.extract_text() or "") for p in reader.pages)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"could not read PDF ({exc}); install pypdf") from exc
    if name.endswith(".docx"):
        try:
            import io

            import docx
            d = docx.Document(io.BytesIO(data))
            return "\n".join(p.text for p in d.paragraphs)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"could not read DOCX ({exc}); install python-docx") from exc
    raise ValueError(f"unsupported file type: {filename!r} (txt/pdf/docx only)")


class GenericConnector:
    """Add user-supplied content as leakage-tagged, provenance-bearing context."""

    key = "generic"

    def add(self, *, kind: str, value: str, drug: str, indication: str | None,
            doc_date: date, filename: str | None = None,
            url_fetcher=None) -> ContextRecord:
        if kind == "url":
            _assert_url_allowed(value)
            text, src_url, source_id = self._fetch_url(value, url_fetcher)
        elif kind == "text":
            text = value
            src_url, source_id = None, f"external:text:{_sha(value)[:12]}"
        elif kind == "file":
            data = value.encode("utf-8") if isinstance(value, str) else value
            if len(data) > get_settings().context_max_file_bytes:
                raise ValueError("file exceeds the configured size cap")
            text = _extract_file_text(data, filename)
            src_url, source_id = None, f"external:file:{filename or _sha(text)[:12]}"
        else:
            raise ValueError(f"unknown kind {kind!r} (url|text|file)")

        if not text.strip():
            raise ValueError("no extractable text in the supplied content")
        rec = snapshot(text.encode("utf-8"), source="external", source_id=source_id,
                       doc_type=DocType.external, url=src_url, doc_date=doc_date,
                       drug=drug)
        chunks = structure_aware_prefixed_chunks(
            text, source_id=source_id, doc_date=doc_date,
            doc_title=filename or "external context", doc_type=DocType.external, drug=drug)
        return ContextRecord(source_record=rec, chunks=chunks)

    def _fetch_url(self, url: str, url_fetcher) -> tuple[str, str, str]:
        if url_fetcher is not None:        # injectable for tests
            body = url_fetcher(url)
        else:  # pragma: no cover - network
            import httpx
            s = get_settings()
            r = httpx.get(url, timeout=30.0, follow_redirects=True)
            r.raise_for_status()
            body = r.content[: s.context_max_url_bytes].decode("utf-8", "replace")
        return _html_to_text(body), url, f"external:url:{_sha(url)[:12]}"


def _sha(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
