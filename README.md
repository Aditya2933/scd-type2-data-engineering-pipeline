# SCD Type 2 customer dimension

A small, tested data pipeline that loads customer change events, validates
them, and maintains a Type 2 slowly changing dimension with effective dates
and a current-row flag. It handles repeated updates, late-arriving corrections,
duplicates, conflicts, invalid references, schema drift, and reruns.

Python 3.12 standard library plus SQLite. The only third-party dependency is
pytest, used for the test suite. Everything runs locally with no credentials.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

bash scripts/demo.sh          # generate, load, rerun, reconcile, benchmark
PYTHONPATH=src pytest -q      # run the test suite
```

Individual commands (run from the repo root, with `PYTHONPATH=src`):

```bash
python -m scd2_pipeline.cli generate  --out data/sample --customers 40 --seed 42
python -m scd2_pipeline.cli load      --db warehouse/scd2.db data/sample/input/customers_batch_01.csv data/sample/input/customers_batch_02.csv
python -m scd2_pipeline.cli reconcile --db warehouse/scd2.db --expected-dir data/sample/expected
python -m scd2_pipeline.cli benchmark --customers 20000 --seed 42
```

`load` exits non-zero if any file fails. `reconcile` exits non-zero if any check fails.

## Repository layout

```
reference/cities.csv              allowed city list (reference data)
src/scd2_pipeline/
  config.py                       paths and contract constants
  validate.py                     header contract, row rules, reason codes
  scd2.py                         pure SCD2 logic (no I/O)
  store.py                        SQLite schema and all SQL
  pipeline.py                     one file = one transaction; run logging
  generate.py                     seeded synthetic data plus separate labels
  reconcile.py                    invariant checks, label comparison, report
  benchmark.py                    full vs incremental rebuild timing
  cli.py                          command-line entry point
tests/                            53 tests (unit, pipeline, end-to-end)
data/sample/input/                generated input batches (committed for review)
data/sample/expected/             expected outcomes per line (kept apart from inputs)
output/                           reconciliation report and benchmark results
docs/DATA_DICTIONARY.md           schemas, naming, reason codes
docs/DESIGN.md                    design, trade-offs, limitations
```

## Architecture

```
 source CSV ──► read + validate ──► rejects ──► quarantine (raw, line-traced)
 (one file)          │
                     ▼
            in-batch dedupe / conflict check
                     │
                     ▼
     stored-event check ──► duplicate / conflict-with-stored (counted or quarantined)
                     │
                     ▼
     insert accepted ──► customer_events   (append-only source of truth)
                     │
                     ▼
     rebuild affected customers ──► dim_customer   (SCD2, derived)
                     │
                     ▼
     accounting check ──► commit, or roll back everything and log FAILED
