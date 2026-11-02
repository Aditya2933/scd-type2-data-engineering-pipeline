"""Central configuration. Every path and constant a run depends on lives here."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Sentinel "open" end date for the current version. Lexicographic order of
# ISO-8601 UTC strings matches chronological order, so these compare as text.
FAR_FUTURE = "9999-12-31T00:00:00Z"

EXPECTED_COLUMNS = ("customer_id", "name", "email", "city", "tier", "updated_at")
TIERS = frozenset({"bronze", "silver", "gold"})


@dataclass(frozen=True)
class Settings:
    db_path: Path = Path("warehouse/scd2.db")
    reference_dir: Path = Path("reference")
    far_future: str = FAR_FUTURE
    expected_columns: tuple[str, ...] = EXPECTED_COLUMNS
    tiers: frozenset[str] = TIERS

    @property
    def cities_path(self) -> Path:
        return self.reference_dir / "cities.csv"
