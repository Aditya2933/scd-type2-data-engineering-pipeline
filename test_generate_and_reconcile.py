"""Generator determinism and end-to-end reconciliation against separate labels."""
from scd2_pipeline import store
from scd2_pipeline.generate import DEFECT_REASONS, generate
from scd2_pipeline.pipeline import load_file
from scd2_pipeline.reconcile import run_checks


def test_generator_is_byte_for_byte_deterministic(tmp_path, settings):
    generate(tmp_path / "a", customers=30, seed=42, reference_path=settings.cities_path)
    generate(tmp_path / "b", customers=30, seed=42, reference_path=settings.cities_path)
    for rel in ["input/customers_batch_01.csv", "input/customers_batch_02.csv",
                "expected/labels_batch_01.csv", "expected/labels_batch_02.csv"]:
        assert (tmp_path / "a" / rel).read_bytes() == (tmp_path / "b" / rel).read_bytes()


def test_generated_sample_loads_and_reconciles_with_labels(tmp_path, settings):
    generate(tmp_path / "data", customers=300, seed=7, reference_path=settings.cities_path)
    conn = store.connect(tmp_path / "e2e.db")
    for f in sorted((tmp_path / "data" / "input").glob("*.csv")):
        assert load_file(conn, f, settings).status == "SUCCEEDED"
    checks = run_checks(conn, settings, tmp_path / "data" / "expected")
    failed = [c for c in checks if not c.passed]
    assert not failed, [f"{c.name}: {c.detail}" for c in failed]
    conn.close()


def test_generated_data_exercises_every_defect_type(tmp_path, settings):
    generate(tmp_path / "data", customers=300, seed=7, reference_path=settings.cities_path)
    conn = store.connect(tmp_path / "e2e.db")
    for f in sorted((tmp_path / "data" / "input").glob("*.csv")):
        load_file(conn, f, settings)
    reasons = {r[0] for r in conn.execute("SELECT DISTINCT reason FROM quarantine")}
    assert set(DEFECT_REASONS.values()) <= reasons
    assert "CONFLICTING_DUPLICATE_EVENT" in reasons
    conn.close()
