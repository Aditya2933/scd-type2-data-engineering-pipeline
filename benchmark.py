"""Performance measurement: full dimension rebuild vs incremental rebuild.

Both modes must produce byte-identical dimension content. The benchmark checks
this with a digest, so the optimisation is only credited if it preserves results.
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

from . import store
from .config import Settings
from .generate import generate
from .pipeline import load_file


def _dim_digest(conn) -> str:
    h = hashlib.sha256()
    for r in conn.execute("SELECT customer_sk, row_hash, valid_from, valid_to, is_current "
                          "FROM dim_customer ORDER BY customer_sk"):
        h.update("|".join(str(x) for x in tuple(r)).encode("utf-8") + b"\n")
    return h.hexdigest()[:16]


def run_benchmark(customers: int, seed: int, workdir: Path, settings: Settings) -> str:
    data_dir = workdir / "data"
    generate(data_dir, customers=customers, seed=seed, reference_path=settings.cities_path)
    files = sorted((data_dir / "input").glob("customers_batch_*.csv"))
    total_rows = 0
    for f in files:
        with f.open(encoding="utf-8") as fh:
            total_rows += max(0, sum(1 for _ in fh) - 1)  # minus header

    lines = [
        f"customers={customers} seed={seed} input_files={len(files)} input_rows={total_rows}",
        f"{'mode':<12}{'seconds':>10}{'dim_rows':>10}{'accepted':>10}  digest",
    ]
    digests = {}
    for mode in ("full", "incremental"):
        db = workdir / f"bench_{mode}.db"
        if db.exists():
            db.unlink()
        conn = store.connect(db)
        accepted = 0
        t0 = time.perf_counter()
        for f in files:
            accepted += load_file(conn, f, settings, rebuild_mode=mode).accepted_rows
        elapsed = time.perf_counter() - t0
        dim_rows = conn.execute("SELECT COUNT(*) FROM dim_customer").fetchone()[0]
        digests[mode] = _dim_digest(conn)
        conn.close()
        lines.append(f"{mode:<12}{elapsed:>10.2f}{dim_rows:>10}{accepted:>10}  {digests[mode]}")

    lines.append("dimension_identical_across_modes=" + str(digests["full"] == digests["incremental"]))
    return "\n".join(lines)
