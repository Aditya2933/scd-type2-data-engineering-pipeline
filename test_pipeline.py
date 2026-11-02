"""End-to-end pipeline behaviour: idempotency, retries, duplicates, edge cases."""
import pytest

from scd2_pipeline import pipeline, store
from scd2_pipeline.pipeline import load_file
from conftest import FAR


def row(cid="C000001", name="Asha Rao", city="Pune", ts="2026-01-01T00:00:00Z", tier="gold", email="asha@example.com"):
    return [cid, name, email, city, tier, ts]


def dim(conn):
    return [tuple(r) for r in conn.execute(
        "SELECT customer_id, city, tier, valid_from, valid_to, is_current FROM dim_customer ORDER BY customer_id, valid_from")]


def quarantine(conn):
    return [tuple(r) for r in conn.execute("SELECT source_file, line_number, reason FROM quarantine ORDER BY source_file, line_number")]


def event_count(conn):
    return conn.execute("SELECT COUNT(*) FROM customer_events").fetchone()[0]


def test_happy_path_builds_history_and_marks_current(conn, settings, write_csv):
    f = write_csv("b1.csv", [row(ts="2026-01-01T00:00:00Z"), row(city="Mumbai", ts="2026-02-01T00:00:00Z")])
    s = load_file(conn, f, settings)
    assert (s.status, s.accepted_rows, s.rejected_rows, s.duplicate_rows) == ("SUCCEEDED", 2, 0, 0)
    assert dim(conn) == [
        ("C000001", "Pune", "gold", "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z", 0),
        ("C000001", "Mumbai", "gold", "2026-02-01T00:00:00Z", FAR, 1),
    ]


def test_rerun_of_identical_file_is_skipped_and_changes_nothing(conn, settings, write_csv):
    f = write_csv("b1.csv", [row()])
    load_file(conn, f, settings)
    before = dim(conn)
    s = load_file(conn, f, settings)
    assert s.status == "SKIPPED_ALREADY_LOADED"
    assert dim(conn) == before and event_count(conn) == 1


def test_same_content_under_new_filename_is_also_skipped(conn, settings, write_csv, tmp_path):
    f = write_csv("b1.csv", [row()])
    load_file(conn, f, settings)
    copy = tmp_path / "copy_of_b1.csv"
    copy.write_bytes(f.read_bytes())
    assert load_file(conn, copy, settings).status == "SKIPPED_ALREADY_LOADED"


def test_empty_batch_succeeds_with_zero_counts(conn, settings, write_csv):
    s = load_file(conn, write_csv("empty.csv", []), settings)
    assert (s.status, s.input_rows, s.accepted_rows) == ("SUCCEEDED", 0, 0)
    assert event_count(conn) == 0


def test_zero_byte_file_is_an_empty_batch(conn, settings, tmp_path):
    p = tmp_path / "zero.csv"
    p.write_bytes(b"")
    assert load_file(conn, p, settings).status == "SUCCEEDED"


def test_exact_duplicate_within_batch_is_counted_not_stored_twice(conn, settings, write_csv):
    s = load_file(conn, write_csv("b.csv", [row(), row()]), settings)
    assert (s.accepted_rows, s.duplicate_rows, s.rejected_rows) == (1, 1, 0)
    assert event_count(conn) == 1


def test_conflicting_duplicates_are_both_quarantined(conn, settings, write_csv):
    f = write_csv("b.csv", [row(name="Asha Rao"), row(name="Asha R.")])
    s = load_file(conn, f, settings)
    assert (s.accepted_rows, s.rejected_rows) == (0, 2)
    assert {r[2] for r in quarantine(conn)} == {"CONFLICTING_DUPLICATE_EVENT"}


def test_redelivery_of_stored_event_in_new_file_is_duplicate(conn, settings, write_csv):
    load_file(conn, write_csv("day1.csv", [row()]), settings)
    s = load_file(conn, write_csv("day2.csv", [row(), row(cid="C000002")]), settings)
    assert (s.accepted_rows, s.duplicate_rows) == (1, 1)


def test_conflict_with_stored_event_is_quarantined_not_overwritten(conn, settings, write_csv):
    load_file(conn, write_csv("day1.csv", [row(tier="gold")]), settings)
    s = load_file(conn, write_csv("day2.csv", [row(tier="bronze"), row(cid="C000002")]), settings)
    assert s.rejected_rows == 1
    assert quarantine(conn) == [("day2.csv", 2, "CONFLICT_WITH_STORED_EVENT")]
    assert dim(conn)[0][2] == "gold"  # stored history untouched


