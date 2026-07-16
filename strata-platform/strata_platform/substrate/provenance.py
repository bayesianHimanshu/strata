"""Provenance: content-addressed snapshots + an append-only ledger.

Every document the platform ingests is snapshotted by SHA-256 (immutable, attributable
- the ALCOA+ property). Locally this writes to a directory; in Azure it writes to Blob
Storage. The ledger row is persisted via the DB layer (see db.models.SourceRecordRow).
"""
from __future__ import annotations

from pathlib import Path

from strata_platform.config import get_settings
from strata_platform.substrate.contracts import DocType, SourceRecord


class SnapshotStore:
    """Content-addressed bytes store. Idempotent: same content => same key, no rewrite."""

    def __init__(self) -> None:
        self._s = get_settings()
        self._blob = None
        if self._s.blob_connection_string:
            from azure.storage.blob import BlobServiceClient  # lazy import

            self._blob = BlobServiceClient.from_connection_string(
                self._s.blob_connection_string
            ).get_container_client(self._s.blob_container)
            try:
                self._blob.create_container()
            except Exception:  # noqa: BLE001 - already exists
                pass
        else:
            Path(self._s.local_blob_dir).mkdir(parents=True, exist_ok=True)

    def put(self, content: bytes) -> str:
        key = SourceRecord.hash_content(content)
        if self._blob is not None:
            client = self._blob.get_blob_client(key)
            if not client.exists():
                client.upload_blob(content)
        else:
            p = Path(self._s.local_blob_dir) / key
            if not p.exists():
                p.write_bytes(content)
        return key

    def get(self, key: str) -> bytes:
        if self._blob is not None:
            return self._blob.get_blob_client(key).download_blob().readall()
        return (Path(self._s.local_blob_dir) / key).read_bytes()

    def exists(self, key: str) -> bool:
        if self._blob is not None:
            return self._blob.get_blob_client(key).exists()
        return (Path(self._s.local_blob_dir) / key).exists()


def snapshot(content: bytes, *, source: str, source_id: str,
             doc_type: DocType | None = None,
             url: str | None = None, **meta) -> SourceRecord:
    """Snapshot bytes and return a SourceRecord. Verifying the digest on read is the
    caller's responsibility (db layer enforces it)."""
    key = SnapshotStore().put(content)
    return SourceRecord(source=source, source_id=source_id, url=url,
                        doc_type=doc_type, content_sha256=key, **meta)
