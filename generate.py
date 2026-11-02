"""Deterministic synthetic generator for customer change events.

Output layout:
  <out>/input/customers_batch_NN.csv   what the pipeline reads
  <out>/expected/labels_batch_NN.csv   what the pipeline should decide, per line

Labels are kept apart from inputs, so the pipeline never sees its own answers.
The same seed and arguments always produce byte-identical files.

Batch 1 holds events before CUTOFF. Batch 2 holds later events plus late
corrections: events whose effective time is earlier than something batch 1
already loaded.
"""
from __future__ import annotations

import csv
import random
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

HEADER = ["customer_id", "name", "email", "city", "tier", "updated_at"]
LABEL_HEADER = ["source_file", "line_number", "kind", "expected_outcome", "expected_reason"]
BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
CUTOFF = datetime(2026, 7, 1, tzinfo=timezone.utc)
IST = timezone(timedelta(hours=5, minutes=30))

FIRST_NAMES = ["Aarav", "Diya", "Kabir", "Ananya", "Ishaan", "Meera", "Rohan", "Saanvi",
               "Vihaan", "Zoë", "Priya", "Arjun", "Neha", "Tara", "Farhan"]
LAST_NAMES = ["Singh", "Sharma", "Iyer", "Rao", "Khan", "D'Souza", "Patel", "Mehta", "Nair", "Gupta"]
TIERS = ["bronze", "silver", "gold", None]

LATE_RATE = 0.05
DUPLICATE_RATE = 0.03
CONFLICT_RATE = 0.01
DEFECT_RATE = 0.03

DEFECT_REASONS = {
    "missing_name": "MISSING_REQUIRED:name",
    "bad_customer_id": "INVALID_CUSTOMER_ID",
    "invalid_city": "INVALID_REFERENCE_CITY",
    "naive_timestamp": "NAIVE_TIMESTAMP",
    "subsecond_timestamp": "INVALID_TIMESTAMP_PRECISION",
    "bad_tier": "INVALID_TIER",
    "bad_email": "INVALID_EMAIL",
    "extra_field": "MALFORMED_ROW",
}


@dataclass
class _Item:
    batch: int
    customer_id: str
    values: list
    dt: datetime | None
    kind: str
    expected: str = "pending"
    reason: str = ""


def load_city_names(path: Path) -> list[str]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return [row["city"].strip() for row in csv.DictReader(fh)]


def _render_ts(dt: datetime, rng: random.Random) -> str:
    if rng.random() < 0.05:  # boundary: same instant expressed with a local offset
        return dt.astimezone(IST).isoformat()
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _email(name: str, cid: str, version: int) -> str:
    slug = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    slug = slug.replace("'", "").replace(" ", ".")
    return f"{slug}.{cid.lower()}.v{version}@example.com"


def _random_name(rng: random.Random, exclude: str | None = None) -> str:
    while True:
        name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
        if name != exclude:
            return name


def _timeline(rng: random.Random, index: int, cities: list[str]):
    cid = f"C{index:06d}"
    state = {"name": _random_name(rng), "city": rng.choice(cities), "tier": rng.choice(TIERS)}
    state["email"] = None if rng.random() < 0.10 else _email(state["name"], cid, 0)
    t = BASE + timedelta(minutes=rng.randrange(0, 30 * 24 * 60))
    events = [(t, dict(state))]
    for version in range(1, rng.randint(1, 4) + 1):  # every customer has at least one change
        t = t + timedelta(days=rng.randint(45, 120), minutes=rng.randrange(0, 1440))
        which = rng.choice(["city", "tier", "name"])
        if which == "city":
            state["city"] = rng.choice([c for c in cities if c != state["city"]])
        elif which == "tier":
            state["tier"] = rng.choice([x for x in TIERS if x != state["tier"]])
        else:
            state["name"] = _random_name(rng, exclude=state["name"])
            state["email"] = None if rng.random() < 0.10 else _email(state["name"], cid, version)
        events.append((t, dict(state)))
    return cid, events


def _values(cid: str, attrs: dict, ts_text: str) -> list:
    return [cid, attrs["name"], attrs["email"] or "", attrs["city"], attrs["tier"] or "", ts_text]


