# Data dictionary

Naming: `snake_case` for every table and column. Timestamps are ISO-8601 in UTC
with a trailing `Z` and second precision, e.g. `2026-01-07T08:24:00Z`.
Business keys are stable strings. Surrogate keys are deterministic hashes, not
sequences, so reruns produce identical keys.

## Source: customer change file (CSV, UTF-8, header required)

| Column | Type | Null | Rule |
|---|---|---|---|
| `customer_id` | text | no | Business key. Must match `^C\d{6}$`. |
| `name` | text | no | Trimmed, 1-100 characters. Tracked for change. |
| `email` | text | yes | Lowercased. Must look like an address when present. Blank becomes NULL. Tracked. |
| `city` | text | no | Must exist in `reference/cities.csv` (case-insensitive). Stored in canonical spelling. Tracked. |
| `tier` | text | yes | One of `bronze`, `silver`, `gold` (case-insensitive). Blank becomes NULL. Tracked. |
| `updated_at` | timestamp | no | The effective time of the change. Must carry an explicit offset. Second precision only. Normalised to UTC. |

Header contract: exactly these six columns, in any order. Missing, extra, or
duplicated columns fail the whole file (`SCHEMA_MISMATCH`).

Grain: one row is one proposed change of one customer at one instant.

## Reference: `reference/cities.csv`

| Column | Type | Rule |
|---|---|---|
| `city` | text | The allowed city list. A city outside this list is a `INVALID_REFERENCE_CITY` reject. |

## Table `customer_events` (source of truth, append-only)

| Column | Type | Null | Notes |
|---|---|---|---|
| `customer_id` | text | no | Part of primary key. |
| `effective_at` | timestamp | no | Part of primary key. Unique per customer, so the same instant cannot hold two different values. |
| `name`, `email`, `city`, `tier` | text | mixed | Cleaned attribute values. |
| `row_hash` | text | no | SHA-256 over the tracked attributes. Used for change detection. |
| `run_id` | text | no | FK to `etl_runs`. The run that first accepted this event. |
| `source_file`, `source_line` | text, int | no | Exact origin of the row. |

Primary key: `(customer_id, effective_at)`.

## Table `dim_customer` (SCD Type 2, derived)

| Column | Type | Null | Notes |
|---|---|---|---|
| `customer_sk` | text | no | Primary key. `sha256(customer_id|valid_from)[:16]`. Stable: a late event that lands later does not change an existing version's key. |
| `customer_id` | text | no | Business key. |
| `name`, `email`, `city`, `tier` | text | mixed | Attribute values for this version. |
| `valid_from` | timestamp | no | Effective time this version starts. |
| `valid_to` | timestamp | no | Start of the next version, or `9999-12-31T00:00:00Z` for the current one. |
| `is_current` | int | no | 1 for the open-ended version, else 0. Exactly one per customer. |
| `row_hash` | text | no | Hash of the attributes. |
| `last_run_id` | text | no | The run that last rebuilt this customer's rows. |

Unique: `(customer_id, valid_from)`. Validity ranges are contiguous: each
`valid_to` equals the next `valid_from`, and the last one is open.

Derived rules: consecutive events with identical tracked attributes collapse
into one version, so re-asserting a value does not create history.

## Table `etl_runs` (operational log)

| Column | Type | Notes |
|---|---|---|
| `run_id` | text | Primary key. Random per attempt. |
| `source_file`, `source_sha256` | text | File name and content hash. |
| `status` | text | `RUNNING` (inside a transaction only), `SUCCEEDED`, `FAILED`, `SKIPPED_ALREADY_LOADED`. |
| `started_at`, `finished_at` | timestamp | UTC. |
| `input_rows` | int | Data rows read. |
| `accepted_rows` | int | New events stored. |
| `duplicate_rows` | int | Identical copies, or events already stored. |
| `rejected_rows` | int | Rows sent to quarantine. |
| `failure_reason` | text | Set when status is `FAILED`. |

Invariant for `SUCCEEDED` runs: `input_rows = accepted_rows + duplicate_rows + rejected_rows`.

## Table `quarantine` (rejected rows, traceable)

| Column | Type | Notes |
|---|---|---|
| `quarantine_id` | int | Primary key. |
| `run_id` | text | FK to `etl_runs`. |
| `source_file`, `line_number` | text, int | Physical line in the source file, header is line 1. |
| `reason` | text | Reason code (see below). |
| `raw_record` | text (JSON) | The unmodified row as read. |

## Reason codes

| Code | Meaning |
|---|---|
| `SCHEMA_MISMATCH` | File header differs from the contract. The whole file fails. Not per row. |
| `MALFORMED_ROW` | Wrong number of fields. |
| `MISSING_REQUIRED:<field>` | Required field is blank. |
| `INVALID_CUSTOMER_ID` | Does not match `C` followed by six digits. |
| `NAME_TOO_LONG` | Name exceeds 100 characters. |
| `INVALID_REFERENCE_CITY` | City is not in the reference table. |
| `INVALID_TIMESTAMP` | Cannot be parsed as ISO-8601. |
| `NAIVE_TIMESTAMP` | No offset, so the instant is ambiguous. |
| `INVALID_TIMESTAMP_PRECISION` | Has sub-second digits. |
| `INVALID_EMAIL` | Does not look like an address. |
| `INVALID_TIER` | Not in the allowed tier list. |
| `CONFLICTING_DUPLICATE_EVENT` | Same customer and instant appear in this file with different values. Both copies are quarantined. |
| `CONFLICT_WITH_STORED_EVENT` | Same customer and instant already stored with different values. The stored value is kept. |
