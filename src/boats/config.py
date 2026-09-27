"""Load config.toml from the repo root."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB_DIR = ROOT / "db" / "ads"
CACHE_DIR = ROOT / "cache"


def load(path: Path | None = None) -> dict:
    with open(path or ROOT / "config.toml", "rb") as fh:
        return tomllib.load(fh)


def expand(p: str) -> Path:
    return Path(p).expanduser()
