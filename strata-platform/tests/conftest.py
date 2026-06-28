"""Test isolation: keep content-addressed snapshots out of the repo tree.

Source-client tests that exercise the snapshot path write bytes through the local
SnapshotStore. Point that store at a throwaway temp dir BEFORE any platform module reads
settings, so tests never pollute ./_snapshots and never touch the network.
"""
from __future__ import annotations

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="strata-test-snap-")
os.environ.setdefault("LOCAL_BLOB_DIR", os.path.join(_TMP, "snapshots"))
