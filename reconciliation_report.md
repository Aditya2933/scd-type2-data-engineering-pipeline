# Reconciliation report

## Checks

| Check | Result | Detail |
|---|---|---|
| dim_primary_key_unique | PASS | 0 duplicate (customer_id, valid_from) pairs |
| exactly_one_current_row_per_customer | PASS | 0 customers violate |
| validity_ranges_contiguous_ordered | PASS | all customers contiguous |
| current_row_matches_latest_event | PASS | 0 stale current rows, 0 customers without a current row |
| referential_integrity | PASS | 0 dimension keys without events, 0 cities outside reference |
| key_aggregate_customers | PASS | dimension=40 distinct customers, events=40 |
| run_count_accounting | PASS | runs unbalanced=0, accepted 154 vs stored events 154, rejected 21 vs quarantine rows 21 |
| labelled_files_loaded | PASS | 2 label files, unloaded inputs: none |
| labelled_outcomes_match | PASS | 178 labelled lines, 0 mismatches |

## Runs

| Source file | Status | Input | Accepted | Duplicate | Rejected | Failure |
|---|---|---|---|---|---|---|
| customers_batch_01.csv | SUCCEEDED | 102 | 92 | 1 | 9 |  |
| customers_batch_01.csv | SKIPPED_ALREADY_LOADED | 0 | 0 | 0 | 0 |  |
| customers_batch_02.csv | SUCCEEDED | 76 | 62 | 2 | 12 |  |
| customers_batch_02.csv | SKIPPED_ALREADY_LOADED | 0 | 0 | 0 | 0 |  |

## Quarantine by reason

| Reason | Rows |
|---|---|
| CONFLICTING_DUPLICATE_EVENT | 2 |
| INVALID_CUSTOMER_ID | 2 |
| INVALID_EMAIL | 2 |
| INVALID_REFERENCE_CITY | 2 |
| INVALID_TIER | 2 |
| INVALID_TIMESTAMP_PRECISION | 5 |
| MALFORMED_ROW | 2 |
| MISSING_REQUIRED:name | 2 |
| NAIVE_TIMESTAMP | 2 |
