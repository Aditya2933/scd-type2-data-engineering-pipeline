"""Pure SCD2 logic: history construction, late arrivals, order independence."""
import random

from scd2_pipeline.scd2 import Event, build_versions
from conftest import FAR


def ev(ts, city="Pune", tier="gold", name="Asha Rao", email="asha@example.com", cid="C000001"):
    return Event(cid, ts, name, email, city, tier)


def test_single_event_is_open_and_current():
    v = build_versions([ev("2026-01-01T00:00:00Z")], FAR)
    assert len(v) == 1
    assert v[0].valid_to == FAR and v[0].is_current == 1


def test_repeated_identical_updates_do_not_create_versions():
    events = [ev("2026-01-01T00:00:00Z"), ev("2026-02-01T00:00:00Z"), ev("2026-03-01T00:00:00Z")]
    assert len(build_versions(events, FAR)) == 1


def test_change_closes_previous_version_at_new_effective_time():
    v = build_versions([ev("2026-01-01T00:00:00Z"), ev("2026-03-01T00:00:00Z", city="Mumbai")], FAR)
    assert [x.city for x in v] == ["Pune", "Mumbai"]
    assert v[0].valid_to == "2026-03-01T00:00:00Z" and v[0].is_current == 0
    assert v[1].valid_from == "2026-03-01T00:00:00Z" and v[1].valid_to == FAR and v[1].is_current == 1


def test_late_arrival_splits_existing_history_and_is_not_overwritten():
    jan = ev("2026-01-01T00:00:00Z")
    mar = ev("2026-03-01T00:00:00Z", city="Mumbai")
    late = ev("2026-02-01T00:00:00Z", tier="silver")  # correction effective in February
    v = build_versions([jan, mar, late], FAR)
    assert [x.valid_from for x in v] == ["2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z", "2026-03-01T00:00:00Z"]
    assert [x.tier for x in v] == ["gold", "silver", "gold"]
    assert v[0].valid_to == "2026-02-01T00:00:00Z"
    assert v[-1].is_current == 1 and v[-1].city == "Mumbai"


def test_late_event_matching_neighbour_is_absorbed():
    jan = ev("2026-01-01T00:00:00Z")
    mar = ev("2026-03-01T00:00:00Z", city="Mumbai")
    late_same_as_jan = ev("2026-02-01T00:00:00Z")
    v = build_versions([jan, mar, late_same_as_jan], FAR)
    assert len(v) == 2 and v[0].valid_to == "2026-03-01T00:00:00Z"


def test_surrogate_key_of_existing_version_survives_late_arrival():
    jan = ev("2026-01-01T00:00:00Z")
    mar = ev("2026-03-01T00:00:00Z", city="Mumbai")
    before = {x.valid_from: x.customer_sk for x in build_versions([jan, mar], FAR)}
    after = {x.valid_from: x.customer_sk for x in build_versions([jan, mar, ev("2026-02-01T00:00:00Z", tier="silver")], FAR)}
    assert before["2026-01-01T00:00:00Z"] == after["2026-01-01T00:00:00Z"]
    assert before["2026-03-01T00:00:00Z"] == after["2026-03-01T00:00:00Z"]


def test_result_is_independent_of_arrival_order():
    events = [
        ev("2026-01-01T00:00:00Z"),
        ev("2026-02-01T00:00:00Z", tier="silver"),
        ev("2026-03-01T00:00:00Z", city="Mumbai"),
        ev("2026-04-01T00:00:00Z", city="Mumbai"),
        ev("2026-05-01T00:00:00Z", tier=None),
    ]
    expected = [tuple(vars(x).values()) for x in build_versions(events, FAR)]
    rng = random.Random(0)
    for _ in range(25):
        shuffled = events[:]
        rng.shuffle(shuffled)
        assert [tuple(vars(x).values()) for x in build_versions(shuffled, FAR)] == expected


def test_validity_ranges_are_contiguous_with_one_open_end():
    events = [ev(f"2026-0{m}-01T00:00:00Z", city=c) for m, c in [(1, "Pune"), (2, "Mumbai"), (4, "Pune")]]
    v = build_versions(events, FAR)
    for a, b in zip(v, v[1:]):
        assert a.valid_to == b.valid_from
        assert a.valid_from < a.valid_to
    assert sum(x.is_current for x in v) == 1 and v[-1].valid_to == FAR


def test_empty_event_list_produces_no_versions():
    assert build_versions([], FAR) == []
