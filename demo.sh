#!/usr/bin/env bash
# End-to-end demo from a clean state: generate, load, rerun, reconcile, benchmark.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH=src
CLI="python -m scd2_pipeline.cli"

rm -rf warehouse output data/sample
$CLI generate --out data/sample --customers 40 --seed 42

echo "== first load =="
$CLI load --db warehouse/scd2.db data/sample/input/customers_batch_01.csv data/sample/input/customers_batch_02.csv

echo "== rerun (expect SKIPPED_ALREADY_LOADED) =="
$CLI load --db warehouse/scd2.db data/sample/input/customers_batch_01.csv data/sample/input/customers_batch_02.csv

echo "== reconcile =="
$CLI reconcile --db warehouse/scd2.db --expected-dir data/sample/expected --report output/reconciliation_report.md

echo "== benchmark =="
$CLI benchmark --customers 20000 --seed 42 --out output/benchmark.txt
