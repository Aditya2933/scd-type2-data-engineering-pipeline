"""Command-line entry point.

  python -m scd2_pipeline.cli generate  --out data/sample --customers 40 --seed 42
  python -m scd2_pipeline.cli load      --db warehouse/scd2.db <files...>
  python -m scd2_pipeline.cli reconcile --db warehouse/scd2.db --expected-dir data/sample/expected
  python -m scd2_pipeline.cli benchmark --customers 20000 --seed 42
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import store
from .benchmark import run_benchmark
from .config import Settings
from .generate import generate
from .pipeline import FAILED, load_file
from .reconcile import run_checks, write_report


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="scd2")
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="write synthetic inputs and labels")
    g.add_argument("--out", default="data/sample")
    g.add_argument("--customers", type=int, default=40)
    g.add_argument("--seed", type=int, default=42)

    ld = sub.add_parser("load", help="load one or more batch files")
    ld.add_argument("--db", default="warehouse/scd2.db")
    ld.add_argument("--rebuild-mode", choices=["incremental", "full"], default="incremental")
    ld.add_argument("files", nargs="+")

    rc = sub.add_parser("reconcile", help="run invariant and label checks")
    rc.add_argument("--db", default="warehouse/scd2.db")
    rc.add_argument("--expected-dir", default=None)
    rc.add_argument("--report", default="output/reconciliation_report.md")

    bm = sub.add_parser("benchmark", help="compare full vs incremental rebuild")
    bm.add_argument("--customers", type=int, default=20000)
    bm.add_argument("--seed", type=int, default=42)
    bm.add_argument("--workdir", default="warehouse/benchmark")
    bm.add_argument("--out", default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.cmd == "generate":
        counts = generate(args.out, customers=args.customers, seed=args.seed,
                          reference_path=Settings().cities_path)
        print(json.dumps(counts, indent=2))
        return 0

    if args.cmd == "load":
        settings = Settings(db_path=Path(args.db))
        conn = store.connect(settings.db_path)
        failed = False
        try:
            for f in args.files:
                s = load_file(conn, Path(f), settings, rebuild_mode=args.rebuild_mode)
                print(f"{s.source_file}: {s.status} input={s.input_rows} accepted={s.accepted_rows} "
                      f"duplicate={s.duplicate_rows} rejected={s.rejected_rows}"
                      + (f" reason={s.failure_reason}" if s.failure_reason else ""))
                failed = failed or s.status == FAILED
        finally:
            conn.close()
        return 1 if failed else 0

    if args.cmd == "reconcile":
        settings = Settings(db_path=Path(args.db))
        conn = store.connect(settings.db_path)
        try:
            checks = run_checks(conn, settings, Path(args.expected_dir) if args.expected_dir else None)
            write_report(conn, checks, Path(args.report))
        finally:
            conn.close()
        for c in checks:
            print(f"[{'PASS' if c.passed else 'FAIL'}] {c.name}: {c.detail}")
        print(f"report written to {args.report}")
        return 0 if all(c.passed for c in checks) else 1

    if args.cmd == "benchmark":
        text = run_benchmark(args.customers, args.seed, Path(args.workdir), Settings())
        print(text)
        if args.out:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(text + "\n", encoding="utf-8")
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
