"""Local LLM fallback (llama.cpp server on the discrete GPU) for ads the rules can't resolve.

The model must quote the sentence its engine year comes from; a year whose quote
is not found verbatim in the ad (or doesn't contain that year) is discarded as
a hallucination.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx

from .config import expand
from . import rules
from .rules import EQUIPMENT

ITEMS = list(EQUIPMENT)

SCHEMA = {
    "type": "object",
    "properties": {
        "engine_type": {"enum": ["outboard", "inboard", "sterndrive", "electric", "none", "unknown"]},
        "engine_maker": {"type": ["string", "null"]},
        "engine_hp": {"type": ["number", "null"]},
        "engine_year_evidence": {"type": ["string", "null"]},
        "engine_year": {"type": ["integer", "null"]},
        "engine_hours": {"type": ["integer", "null"]},
        "boat_year": {"type": ["integer", "null"]},
        "equipment": {
            "type": "object",
            "properties": {k: {"enum": ["included", "excluded", "not_mentioned"]} for k in ITEMS},
            "required": ITEMS,
        },
    },
    "required": [
        "engine_type", "engine_maker", "engine_hp", "engine_year_evidence",
        "engine_year", "engine_hours", "boat_year", "equipment",
    ],
}

SYSTEM = """You extract facts from Swedish used-boat ads (Blocket). Answer only from the ad text.

engine_type: outboard (utombordare, aktersnurra), inboard (inombordare), sterndrive (drev, aquamatic, z-drev), electric, none (sold without engine), unknown.
engine_year: the model year the ENGINE was manufactured, or the year it was bought new ("ny motor 2026", "köpt ny 2019").
  - NOT the boat/hull year, unless the text says the engine is original ("originalmotor", "samma år som båten").
  - NOT service, repair, purchase-used, bottom-paint or warranty years ("servad 2023", "köpt 2020").
  - Two-digit years like "-18" mean 2018.
  - If the text doesn't state it, use null. Do not guess from the boat year.
engine_year_evidence: copy the whole sentence from the ad that states the engine year, verbatim (at least the engine name and the year), or null.
engine_hours: running hours (gångtid, timmar) or null.
equipment: for each item, "included" if the sale includes it, "excluded" if the ad says it is not included / sold separately / can be bought extra, else "not_mentioned".
  trailer = trailer/kärra/båtvagn; plotter = GPS/kartplotter/chartplotter; ekolod = fishfinder/sonar; kapell = sprayhood/suncover/förkapell (not winter cover);
  vinterkapell = winter cover/presenning; elmotor = electric trolling/bow motor; hjalpmotor = auxiliary engine; vhf; stereo; hydraulstyrning; landstrom = shore power/charger.
"""


def _ad_prompt(text: dict) -> str:
    specs = "\n".join(f"{k}: {v}" for k, v in text["specs"].items())
    desc = text["description"][:6000]
    return f"Rubrik: {text['heading']}\n\nSpecifikationer:\n{specs}\n\nBeskrivning:\n{desc}"


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().casefold()


def verify_year(result: dict, text: dict) -> dict:
    """Keep the LLM's engine year only if its quote is real and the year isn't an event date.

    The quote is expanded to its full sentence in the ad, then the same cue logic the rules use
    decides what the year refers to: a year right after servad/köpt/bytt/... or a boat word is
    rejected ("Motorn servades i maj 2026", "köpt begagnat 2024", "båten 2010").
    """
    year, ev = result.get("engine_year"), result.get("engine_year_evidence")
    if year is None:
        return result
    reject = {**result, "engine_year": None, "engine_year_rejected": year}
    if not ev or not (1955 <= year <= rules.THIS_YEAR + 1):
        return reject
    specs = "\n".join(f"{k}: {v}" for k, v in text["specs"].items())
    sents = rules.sentences(f"{text['heading']}.\n{text['description']}\n{specs}")
    sent = next((s for s in sents if _norm(ev) in _norm(s)), None)
    if sent is None:
        return reject
    if re.search(r"orginal|original|samma år", ev, re.I) and rules.ENGINE_CUE.search(sent):
        return {**result, "engine_year_evidence": sent}
    for y, pos in rules.year_mentions(sent):
        if y == year and rules._nearest_cue_before(sent, pos) in ("engine", "new", None):
            if rules.ENGINE_CUE.search(sent):
                return {**result, "engine_year_evidence": sent}
    return reject


def _pick_device(server_bin: Path, match: str) -> str:
    out = subprocess.run([str(server_bin), "--list-devices"], capture_output=True, text=True, timeout=60)
    for line in (out.stdout + out.stderr).splitlines():
        m = re.match(r"\s*(\w+\d+):\s*(.+)", line)
        if m and match.lower() in m.group(2).lower():
            return m.group(1)
    raise RuntimeError(f"no llama.cpp device matching {match!r}:\n{out.stdout}{out.stderr}")


@contextlib.contextmanager
def gpu_lock(path: Path, timeout_s: float):
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_s
    with open(path, "a+") as fh:
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"GPU busy (lock {path})")
                time.sleep(10)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


@contextlib.contextmanager
def server(cfg: dict, section: str = "llm"):
    """Start llama-server on the configured GPU (holding the shared GPU lock); yield its base URL.

    `section` picks the model: "llm" (text) or "vision" (adds the multimodal projector).
    """
    lcfg = {**cfg["llm"], **cfg.get(section, {})}
    base = f"http://127.0.0.1:{lcfg['port']}"
    with contextlib.suppress(httpx.HTTPError):
        if httpx.get(f"{base}/health", timeout=2).status_code == 200:
            yield base  # someone already runs it
            return
    with gpu_lock(expand(lcfg["gpu_lock"]), lcfg["gpu_lock_timeout_s"]):
        bin_ = expand(lcfg["server_bin"])
        device = _pick_device(bin_, lcfg["device_match"])
        print(f"starting llama-server on {device}…", file=sys.stderr)
        log = open(expand("~/.cache/boats-llama-server.log"), "w")
        proc = subprocess.Popen(
            [str(bin_), "-m", str(expand(lcfg["model"])), "--device", device, "-ngl", "99",
             "-c", str(lcfg["ctx"]), "--host", "127.0.0.1", "--port", str(lcfg["port"]), "-np", "1"]
            + (["--mmproj", str(expand(lcfg["mmproj"]))] if lcfg.get("mmproj") else []),
            stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            for _ in range(180):
                if proc.poll() is not None:
                    raise RuntimeError("llama-server exited; see ~/.cache/boats-llama-server.log")
                with contextlib.suppress(httpx.HTTPError):
                    if httpx.get(f"{base}/health", timeout=2).status_code == 200:
                        break
                time.sleep(1)
            else:
                raise RuntimeError("llama-server did not become healthy")
            yield base
        finally:
            proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(20)
            if proc.poll() is None:
                proc.kill()


def extract(base: str, text: dict, model_name: str) -> dict:
    body = {
        "model": model_name,
        "temperature": 0,
        "max_tokens": 600,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": _ad_prompt(text)}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "boat", "schema": SCHEMA}},
    }
    r = httpx.post(f"{base}/v1/chat/completions", json=body, timeout=300)
    r.raise_for_status()
    result = json.loads(r.json()["choices"][0]["message"]["content"])
    return verify_year(result, text)
