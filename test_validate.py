"""Row- and batch-level validation: every reason code, boundary values, schema contract."""
import pytest

from scd2_pipeline.config import Settings
from scd2_pipeline.scd2 import Event
from scd2_pipeline.validate import (
    Rejection, RowError, SchemaMismatch, check_header, load_city_lookup,
    normalize_timestamp, read_batch, validate_row,
)
from conftest import HEADER, ROOT


@pytest.fixture
def cities():
    return load_city_lookup(Settings(reference_dir=ROOT / "reference"))


def raw(**overrides):
    base = {"customer_id": "C000001", "name": "Asha Rao", "email": "Asha@Example.com",
            "city": "pune", "tier": "Gold", "updated_at": "2026-01-01T10:00:00Z"}
    base.update(overrides)
    return base


def test_timestamp_offset_is_normalised_to_utc():
    assert normalize_timestamp("2026-01-01T05:30:00+05:30") == "2026-01-01T00:00:00Z"


def test_naive_timestamp_is_rejected():
    with pytest.raises(RowError) as e:
        normalize_timestamp("2026-01-01T10:00:00")
    assert e.value.reason == "NAIVE_TIMESTAMP"


def test_subsecond_timestamp_is_rejected_not_truncated():
    with pytest.raises(RowError) as e:
        normalize_timestamp("2026-01-01T10:00:00.500Z")
    assert e.value.reason == "INVALID_TIMESTAMP_PRECISION"


def test_unparseable_timestamp_is_rejected():
    with pytest.raises(RowError) as e:
        normalize_timestamp("yesterday")
    assert e.value.reason == "INVALID_TIMESTAMP"


def test_happy_path_canonicalises_city_email_and_tier(cities):
    result = validate_row(raw(), cities, Settings())
    assert isinstance(result, Event)
    assert result.city == "Pune" and result.email == "asha@example.com" and result.tier == "gold"


@pytest.mark.parametrize("overrides, reason", [
    ({"customer_id": ""}, "MISSING_REQUIRED:customer_id"),
    ({"customer_id": "X-1"}, "INVALID_CUSTOMER_ID"),
    ({"name": "   "}, "MISSING_REQUIRED:name"),
    ({"name": "x" * 101}, "NAME_TOO_LONG"),
    ({"city": ""}, "MISSING_REQUIRED:city"),
    ({"city": "Atlantis"}, "INVALID_REFERENCE_CITY"),
    ({"updated_at": ""}, "MISSING_REQUIRED:updated_at"),
    ({"updated_at": "2026-01-01T10:00:00"}, "NAIVE_TIMESTAMP"),
    ({"email": "not-an-email"}, "INVALID_EMAIL"),
    ({"tier": "platinum"}, "INVALID_TIER"),
])
def test_each_rule_produces_its_reason_code(cities, overrides, reason):
    result = validate_row(raw(**overrides), cities, Settings())
    assert isinstance(result, Rejection) and result.reason == reason
    assert result.raw["customer_id"] == overrides.get("customer_id", "C000001")  # raw kept for tracing


def test_optional_attributes_may_be_blank(cities):
    result = validate_row(raw(email="", tier=""), cities, Settings())
    assert isinstance(result, Event) and result.email is None and result.tier is None


def test_extra_field_marks_row_malformed(cities):
    row = raw()
    row["__extra__"] = ["surprise"]
    assert validate_row(row, cities, Settings()).reason == "MALFORMED_ROW"


def test_short_row_is_malformed(cities):
    row = raw()
    row["tier"] = None  # DictReader fills missing trailing fields with None
    assert validate_row(row, cities, Settings()).reason == "MALFORMED_ROW"


def test_header_contract_rejects_extra_and_missing_columns():
    with pytest.raises(SchemaMismatch, match="unexpected=\\['loyalty'\\]"):
        check_header(HEADER + ["loyalty"], tuple(HEADER))
    with pytest.raises(SchemaMismatch, match="missing=\\['tier'\\]"):
        check_header([h for h in HEADER if h != "tier"], tuple(HEADER))


def test_empty_file_yields_empty_batch(tmp_path, settings):
    p = tmp_path / "empty.csv"
    p.write_bytes(b"")
    assert read_batch(p, settings) == []


def test_header_only_file_yields_empty_batch(write_csv, settings):
    assert read_batch(write_csv("header_only.csv", []), settings) == []


def test_bom_prefixed_file_is_read(tmp_path, settings):
    p = tmp_path / "bom.csv"
    p.write_text(",".join(HEADER) + "\nC000001,Asha Rao,,Pune,gold,2026-01-01T10:00:00Z\n", encoding="utf-8-sig")
    rows = read_batch(p, settings)
    assert len(rows) == 1 and isinstance(rows[0][2], Event)


def test_physical_line_numbers_are_preserved(write_csv, settings):
    p = write_csv("lines.csv", [
        ["C000001", "Asha Rao", "", "Pune", "gold", "2026-01-01T10:00:00Z"],
        ["C000002", "", "", "Pune", "gold", "2026-01-01T10:00:00Z"],
    ])
    lines = [(line, getattr(item, "reason", "ok")) for line, _, item in read_batch(p, settings)]
    assert lines == [(2, "ok"), (3, "MISSING_REQUIRED:name")]