```

Entities: one customer (`customer_id`) has many change events, each effective
at an instant (`updated_at`). The dimension holds one row per version, with
`valid_from`, `valid_to`, and `is_current`. Full schemas and rules are in
[docs/DATA_DICTIONARY.md](docs/DATA_DICTIONARY.md).

## Assumptions

- `updated_at` is the effective time of the change, not the arrival time.
  That is what makes late arrivals meaningful.
- A customer and an instant identify one event. Two different values at the
  same instant are a conflict, not an update.
- Tracked attributes for change detection: `name`, `email`, `city`, `tier`.
- Timestamps must carry an offset. They are normalised to UTC.
- City must come from `reference/cities.csv`. Rows with unknown cities are rejected.
- Synthetic data only. No real customer data is used or needed.

## Naming conventions

- Tables and columns use `snake_case`. Timestamps are ISO-8601 UTC with a `Z`
  suffix and second precision.
- Business keys are stable strings (`C000001`). Surrogate keys
  (`customer_sk`) are deterministic hashes, not sequences, so reruns produce
  identical keys.
- `run_id` identifies one load attempt. It is random per attempt, which is why
  idempotency rests on the file hash and the primary key rather than on `run_id`.
- `source_file` and `source_line` (physical line, header = 1) trace every
  stored or rejected row back to the input.

## Validation rules and reject handling

Each row is checked in order. The first failing rule decides its reason code,
and the raw record goes to `quarantine` with its file and line. Nothing is
dropped silently.

Batch-level failures are different. A header that does not match the contract
fails the entire file (`SCHEMA_MISMATCH`), and no rows from that file are loaded.

Within a batch, identical duplicates keep the first copy and count the rest
as duplicates. Conflicting duplicates quarantine all copies. A row that
conflicts with an already stored event is quarantined, and the stored value is kept.

Referential integrity is enforced twice: at load time (city must exist in the
reference table) and in reconciliation (every dimension key has events, and
every event city is in the reference).

## Idempotency, retries, restarts, and backfills

- **Transactions.** One file is one transaction. Events, quarantine rows,
  dimension rows, and the run record commit together or not at all.
- **Reruns.** A file with the same SHA-256 as a successful run is recorded as
  `SKIPPED_ALREADY_LOADED` and nothing else changes. This also catches the same
  content delivered under a different name.
- **Retries after failure.** A failed attempt rolls back fully and writes a
  `FAILED` row with the reason. Rerunning the same file is allowed, and it
  succeeds once the cause is fixed.
- **Restart.** Nothing is kept in memory between files. Stopping mid-batch
  leaves the database at the last committed file. Restarting the command picks
  up the remaining files, and the completed ones are skipped.
- **Backfill.** Historical files can be loaded in any order. The dimension
  depends only on the events, so the result is the same whatever the order.
  A test loads two files forward and backward and compares the output.

## Logging and operational metrics

Every run logs its ID, file, hash prefix, status, and counts. Input, accepted,
duplicate, and rejected counts are stored in `etl_runs`, and the rule
`input = accepted + duplicate + rejected` is checked before commit. Start and
end times are recorded in `etl_runs`. Failures carry the exception type and
message, and the full traceback is logged.

## Testing and evidence

The test suite covers:

- **Happy path:** history built correctly, current-row flag set.
- **Empty input:** header-only and zero-byte files.
- **Malformed records:** wrong field count, blank required fields, unparseable timestamps.
- **Duplicates:** exact duplicates within a batch, redelivery across files, conflicting duplicates.
- **Nulls:** blank optional attributes become NULL, and a value that changes to NULL is a real change.
- **Domain edge cases:**
  - Late correction that splits an existing history (`test_late_arrival_splits_existing_history...`).
  - Same instant with a different offset counted as one event (`test_same_instant_with_different_offset...`).
  - Late event identical to a neighbour is absorbed without creating a version.
  - Surrogate keys of existing versions survive a late arrival.
- **Negative scenarios:** schema change, invalid reference key, partial failure
  with rollback and retry, failed attempt visible in the run log.
- **Order independence:** shuffled arrival orders and forward/backward backfill
  produce identical dimensions.
- **Reproducibility:** the generator produces byte-identical output for a given seed.
- **End-to-end:** a generated dataset loads and reconciles against separately
  stored expected outcomes, and every defect type appears in the test data.

Latest run: all 53 tests pass. See the caveat below about how they were run.

Reconciliation on the committed sample (`output/reconciliation_report.md`):
all 9 checks pass, covering 178 labelled lines with 0 mismatches, 154 accepted
events, and 21 quarantined rows, all accounted for.

## Results

| Measure | Value |
|---|---|
| Sample: input rows / batches | 178 rows, 2 batches, 40 customers |
| Sample: accepted / duplicate / rejected | 154 / 3 / 21 |
| Sample: reconciliation checks | 9 of 9 pass |
| Sample: labelled outcomes matching | 178 of 178 |
| Benchmark: 20,000 customers, 76,181 input rows | full 4.84 s vs incremental 4.42 s |
| Benchmark: dimensions identical across modes | yes (digest match) |

See [docs/DESIGN.md](docs/DESIGN.md) for what the benchmark does and doesn't show.

## Sample data and expected answers

`data/sample/input/` holds the inputs the pipeline reads.
`data/sample/expected/` holds one label row per input line: the expected
outcome, reason, and the kind of row (clean, late arrival, exact duplicate,
conflict pair, or a specific defect). Keeping labels in a separate directory means the pipeline never
reads its own answers. Generation is seeded (`--seed 42`) and reproducible.

The generator plants at least one of every defect type in each batch, so
coverage is guaranteed rather than left to chance.

## Known limitations

- A correction at an already stored instant is quarantined rather than applied.
  A real system would need a versioned correction channel (see DESIGN.md).
- The city reference is checked at load time. Later changes to it do not rewrite history.
- SQLite with `BEGIN IMMEDIATE` is single-writer. Suitable for a local vertical slice only.
- An added or renamed source column fails the batch. Schema evolution is not supported.
- Names that differ only slightly are treated as different values, not as the same person.
- Late-data timeliness is not monitored or alerted on.

## Possible next steps

1. Set-based rebuild with window functions to replace the per-customer loop.
2. A correction process: a versioned source key or `source_version` column.
3. Property-based tests for the SCD2 functions.
4. Publish `dim_customer` as a view on a warehouse engine.
5. Scheduled runs with alerting on reject rate and load latency.

## A note on the test runner

The suite is written for pytest, which is declared in `requirements.txt`.
In the environment used to build this repo there was no network access, so
pytest could not be installed. The suite was run through a minimal local
stand-in that supports the fixtures, `raises`, and `parametrize` the tests use.
All 53 tests passed under that runner. Running `PYTHONPATH=src pytest -q` in a
normal environment is the authoritative check, and it may surface differences
that the stand-in does not model.
