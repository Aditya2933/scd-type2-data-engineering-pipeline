"""Schema contract and row-level validation.

Each data row becomes either an Event (a candidate change) or a Rejection that
carries a machine-readable reason code and the raw record. Nothing is dropped
silently. Batch-level problems (wrong header) raise SchemaMismatch instead.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .scd2 import Event

CUSTOMER_ID_RE = re.compile(r"^C\d{6}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_NAME_LEN = 100
EXTRA_KEY = "__extra__"  # DictReader restkey: collects fields beyond the header


class SchemaMismatch(Exception):
    """The file header does not match the contract. The whole batch is refused."""


class RowError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Rejection:
    reason: str
    raw: dict[str, Any]


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _jsonable(raw: dict) -> dict[str, Any]:
    return {str(k): v for k, v in raw.items()}


def normalize_timestamp(value: str) -> str:
    """ISO-8601 with an explicit offset, converted to 'YYYY-MM-DDTHH:MM:SSZ'.

    Naive timestamps are rejected because their instant is ambiguous.
    Sub-second precision is rejected so that distinct instants cannot
    collapse into one key after truncation.
    """
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RowError("INVALID_TIMESTAMP") from exc
    if dt.tzinfo is None:
        raise RowError("NAIVE_TIMESTAMP")
    if dt.microsecond:
        raise RowError("INVALID_TIMESTAMP_PRECISION")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_city_lookup(settings: Settings) -> dict[str, str]:
    """Map lowercased city -> canonical spelling from the reference table."""
    with settings.cities_path.open(newline="", encoding="utf-8-sig") as fh:
        return {r["city"].strip().lower(): r["city"].strip() for r in csv.DictReader(fh)}


def check_header(header: list[str], expected: tuple[str, ...]) -> None:
    missing = sorted(set(expected) - set(header))
    unexpected = sorted(set(header) - set(expected))
    duplicated = sorted({h for h in header if header.count(h) > 1})
    if missing or unexpected or duplicated:
        raise SchemaMismatch(
            f"missing={missing} unexpected={unexpected} duplicated={duplicated}"
        )


def validate_row(raw: dict, cities: dict[str, str], settings: Settings) -> Event | Rejection:
    if EXTRA_KEY in raw or any(v is None for v in raw.values()):
        return Rejection("MALFORMED_ROW", _jsonable(raw))
    try:
        cid = _clean(raw["customer_id"])
        if cid is None:
            raise RowError("MISSING_REQUIRED:customer_id")
        if not CUSTOMER_ID_RE.match(cid):
            raise RowError("INVALID_CUSTOMER_ID")

        name = _clean(raw["name"])
        if name is None:
            raise RowError("MISSING_REQUIRED:name")
        if len(name) > MAX_NAME_LEN:
            raise RowError("NAME_TOO_LONG")

        city_text = _clean(raw["city"])
        if city_text is None:
            raise RowError("MISSING_REQUIRED:city")
        city = cities.get(city_text.lower())
        if city is None:
            raise RowError("INVALID_REFERENCE_CITY")

        ts_text = _clean(raw["updated_at"])
        if ts_text is None:
            raise RowError("MISSING_REQUIRED:updated_at")
        ts = normalize_timestamp(ts_text)

        email = _clean(raw["email"])  # optional attribute
        if email is not None:
            email = email.lower()
            if not EMAIL_RE.match(email):
                raise RowError("INVALID_EMAIL")

        tier = _clean(raw["tier"])  # optional attribute
        if tier is not None:
            tier = tier.lower()
            if tier not in settings.tiers:
                raise RowError("INVALID_TIER")
    except RowError as exc:
        return Rejection(exc.reason, _jsonable(raw))
    return Event(customer_id=cid, effective_at=ts, name=name, email=email, city=city, tier=tier)


def read_batch(path: Path, settings: Settings) -> list[tuple[int, dict, Event | Rejection]]:
    """Read and validate one file. Returns (physical_line, raw_row, result) per data row."""
    if path.stat().st_size == 0:
        return []
    cities = load_city_lookup(settings)
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh, restkey=EXTRA_KEY)
        check_header(list(reader.fieldnames or []), settings.expected_columns)
        out = []
        for raw in reader:
            out.append((reader.line_num, raw, validate_row(raw, cities, settings)))
    return out
