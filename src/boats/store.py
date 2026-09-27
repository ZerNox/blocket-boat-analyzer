"""Git-backed ad database: one pretty-printed JSON file per ad under db/ads/.

One file per ad keeps git diffs readable (a price change is a one-line diff) and
`git log -p db/ads/<id>.json` is the full history of that ad.

The full page and description live in cache/ (gitignored). The repo is public,
so only structured fields, extracted values and short scrubbed evidence quotes
are committed.
"""

from __future__ import annotations

import gzip
import json
import os
import re
from pathlib import Path

from .config import CACHE_DIR, DB_DIR


def ad_path(ad_id: int | str) -> Path:
    return DB_DIR / f"{ad_id}.json"


def load_ad(ad_id: int | str) -> dict | None:
    p = ad_path(ad_id)
    if not p.exists():
        return None
    return json.loads(p.read_text())


def save_ad(ad: dict) -> None:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    p = ad_path(ad["ad_id"])
    text = json.dumps(ad, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    if not p.exists() or p.read_text() != text:
        tmp = p.with_suffix(".tmp")
        tmp.write_text(text)
        os.replace(tmp, p)


def patch_ad(ad_id: int | str, **fields) -> dict:
    """Re-read the ad and set only these keys, so concurrent fetch/extract runs don't clobber each other."""
    ad = load_ad(ad_id) or {"ad_id": int(ad_id)}
    ad.update(fields)
    save_ad(ad)
    return ad


def all_ads() -> list[dict]:
    if not DB_DIR.exists():
        return []
    return [json.loads(p.read_text()) for p in sorted(DB_DIR.glob("*.json"))]


# --- local cache of the original ad (never committed) ---


def page_path(ad_id: int | str) -> Path:
    return CACHE_DIR / "pages" / f"{ad_id}.html.gz"


def save_page(ad_id: int | str, html: str) -> None:
    p = page_path(ad_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(gzip.compress(html.encode()))


def load_page(ad_id: int | str) -> str | None:
    p = page_path(ad_id)
    return gzip.decompress(p.read_bytes()).decode() if p.exists() else None


_PHONE = re.compile(r"(?:\+46|0)[\d\s-]{6,14}\d")
_EMAIL = re.compile(r"\S+@\S+\.\w+")


def scrub(text: str | None, limit: int = 180) -> str | None:
    """Short, contact-free quote safe to commit to a public repo."""
    if not text:
        return text
    text = _EMAIL.sub("[e-post]", _PHONE.sub("[tel]", text))
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"
