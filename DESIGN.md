# Design and trade-off note

## The core decision: the event log is the source of truth

The pipeline stores every accepted change in `customer_events` and treats the
SCD2 dimension as a pure function of that log. Each run rebuilds the dimension
rows for the affected customers from their full event history.

Why this rather than updating rows in place:

- **Late arrivals are a normal case here, not an error.** A correction
  effective in February that arrives after March is inserted between the two
  existing versions. Applying an in-place update would require remembering
  arrival order. With an event log, the result depends only on the events, so
  it is the same whatever order files arrived in. A test verifies this
  directly, loading the same two files forward and backward.
- **History is never silently overwritten.** A conflicting value at an already
  stored instant is quarantined, and the stored value stays. The alternative,
  last-write-wins, would make reruns and backfills change history without a trace.
- **Rebuilds are cheap to reason about.** The dimension for one customer is
  deleted and rewritten from its events inside the same transaction. There is
  no partial state to repair.

## Alternatives considered

| Option | Why not chosen |
|---|---|
| Classic MERGE into the dimension (close old row, insert new) | Only handles changes in arrival order. A late event would need a manual split, and the logic gets fragile for repeated late updates. |
| Last-write-wins on conflicts | Silently destroys history and makes reruns non-idempotent in effect. |
| Row-hash deduplication only | Detects that a row changed, but not where it belongs in time. |
| Full dimension rebuild every run | Simplest to get right, and used as the reference mode in the benchmark. Costs more as history grows. |

## Idempotency and recovery

- **One file, one transaction.** All events, quarantine rows, dimension rows,
  and the run record commit together or not at all. The test suite
  injects a crash mid-load and checks that zero events remain.
- **Skip by content hash.** A file whose SHA-256 already has a `SUCCEEDED` run
  is recorded as `SKIPPED_ALREADY_LOADED` and does nothing else. This catches
  re-delivery under a new file name too.
- **Forced reruns still converge.** The primary key `(customer_id, effective_at)`
  means re-inserting an identical event is counted as a duplicate, not stored
  again. The dimension is rebuilt deterministically, so the result is the same.
- **Failed attempts stay visible.** A `FAILED` row is written in its own
  transaction after rollback, with the reason. Retrying the same file is
  allowed, because only `SUCCEEDED` hashes block a reload.

## Timestamp handling

Timestamps are normalised to UTC, so `05:30+05:30` and `00:00Z` are the same
instant and count as one event. Naive timestamps and sub-second precision are
rejected rather than guessed at. Guessing either would silently merge or
misorder events, which is worse than a visible reject.

## Performance

Measured on this machine with 20,000 generated customers (76,181 input rows,
two files), two modes in fresh databases:

| Mode | Seconds | Dimension rows | Digest |
|---|---|---|---|
| full rebuild every file | 4.84 | 70,513 | 5b65a25aa0756416 |
| incremental (affected customers only) | 4.42 | 70,513 | 5b65a25aa0756416 |

The digests match, so both modes produce identical dimension content. The
optimisation is the incremental rebuild: it touches only customers that appear
in the file. The gain on this workload is modest (about 9%), and it grows
with history size. Most of the remaining time is per-row SQL and per-customer
DELETE/INSERT. The next reasonable step is a set-based rebuild using window
functions (`LEAD` over `effective_at` per customer), which would replace the
per-customer loop with one statement per file. That was not built here.

Timings vary by machine. Run `scripts/demo.sh` to reproduce them.

## Known limitations

- **Corrections at an existing instant are quarantined, not applied.** To
  correct a value that was already stored at `effective_at`, the source needs
  to send a new effective time, or a versioned source key. That is a real
  restriction and a design choice. It is the safe default, but a production
  system would need an explicit correction process.
- **Reference changes are not retroactive.** The city reference is checked at
  load time. Changing a city's canonical spelling later does not rewrite
  stored events.
- **Single-writer SQLite.** `BEGIN IMMEDIATE` serialises writers. Fine for a
  local vertical slice, not for concurrent loads.
- **No schema evolution.** An added column fails the batch by design. Adding
  one needs a contract change and a migration.
- **Email and name are compared exactly after trimming and lowercasing email.**
  Name variants like "Asha Rao" and "Asha R." are treated as different values
  and both quarantine when they clash at one instant.
- **Timeliness is not measured.** The pipeline records `updated_at` and the load
  time, but does not alert on late data.

## What I would do next

1. Set-based rebuild with window functions, then measure again on a larger history.
2. A correction channel: a versioned source key, or a `source_version` column,
   so a deliberate correction supersedes an earlier value without silent overwrite.
3. Property-based tests for the SCD2 functions (Hypothesis), beyond the seeded
   shuffle test used here.
4. Run the same pipeline on a warehouse engine, with the dimension published
   as a view over `dim_customer`.
