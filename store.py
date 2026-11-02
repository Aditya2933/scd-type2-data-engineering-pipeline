"""SQLite persistence. All SQL lives here; pipeline logic stays readable."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .scd2 import Event, Version

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS etl_runs (
    run_id          TEXT PRIMARY KEY,
    source_file     TEXT NOT NULL,
    source_sha256   TEXT NOT NULL,
    status          TEXT NOT NULL
                    CHECK (status IN ('RUNNING','SUCCEEDED','FAILED','SKIPPED_ALREADY_LOADED')),
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    input_rows      INTEGER NOT NULL DEFAULT 0,
    accepted_rows   INTEGER NOT NULL DEFAULT 0,
    duplicate_rows  INTEGER NOT NULL DEFAULT 0,
    rejected_rows   INTEGER NOT NULL DEFAULT 0,
    failure_reason  TEXT
);
CREATE INDEX IF NOT EXISTS ix_etl_runs_sha ON etl_runs (source_sha256, status);

-- Append-only source of truth. One row per (customer, effective time).
CREATE TABLE IF NOT EXISTS customer_events (
    customer_id   TEXT NOT NULL,
    effective_at  TEXT NOT NULL,
    name          TEXT NOT NULL,
    email         TEXT,
    city          TEXT NOT NULL,
    tier          TEXT,
    row_hash      TEXT NOT NULL,
    run_id        TEXT NOT NULL REFERENCES etl_runs (run_id),
    source_file   TEXT NOT NULL,
    source_line   INTEGER NOT NULL,
    PRIMARY KEY (customer_id, effective_at)
);

-- Every rejected row, traceable to file and line, with the raw record as JSON.
CREATE TABLE IF NOT EXISTS quarantine (
    quarantine_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL REFERENCES etl_runs (run_id),
    source_file   TEXT NOT NULL,
    line_number   INTEGER NOT NULL,
    reason        TEXT NOT NULL,
    raw_record    TEXT NOT NULL
);

-- Derived SCD2 dimension. Rebuilt for affected customers from customer_events.
CREATE TABLE IF NOT EXISTS dim_customer (
    customer_sk   TEXT PRIMARY KEY,
    customer_id   TEXT NOT NULL,
    name          TEXT NOT NULL,
    email         TEXT,
    city          TEXT NOT NULL,
    tier          TEXT,
    valid_from    TEXT NOT NULL,
    valid_to      TEXT NOT NULL,
    is_current    INTEGER NOT NULL CHECK (is_current IN (0, 1)),
    row_hash      TEXT NOT NULL,
    last_run_id   TEXT NOT NULL,
    UNIQUE (customer_id, valid_from)
);
CREATE INDEX IF NOT EXISTS ix_dim_customer_current ON dim_customer (customer_id, is_current);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)  # explicit BEGIN/COMMIT
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_SQL)
    return conn


def find_successful_run(conn: sqlite3.Connection, sha256: str) -> str | None:
    row = conn.execute(
        "SELECT run_id FROM etl_runs WHERE source_sha256 = ? AND status = 'SUCCEEDED' LIMIT 1",
        (sha256,),
    ).fetchone()
    return row["run_id"] if row else None


def insert_run(conn, run_id: str, source_file: str, sha256: str, started_at: str) -> None:
    conn.execute(
        "INSERT INTO etl_runs (run_id, source_file, source_sha256, status, started_at) "
        "VALUES (?, ?, ?, 'RUNNING', ?)",
        (run_id, source_file, sha256, started_at),
    )


def finish_run(conn, summary, finished_at: str) -> None:
    conn.execute(
        """UPDATE etl_runs SET status = ?, finished_at = ?, input_rows = ?, accepted_rows = ?,
           duplicate_rows = ?, rejected_rows = ?, failure_reason = ?
           WHERE run_id = ?""",
        (
            summary.status,
            finished_at,
            summary.input_rows,
            summary.accepted_rows,
            summary.duplicate_rows,
            summary.rejected_rows,
            summary.failure_reason,
            summary.run_id,
        ),
    )


def stored_event_hash(conn, customer_id: str, effective_at: str) -> str | None:
    row = conn.execute(
        "SELECT row_hash FROM customer_events WHERE customer_id = ? AND effective_at = ?",
        (customer_id, effective_at),
    ).fetchone()
    return row["row_hash"] if row else None


def insert_event(conn, ev: Event, digest: str, run_id: str, source_file: str, line: int) -> None:
    conn.execute(
        """INSERT INTO customer_events
           (customer_id, effective_at, name, email, city, tier, row_hash, run_id, source_file, source_line)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (ev.customer_id, ev.effective_at, ev.name, ev.email, ev.city, ev.tier,
         digest, run_id, source_file, line),
    )


def insert_quarantine(conn, run_id: str, source_file: str, line: int, reason: str, raw: dict) -> None:
    conn.execute(
        "INSERT INTO quarantine (run_id, source_file, line_number, reason, raw_record) VALUES (?, ?, ?, ?, ?)",
        (run_id, source_file, line, reason,
         json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str)),
    )


def events_for_customer(conn, customer_id: str) -> list[Event]:
    rows = conn.execute(
        "SELECT customer_id, effective_at, name, email, city, tier FROM customer_events "
        "WHERE customer_id = ? ORDER BY effective_at",
        (customer_id,),
    ).fetchall()
    return [Event(r["customer_id"], r["effective_at"], r["name"], r["email"], r["city"], r["tier"])
            for r in rows]


def all_customer_ids(conn) -> list[str]:
    return [r["customer_id"] for r in conn.execute(
        "SELECT DISTINCT customer_id FROM customer_events ORDER BY customer_id")]


def rebuild_dimension(conn, customer_ids: list[str], far_future: str, run_id: str) -> None:
    """Replace the dimension rows for the given customers, derived from their event log."""
    from .scd2 import build_versions  # local import keeps module graph simple

    for cid in customer_ids:
        conn.execute("DELETE FROM dim_customer WHERE customer_id = ?", (cid,))
        versions: list[Version] = build_versions(events_for_customer(conn, cid), far_future)
        conn.executemany(
            """INSERT INTO dim_customer
               (customer_sk, customer_id, name, email, city, tier, valid_from, valid_to,
                is_current, row_hash, last_run_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(v.customer_sk, v.customer_id, v.name, v.email, v.city, v.tier, v.valid_from,
              v.valid_to, v.is_current, v.row_hash, run_id) for v in versions],
        )
