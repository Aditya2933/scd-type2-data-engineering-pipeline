import csv
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scd2_pipeline import store  # noqa: E402
from scd2_pipeline.config import Settings  # noqa: E402

HEADER = ["customer_id", "name", "email", "city", "tier", "updated_at"]
FAR = "9999-12-31T00:00:00Z"


@pytest.fixture
def settings(tmp_path):
    return Settings(db_path=tmp_path / "test.db", reference_dir=ROOT / "reference")


@pytest.fixture
def conn(settings):
    c = store.connect(settings.db_path)
    yield c
    c.close()


@pytest.fixture
def write_csv(tmp_path):
    def _write(name, rows, header=HEADER):
        path = tmp_path / name
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh, lineterminator="\n")
            w.writerow(header)
            w.writerows(rows)
        return path
    return _write
