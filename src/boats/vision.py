"""Last resort: read the engine's horsepower off the photos when the ad text doesn't state it.

Runs only for outboard ads whose power is still unknown after the rules and the text LLM.
Two passes with a local vision model (Qwen3-VL on the discrete GPU): pick the photos that show
the engine at low resolution, then read the cowling text on those at high resolution. A power
figure is accepted only if it appears in the text the model says it read ("50 SUZUKI FOUR
STROKE" -> 50). Photos are downloaded on demand and never stored.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re

import httpx

PICK_PROMPT = (
    "Photos 0..{last} from a boat ad, in order. Which photo indices clearly show the outboard engine "
    '(cowling or motor)? JSON: {{"engine_photos": [indices]}}'
)
READ_PROMPT = (
    "Read the text printed on this outboard engine (cowling decals, labels, plates). "
    'JSON: {"text_seen": "exact text you can read", "brand": str|null, "hp": number|null, '
    '"four_stroke": bool|null}. hp only if a horsepower number is visible.'
)


HULL_PROMPT = (
    "Photos 0..{last} from a used-boat ad. Does this boat have a CABIN (an enclosed hytt/cuddy with a roof, "
    "windows and a door or hatch you can sit or sleep in) or is it an OPEN boat (styrpulpet/console, no enclosed "
    'space)? JSON: {{"hull": "cabin"|"open"|"unclear", "photo": index of the clearest photo or null, '
    '"evidence": "what you see, max 15 words"}}'
)


def classify_hull(base: str, urls: list[str], max_photos: int) -> dict:
    """Cabin vs. open boat from the photos, for ads where text and boat type don't settle it."""
    urls = urls[:max_photos]
    res = _ask(base, [_img(u, "640w") for u in urls] + [{"type": "text", "text": HULL_PROMPT.format(last=len(urls) - 1)}], 80)
    hull = res.get("hull") if res.get("hull") in ("cabin", "open") else None
    return {"hull": hull, "hull_photo": res.get("photo"), "hull_evidence": res.get("evidence")}


def images_sha(urls: list[str]) -> str:
    return hashlib.sha256("\n".join(urls).encode()).hexdigest()[:16]


def _img(url: str, width: str) -> dict:
    data = httpx.get(url.replace("/dynamic/default/", f"/dynamic/{width}/"), timeout=30).content
    return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(data).decode()}}


def _ask(base: str, content: list, max_tokens: int) -> dict:
    r = httpx.post(
        f"{base}/v1/chat/completions",
        json={"temperature": 0, "max_tokens": max_tokens, "messages": [{"role": "user", "content": content}],
              "response_format": {"type": "json_object"}},
        timeout=900,
    )
    r.raise_for_status()
    return json.loads(r.json()["choices"][0]["message"]["content"])


def verify_hp(res: dict) -> float | None:
    """The power figure must be printed in the text the model read, e.g. "50" in "50 SUZUKI FOUR STROKE"."""
    hp, seen = res.get("hp"), str(res.get("text_seen") or "")
    if not isinstance(hp, (int, float)) or not 2 <= hp <= 450:
        return None
    figure = f"{hp:g}"
    return float(hp) if re.search(rf"(?<![\d.]){re.escape(figure)}(?![\d])", seen.replace(",", ".")) else None


def read_engine(base: str, urls: list[str], max_photos: int) -> dict:
    urls = urls[:max_photos]
    pick = _ask(base, [_img(u, "640w") for u in urls] + [{"type": "text", "text": PICK_PROMPT.format(last=len(urls) - 1)}], 60)
    photos = [i for i in pick.get("engine_photos") or [] if isinstance(i, int) and 0 <= i < len(urls)]
    reads = []
    for i in photos[:2]:
        res = _ask(base, [_img(urls[i], "1280w"), {"type": "text", "text": READ_PROMPT}], 200)
        res["photo"] = i
        reads.append(res)
        if verify_hp(res) and res.get("four_stroke") is not None:
            break  # power and stroke both read: done
    hp = next((verify_hp(r) for r in reads if verify_hp(r)), None)
    best = next((r for r in reads if verify_hp(r)), reads[0] if reads else {})
    stroke = next((r["four_stroke"] for r in reads if r.get("four_stroke") is not None), None)
    return {
        "engine_photos": photos,
        "hp": hp,
        "brand": best.get("brand"),
        "four_stroke": stroke,
        "text_seen": best.get("text_seen"),
        "photo": best.get("photo"),
    }
