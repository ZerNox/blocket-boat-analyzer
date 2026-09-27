"""Load config.toml from the repo root."""

from __future__ import annotations

import tomllib
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

ROOT = Path(__file__).resolve().parents[2]
DB_DIR = ROOT / "db" / "ads"
CACHE_DIR = ROOT / "cache"


def load(path: Path | None = None) -> dict:
    with open(path or ROOT / "config.toml", "rb") as fh:
        return tomllib.load(fh)


# Blocket's range filters silently drop ads where the seller left the field empty
# (a "max 80 hk" search hides every ad without Motorstorlek), so these are applied locally.
LOCAL_RANGES = {"motor_size": ("motor_size_from", "motor_size_to"), "length": ("length_feet_from", "length_feet_to")}


def local_ranges(search_url: str) -> dict[str, tuple[float | None, float | None]]:
    q = dict(parse_qsl(urlparse(search_url).query))
    out = {}
    for field, (lo, hi) in LOCAL_RANGES.items():
        if lo in q or hi in q:
            out[field] = (float(q[lo]) if lo in q else None, float(q[hi]) if hi in q else None)
    return out


def in_range(value: float | None, rng: tuple[float | None, float | None]) -> bool:
    """Unknown values pass: they are resolved from the ad text later."""
    lo, hi = rng
    return value is None or ((lo is None or value >= lo) and (hi is None or value <= hi))


def expand(p: str) -> Path:
    return Path(p).expanduser()
