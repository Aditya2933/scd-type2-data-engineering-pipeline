"""Load orchestration: one file = one transaction.

Flow for a file:
  1. Hash the file. If an identical file already SUCCEEDED, record a skip and stop.
  2. Validate every row. Rejects go to quarantine with their line number.
  3. Resolve duplicates inside the batch and against stored events.
  4. Insert accepted events (append-only log).
  5. Rebuild the SCD2 rows for affected customers.
  6. Check that counts reconcile, then commit. Any failure rolls back everything.

Reruns are safe. Identical files are skipped, and even a forced rerun only
hits the unique key and gets counted as duplicates.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import store
from .config import Settings
from .scd2 import Event, row_hash
from .validate import Rejection, SchemaMismatch, read_batch

log = logging.getLogger("scd2.pipeline")

SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
SKIPPED = "SKIPPED_ALREADY_LOADED"
REBUILD_MODES = ("incremental", "full")


@dataclass
class RunSummary:
    run_id: str
    source_file: str
    status: str
    input_rows: int = 0
    accepted_rows: int = 0
    duplicate_rows: int = 0
    rejected_rows: int = 0
    failure_reason: str | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _failure_reason(exc: Exception) -> str:
    if isinstance(exc, SchemaMismatch):
        return f"SCHEMA_MISMATCH: {exc}"
    return f"{type(exc).__name__}: {exc}"


def load_file(conn, path: Path, settings: Settings, rebuild_mode: str = "incremental") -> RunSummary:
    if rebuild_mode not in REBUILD_MODES:
        raise ValueError(f"rebuild_mode must be one of {REBUILD_MODES}")
    path = Path(path)
    sha = sha256_file(path)
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    started = utc_now()
    source = path.name
    log.info("run=%s file=%s sha256=%s status=STARTED", run_id, source, sha[:12])

    try:
        conn.execute("BEGIN IMMEDIATE")
        store.insert_run(conn, run_id, source, sha, started)
        if store.find_successful_run(conn, sha):
            summary = RunSummary(run_id, source, SKIPPED)
        else:
            summary = _apply(conn, path, settings, run_id, source, rebuild_mode)
        store.finish_run(conn, summary, utc_now())
        conn.execute("COMMIT")
    except Exception as exc:  # noqa: BLE001  - any failure must roll back fully
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        reason = _failure_reason(exc)
        log.error("run=%s file=%s status=FAILED reason=%s", run_id, source, reason,
                  exc_info=not isinstance(exc, SchemaMismatch))
        summary = RunSummary(run_id, source, FAILED, failure_reason=reason)
        # Record the failure in its own transaction so the attempt stays visible.
        conn.execute("BEGIN IMMEDIATE")
        store.insert_run(conn, run_id, source, sha, started)
        store.finish_run(conn, summary, utc_now())
        conn.execute("COMMIT")

    log.info(
        "run=%s file=%s status=%s input=%d accepted=%d duplicate=%d rejected=%d",
        summary.run_id, source, summary.status, summary.input_rows,
        summary.accepted_rows, summary.duplicate_rows, summary.rejected_rows,
    )
    return summary


def _apply(conn, path: Path, settings: Settings, run_id: str, source: str, rebuild_mode: str) -> RunSummary:
    rows = read_batch(path, settings)  # SchemaMismatch propagates and fails the run
    summary = RunSummary(run_id, source, SUCCEEDED, input_rows=len(rows))
    if not rows:
        log.info("run=%s file=%s empty batch, nothing to do", run_id, source)
        return summary

    groups: dict[tuple[str, str], list[tuple[int, dict, Event]]] = defaultdict(list)
    for line, raw, item in rows:
        if isinstance(item, Rejection):
            store.insert_quarantine(conn, run_id, source, line, item.reason, item.raw)
            summary.rejected_rows += 1
        else:
            groups[(item.customer_id, item.effective_at)].append((line, raw, item))

    to_insert: list[tuple[int, Event, str]] = []
    for (cid, ts), members in groups.items():
        hashes = {row_hash(item.attributes()) for _, _, item in members}
        if len(hashes) > 1:
            # Same customer and instant, different content. Neither copy is trustworthy.
            for line, raw, _ in members:
                store.insert_quarantine(conn, run_id, source, line, "CONFLICTING_DUPLICATE_EVENT", raw)
            summary.rejected_rows += len(members)
            continue

        (digest,) = hashes
        first_line, _, first_item = members[0]
        stored = store.stored_event_hash(conn, cid, ts)
        if stored is None:
            to_insert.append((first_line, first_item, digest))
            summary.duplicate_rows += len(members) - 1  # extra identical copies
        elif stored == digest:
            summary.duplicate_rows += len(members)      # already loaded earlier
        else:
            for line, raw, _ in members:
                store.insert_quarantine(conn, run_id, source, line, "CONFLICT_WITH_STORED_EVENT", raw)
            summary.rejected_rows += len(members)

    affected: set[str] = set()
    for line, item, digest in to_insert:
        store.insert_event(conn, item, digest, run_id, source, line)
        affected.add(item.customer_id)
    summary.accepted_rows = len(to_insert)

    if summary.accepted_rows + summary.duplicate_rows + summary.rejected_rows != summary.input_rows:
        raise AssertionError("row accounting does not reconcile: accepted + duplicate + rejected != input")

    ids = sorted(affected) if rebuild_mode == "incremental" else store.all_customer_ids(conn)
    store.rebuild_dimension(conn, ids, settings.far_future, run_id)
    return summary
