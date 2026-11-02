"""Pure SCD Type 2 logic. No I/O, so it can be tested exhaustively.

Model: the source of truth is an append-only log of customer change events
(`Event`). The dimension is *derived* from that log by `build_versions`.
Because the dimension is a pure function of the event log, the result does
not depend on the order in which files arrived. That is what makes late
arrivals and backfills safe.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Event:
    """One accepted source change, effective at `effective_at` (UTC ISO-8601)."""

    customer_id: str
    effective_at: str
    name: str
    email: str | None
    city: str
    tier: str | None

    def attributes(self) -> tuple:
        return (self.name, self.email, self.city, self.tier)


@dataclass(frozen=True)
class Version:
    customer_sk: str
    customer_id: str
    name: str
    email: str | None
    city: str
    tier: str | None
    valid_from: str
    valid_to: str
    is_current: int
    row_hash: str


def row_hash(attributes: tuple) -> str:
    """Deterministic change-detection hash over tracked attributes."""
    payload = json.dumps(list(attributes), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def surrogate_key(customer_id: str, valid_from: str) -> str:
    """Stable key: depends only on the business key and the version start.

    A late event that lands *after* an existing version's start does not change
    that version's key. Only its `valid_to` moves.
    """
    return hashlib.sha256(f"{customer_id}|{valid_from}".encode("utf-8")).hexdigest()[:16]


def build_versions(events: list[Event], far_future: str) -> list[Version]:
    """Turn one customer's event log into contiguous SCD2 versions.

    1. Sort by effective time (arrival order is irrelevant).
    2. Collapse consecutive events whose tracked attributes are unchanged, so
       a re-asserted value does not create a new version. The earliest event
       keeps the version start.
    3. Each version ends where the next begins. The last one is open-ended and
       flagged current.
    """
    ordered = sorted(events, key=lambda e: e.effective_at)
    kept: list[tuple[str, Event]] = []
    for ev in ordered:
        digest = row_hash(ev.attributes())
        if kept and kept[-1][0] == digest:
            continue
        kept.append((digest, ev))

    versions: list[Version] = []
    last_index = len(kept) - 1
    for i, (digest, ev) in enumerate(kept):
        is_last = i == last_index
        versions.append(
            Version(
                customer_sk=surrogate_key(ev.customer_id, ev.effective_at),
                customer_id=ev.customer_id,
                name=ev.name,
                email=ev.email,
                city=ev.city,
                tier=ev.tier,
                valid_from=ev.effective_at,
                valid_to=far_future if is_last else kept[i + 1][1].effective_at,
                is_current=int(is_last),
                row_hash=digest,
            )
        )
    return versions