def _corrupt(defect: str, values: list, dt: datetime | None) -> list:
    v = list(values)
    if defect == "missing_name":
        v[1] = ""
    elif defect == "bad_customer_id":
        v[0] = "X-" + v[0][-4:]
    elif defect == "invalid_city":
        v[3] = "Atlantis"
    elif defect == "naive_timestamp":
        v[5] = v[5].split("+")[0].replace("Z", "")
    elif defect == "subsecond_timestamp":
        v[5] = v[5].replace("Z", ".500Z") if v[5].endswith("Z") else v[5].replace("+", ".500+", 1)
    elif defect == "bad_tier":
        v[4] = "platinum"
    elif defect == "bad_email":
        v[2] = "not-an-email"
    elif defect == "extra_field":
        v.append("unexpected")
    return v


def generate(out_dir, customers: int = 40, seed: int = 42,
             reference_path: Path = Path("reference/cities.csv")) -> dict:
    if customers < 1:
        raise ValueError("customers must be >= 1")
    out = Path(out_dir)
    (out / "input").mkdir(parents=True, exist_ok=True)
    (out / "expected").mkdir(parents=True, exist_ok=True)

    rng = random.Random(seed)
    cities = load_city_names(Path(reference_path))
    items: list[_Item] = []

    # 1. Clean events, plus late corrections that arrive after later history.
    for index in range(1, customers + 1):
        cid, events = _timeline(rng, index, cities)
        for dt, attrs in events:
            batch = 1 if dt < CUTOFF else 2
            items.append(_Item(batch, cid, _values(cid, attrs, _render_ts(dt, rng)), dt, "clean"))
        if len(events) >= 2 and events[1][0] < CUTOFF and rng.random() < LATE_RATE:
            dt0, attrs0 = events[0]
            late_dt = dt0 + timedelta(hours=1)  # earlier than events[1], already loaded
            late_attrs = dict(attrs0, name=_random_name(rng, exclude=attrs0["name"]))
            items.append(_Item(2, cid, _values(cid, late_attrs, _render_ts(late_dt, rng)),
                               late_dt, "late_arrival"))

    # 2. Same-instant conflicts and exact duplicates.
    base = [it for it in items if it.kind in ("clean", "late_arrival")]
    for it in base:
        roll = rng.random()
        if roll < CONFLICT_RATE:
            it.kind, it.expected, it.reason = "conflict_pair", "rejected", "CONFLICTING_DUPLICATE_EVENT"
            other = list(it.values)
            other[1] = _random_name(rng, exclude=it.values[1])
            items.append(_Item(it.batch, it.customer_id, other, it.dt, "conflict_pair",
                               "rejected", "CONFLICTING_DUPLICATE_EVENT"))
        elif roll < CONFLICT_RATE + DUPLICATE_RATE:
            items.append(_Item(it.batch, it.customer_id, list(it.values), it.dt, "exact_duplicate"))

    # 3. Deliberately invalid rows, each with its expected reason code.
    #    Guarantee: every defect type appears at least once in every batch, so
    #    coverage does not depend on the random draw. Random extras go on top.
    candidates = [x for x in items if x.kind in ("clean", "late_arrival")]

    def _add_defect(src: _Item, defect: str) -> None:
        items.append(_Item(src.batch, src.customer_id, _corrupt(defect, src.values, src.dt), None,
                           f"defect:{defect}", "rejected", DEFECT_REASONS[defect]))

    for batch in (1, 2):
        pool = [x for x in candidates if x.batch == batch]
        if not pool:
            continue
        for defect in DEFECT_REASONS:
            _add_defect(rng.choice(pool), defect)
    for it in candidates:
        if rng.random() < DEFECT_RATE:
            _add_defect(it, rng.choice(list(DEFECT_REASONS)))

    # 4. Shuffle within each batch (out-of-order arrival), then label by file position.
    summary: dict[str, dict] = {}
    for batch in (1, 2):
        batch_items = [it for it in items if it.batch == batch]
        rng.shuffle(batch_items)
        seen: set = set()
        for it in batch_items:
            if it.expected != "pending":
                continue
            key = (it.customer_id, it.dt)
            if key in seen:
                it.expected = "duplicate"
            else:
                it.expected = "accepted"
                seen.add(key)

        inp = out / "input" / f"customers_batch_{batch:02d}.csv"
        with inp.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh, lineterminator="\n")
            writer.writerow(HEADER)
            writer.writerows(it.values for it in batch_items)

        lab = out / "expected" / f"labels_batch_{batch:02d}.csv"
        with lab.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh, lineterminator="\n")
            writer.writerow(LABEL_HEADER)
            for line, it in enumerate(batch_items, start=2):  # line 1 is the header
                writer.writerow([inp.name, line, it.kind, it.expected, it.reason])

        summary[f"batch_{batch:02d}"] = dict(Counter(it.expected for it in batch_items))
    return summary
