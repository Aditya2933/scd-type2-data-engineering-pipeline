"""Post-load verification: structural invariants, count reconciliation, and labelled outcomes."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from itertools import groupby
from pathlib import Path

from .config import Settings
from .validate import load_city_lookup


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


def run_checks(conn, settings: Settings, expected_dir: Path | None = None) -> list[Check]:
    checks: list[Check] = []

    dup_keys = conn.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM dim_customer GROUP BY customer_id, valid_from HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    checks.append(Check("dim_primary_key_unique", dup_keys == 0, f"{dup_keys} duplicate (customer_id, valid_from) pairs"))

    bad_current = conn.execute(
        "SELECT customer_id FROM dim_customer GROUP BY customer_id HAVING SUM(is_current) != 1"
    ).fetchall()
    checks.append(Check("exactly_one_current_row_per_customer", not bad_current,
                        f"{len(bad_current)} customers violate"))

    problems = []
    rows = conn.execute("SELECT customer_id, valid_from, valid_to FROM dim_customer ORDER BY customer_id, valid_from").fetchall()
    for cid, group in groupby(rows, key=lambda r: r["customer_id"]):
        versions = list(group)
        for a, b in zip(versions, versions[1:]):
            if a["valid_to"] != b["valid_from"]:
                problems.append(f"{cid}: gap or overlap at {a['valid_to']}")
        if versions[-1]["valid_to"] != settings.far_future:
            problems.append(f"{cid}: last version not open-ended")
        for v in versions:
            if v["valid_from"] >= v["valid_to"]:
                problems.append(f"{cid}: empty validity range")
    checks.append(Check("validity_ranges_contiguous_ordered", not problems,
                        f"{len(problems)} problems" if problems else "all customers contiguous"))

    stale = conn.execute("""
        SELECT d.customer_id FROM dim_customer d
        JOIN (SELECT customer_id, MAX(effective_at) AS latest FROM customer_events GROUP BY customer_id) l
          ON l.customer_id = d.customer_id
        JOIN customer_events e ON e.customer_id = d.customer_id AND e.effective_at = l.latest
        WHERE d.is_current = 1 AND d.row_hash != e.row_hash
    """).fetchall()
    missing_current = conn.execute("""
        SELECT COUNT(*) FROM (SELECT DISTINCT customer_id FROM customer_events)
        WHERE customer_id NOT IN (SELECT customer_id FROM dim_customer WHERE is_current = 1)
    """).fetchone()[0]
    checks.append(Check("current_row_matches_latest_event",
                        not stale and missing_current == 0,
                        f"{len(stale)} stale current rows, {missing_current} customers without a current row"))

    orphans = conn.execute(
        "SELECT COUNT(DISTINCT customer_id) FROM dim_customer WHERE customer_id NOT IN (SELECT customer_id FROM customer_events)"
    ).fetchone()[0]
    valid_cities = set(load_city_lookup(settings).values())
    bad_city = sum(1 for r in conn.execute("SELECT DISTINCT city FROM customer_events") if r["city"] not in valid_cities)
    checks.append(Check("referential_integrity", orphans == 0 and bad_city == 0,
                        f"{orphans} dimension keys without events, {bad_city} cities outside reference"))

    dim_keys = conn.execute("SELECT COUNT(DISTINCT customer_id) FROM dim_customer").fetchone()[0]
    event_keys = conn.execute("SELECT COUNT(DISTINCT customer_id) FROM customer_events").fetchone()[0]
    checks.append(Check("key_aggregate_customers", dim_keys == event_keys,
                        f"dimension={dim_keys} distinct customers, events={event_keys}"))

    bad_runs = [r["run_id"] for r in conn.execute(
        "SELECT run_id FROM etl_runs WHERE status = 'SUCCEEDED' "
        "AND input_rows != accepted_rows + duplicate_rows + rejected_rows")]
    accepted_total = conn.execute("SELECT COALESCE(SUM(accepted_rows), 0) FROM etl_runs WHERE status='SUCCEEDED'").fetchone()[0]
    rejected_total = conn.execute("SELECT COALESCE(SUM(rejected_rows), 0) FROM etl_runs WHERE status='SUCCEEDED'").fetchone()[0]
    events_total = conn.execute("SELECT COUNT(*) FROM customer_events").fetchone()[0]
    quarantine_total = conn.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0]
    checks.append(Check(
        "run_count_accounting",
        not bad_runs and accepted_total == events_total and rejected_total == quarantine_total,
        f"runs unbalanced={len(bad_runs)}, accepted {accepted_total} vs stored events {events_total}, "
        f"rejected {rejected_total} vs quarantine rows {quarantine_total}",
    ))

    if expected_dir is not None:
        checks.extend(_label_checks(conn, Path(expected_dir)))
    return checks


def _label_checks(conn, expected_dir: Path) -> list[Check]:
    accepted_lines = {(r["source_file"], r["source_line"]) for r in
                      conn.execute("SELECT source_file, source_line FROM customer_events")}
    quarantined = {(r["source_file"], r["line_number"]): r["reason"] for r in
                   conn.execute("SELECT source_file, line_number, reason FROM quarantine")}
    loaded = {r["source_file"] for r in
              conn.execute("SELECT DISTINCT source_file FROM etl_runs WHERE status = 'SUCCEEDED'")}

    label_files = sorted(expected_dir.glob("labels_*.csv"))
    total = 0
    mismatches: list[str] = []
    unloaded: set[str] = set()
    for path in label_files:
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                total += 1
                source, line = row["source_file"], int(row["line_number"])
                if source not in loaded:
                    unloaded.add(source)
                key = (source, line)
                if key in accepted_lines:
                    actual = "accepted"
                elif key in quarantined:
                    actual = f"rejected:{quarantined[key]}"
                else:
                    actual = "duplicate"
                expected = (f"rejected:{row['expected_reason']}"
                            if row["expected_outcome"] == "rejected" else row["expected_outcome"])
                if actual != expected:
                    mismatches.append(f"{source}:{line} expected={expected} actual={actual}")

    checks = [
        Check("labelled_files_loaded", not unloaded and bool(label_files),
              f"{len(label_files)} label files, unloaded inputs: {sorted(unloaded) or 'none'}"),
        Check("labelled_outcomes_match", total > 0 and not mismatches,
              f"{total} labelled lines, {len(mismatches)} mismatches" + (f": {mismatches[:3]}" if mismatches else "")),
    ]
    return checks


def write_report(conn, checks: list[Check], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Reconciliation report", "", "## Checks", "", "| Check | Result | Detail |", "|---|---|---|"]
    for c in checks:
        lines.append(f"| {c.name} | {'PASS' if c.passed else 'FAIL'} | {c.detail} |")
    lines += ["", "## Runs", "", "| Source file | Status | Input | Accepted | Duplicate | Rejected | Failure |",
              "|---|---|---|---|---|---|---|"]
    for r in conn.execute("SELECT * FROM etl_runs ORDER BY started_at, source_file"):
        lines.append(f"| {r['source_file']} | {r['status']} | {r['input_rows']} | {r['accepted_rows']} | "
                     f"{r['duplicate_rows']} | {r['rejected_rows']} | {r['failure_reason'] or ''} |")
    lines += ["", "## Quarantine by reason", "", "| Reason | Rows |", "|---|---|"]
    for r in conn.execute("SELECT reason, COUNT(*) AS n FROM quarantine GROUP BY reason ORDER BY reason"):
        lines.append(f"| {r['reason']} | {r['n']} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
