"""Run rule extraction over cached ads, then the local LLM over what rules couldn't resolve."""

from __future__ import annotations

import datetime as dt
import sys

from . import llm, rules, store
from .config import expand
from .scrape import cached_text


def _merge(rule: dict, llm_res: dict | None) -> dict:
    """Rules win where they were confident; the LLM fills what they flagged or missed."""
    out = dict(rule)
    if not llm_res:
        return out
    if rule["needs_llm"]:
        if llm_res.get("engine_type") in ("outboard", "inboard", "sterndrive", "electric", "none") and (
            rule["engine_type"] is None or any("declared" in r for r in rule["llm_reasons"])
        ):
            out["engine_type"], out["engine_type_source"] = llm_res["engine_type"], "llm"
        if llm_res.get("engine_year"):
            if llm_res["engine_year"] != rule["engine_year"]:
                out["engine_year"], out["engine_year_source"] = llm_res["engine_year"], "llm"
                out["engine_year_evidence"] = llm_res.get("engine_year_evidence")
            else:
                out["engine_year_source"] = (rule["engine_year_source"] or "") + "+llm"
        elif rule["engine_year"] is not None and rule["llm_reasons"]:
            # LLM couldn't confirm the rule's shaky year: keep it, marked unconfirmed
            out["engine_year_source"] = (rule["engine_year_source"] or "") + "?"
    if out.get("engine_hours") is None and llm_res.get("engine_hours"):
        out["engine_hours"] = llm_res["engine_hours"]
    # LLM equipment answers are kept in ad["llm"] for reference only: in testing it listed a plotter
    # for an ad that only mentions an ekolod. Equipment value comes from rule matches with evidence.
    return out


def _public(ext: dict) -> dict:
    """Scrub evidence quotes before they go into the public git DB."""
    ext = dict(ext)
    for k in ("engine_year_evidence", "engine_type_evidence"):
        ext[k] = store.scrub(ext.get(k))
    ext["equipment"] = {k: {**v, "evidence": store.scrub(v.get("evidence"), 120)} for k, v in ext["equipment"].items()}
    return ext


def run(cfg: dict, use_llm: bool = True, llm_all: bool = False, limit: int | None = None) -> dict:
    ads = [a for a in store.all_ads() if a.get("status") == "active"]
    stats = {"extracted": 0, "no_cache": 0, "llm_queue": 0, "llm_done": 0, "llm_rejected_years": 0}
    queue: list[tuple[dict, dict, dict]] = []

    for ad in ads:
        text = cached_text(ad)
        if text is None:
            stats["no_cache"] += 1
            continue
        rule = rules.extract(text["heading"], text["description"], text["specs"], ad.get("year"), ad.get("motor_type"))
        sha = (ad.get("page") or {}).get("text_sha")
        cached_llm = ad.get("llm") if (ad.get("llm") or {}).get("text_sha") == sha else None
        if (rule["needs_llm"] or llm_all) and cached_llm is None and sha:
            queue.append((ad, rule, text))
        ad["extraction"] = _public(_merge(rule, (cached_llm or {}).get("result")))
        store.patch_ad(ad["ad_id"], extraction=ad["extraction"])
        stats["extracted"] += 1

    stats["llm_queue"] = len(queue)
    if limit:
        queue = queue[:limit]
    if use_llm and queue:
        model_name = expand(cfg["llm"]["model"]).stem
        with llm.server(cfg) as base:
            t0 = dt.datetime.now()
            for n, (ad, rule, text) in enumerate(queue, 1):
                try:
                    res = llm.extract(base, text, model_name)
                except Exception as e:  # one bad ad must not kill the batch
                    print(f"  {ad['ad_id']}: LLM failed: {e!r}", file=sys.stderr)
                    continue
                if res.get("engine_year_rejected"):
                    stats["llm_rejected_years"] += 1
                llm_rec = {
                    "text_sha": ad["page"]["text_sha"],
                    "model": model_name,
                    "at": dt.date.today().isoformat(),
                    "result": {**res, "engine_year_evidence": store.scrub(res.get("engine_year_evidence"))},
                }
                store.patch_ad(ad["ad_id"], llm=llm_rec, extraction=_public(_merge(rule, res)))
                stats["llm_done"] += 1
                if n % 10 == 0:
                    rate = (dt.datetime.now() - t0).total_seconds() / n
                    print(f"  llm {n}/{len(queue)} ({rate:.1f}s/ad)", file=sys.stderr)
    return stats