def test_same_instant_with_different_offset_is_recognised_as_duplicate(conn, settings, write_csv):
    load_file(conn, write_csv("utc.csv", [row(ts="2026-01-01T00:00:00Z")]), settings)
    s = load_file(conn, write_csv("ist.csv", [row(ts="2026-01-01T05:30:00+05:30"), row(cid="C000002")]), settings)
    assert (s.duplicate_rows, s.accepted_rows) == (1, 1)


def test_schema_change_fails_the_whole_batch(conn, settings, write_csv):
    from conftest import HEADER
    f = write_csv("bad.csv", [row() + ["gold_plus"]], header=HEADER + ["loyalty"])
    s = load_file(conn, f, settings)
    assert s.status == "FAILED" and s.failure_reason.startswith("SCHEMA_MISMATCH")
    assert event_count(conn) == 0
    assert conn.execute("SELECT status FROM etl_runs WHERE source_file='bad.csv'").fetchone()[0] == "FAILED"


def test_invalid_reference_key_is_quarantined_with_traceable_raw_record(conn, settings, write_csv):
    s = load_file(conn, write_csv("b.csv", [row(city="Atlantis"), row(cid="C000002")]), settings)
    assert (s.accepted_rows, s.rejected_rows) == (1, 1)
    q = conn.execute("SELECT line_number, reason, raw_record FROM quarantine").fetchone()
    assert q["line_number"] == 2 and q["reason"] == "INVALID_REFERENCE_CITY" and "Atlantis" in q["raw_record"]


def test_partial_failure_rolls_back_everything_then_retry_succeeds(conn, settings, write_csv, monkeypatch):
    f = write_csv("b.csv", [row(), row(cid="C000002")])

    def explode(*args, **kwargs):
        raise RuntimeError("simulated crash after event inserts")

    monkeypatch.setattr(store, "rebuild_dimension", explode)
    s = load_file(conn, f, settings)
    assert s.status == "FAILED" and "simulated crash" in s.failure_reason
    assert event_count(conn) == 0 and dim(conn) == []  # all-or-nothing

    monkeypatch.undo()
    s2 = load_file(conn, f, settings)
    assert s2.status == "SUCCEEDED" and s2.accepted_rows == 2
    assert event_count(conn) == 2


def test_failed_attempt_is_visible_in_run_log(conn, settings, write_csv, monkeypatch):
    f = write_csv("b.csv", [row()])
    monkeypatch.setattr(store, "rebuild_dimension", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    load_file(conn, f, settings)
    statuses = [r[0] for r in conn.execute("SELECT status FROM etl_runs WHERE source_file='b.csv'")]
    assert statuses == ["FAILED"]


def test_late_arriving_correction_splits_history_in_database(conn, settings, write_csv):
    load_file(conn, write_csv("b1.csv", [row(ts="2026-01-01T00:00:00Z"), row(city="Mumbai", ts="2026-03-01T00:00:00Z")]), settings)
    load_file(conn, write_csv("b2.csv", [row(tier="silver", ts="2026-02-01T00:00:00Z")]), settings)
    assert dim(conn) == [
        ("C000001", "Pune", "gold", "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z", 0),
        ("C000001", "Pune", "silver", "2026-02-01T00:00:00Z", "2026-03-01T00:00:00Z", 0),
        ("C000001", "Mumbai", "gold", "2026-03-01T00:00:00Z", FAR, 1),
    ]


def test_backfill_order_does_not_change_the_dimension(tmp_path, write_csv, settings):
    b1 = write_csv("b1.csv", [row(ts="2026-01-01T00:00:00Z"), row(city="Mumbai", ts="2026-03-01T00:00:00Z")])
    b2 = write_csv("b2.csv", [row(tier="silver", ts="2026-02-01T00:00:00Z")])

    forward = store.connect(tmp_path / "fwd.db")
    load_file(forward, b1, settings); load_file(forward, b2, settings)
    backward = store.connect(tmp_path / "bwd.db")
    load_file(backward, b2, settings); load_file(backward, b1, settings)

    assert dim(forward) == dim(backward)
    forward.close(); backward.close()


def test_full_and_incremental_rebuild_modes_agree(tmp_path, write_csv, settings):
    b1 = write_csv("b1.csv", [row(cid=f"C{i:06d}") for i in range(1, 6)])
    b2 = write_csv("b2.csv", [row(cid="C000003", city="Mumbai", ts="2026-03-01T00:00:00Z")])
    results = []
    for mode in ("full", "incremental"):
        c = store.connect(tmp_path / f"{mode}.db")
        load_file(c, b1, settings, rebuild_mode=mode)
        load_file(c, b2, settings, rebuild_mode=mode)
        results.append(dim(c))
        c.close()
    assert results[0] == results[1]


def test_invalid_rebuild_mode_is_rejected(conn, settings, write_csv):
    with pytest.raises(ValueError):
        load_file(conn, write_csv("b.csv", [row()]), settings, rebuild_mode="magic")
