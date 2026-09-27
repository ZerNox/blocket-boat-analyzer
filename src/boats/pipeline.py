"""Run rule extraction over cached ads, then the local LLM over what rules couldn't resolve."""

from __future__ import annotations

import datetime as dt
import sys

from . import llm, rules, store, vision
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
        elif rule["engine_year"] is not None and any("engine" in r and ("older" in r or "ambiguous" in r) for r in rule["llm_reasons"]):
            # LLM couldn't confirm the rule's shaky year: keep it, marked unconfirmed
            out["engine_year_source"] = (rule["engine_year_source"] or "") + "?"
    if out.get("engine_hp") is None and llm_res.get("engine_hp"):
        out["engine_hp"], out["engine_hp_source"] = llm_res["engine_hp"], "llm"
    if out.get("engine_hours") is None and llm_res.get("engine_hours"):
        out["engine_hours"] = llm_res["engine_hours"]
    # LLM equipment answers are kept in ad["llm"] for reference only: in testing it listed a plotter
    # for an ad that only mentions an ekolod. Equipment value comes from rule matches with evidence.
    return out


def _apply_vision(ext: dict, res: dict | None, boat_class: str | None = None) -> dict:
    """Photo reading only fills what the text left empty."""
    if res and res.get("hull") in ("cabin", "open") and (not ext.get("hull_certain") or ext.get("hull_source") == "bild"):
        # A small cuddy under the foredeck is easy to miss in photos: if the seller filed it as a cabin
        # type and the photos say open, keep it (marked uncertain) rather than risk dropping a match.
        disagrees = res["hull"] == "open" and boat_class in rules.CABIN_CLASSES
        ext = {**ext, "hull": res["hull"], "hull_source": "bild", "hull_certain": not disagrees,
               "hull_evidence": store.scrub(res.get("hull_evidence"), 120)}
    if not res or not res.get("hp") or (ext.get("engine_hp") and ext.get("engine_hp_source") != "bild"):
        return ext
    ext = {**ext, "engine_hp": res["hp"], "engine_hp_source": "bild",
           "engine_hp_evidence": store.scrub(f"Foto {res.get('photo')}: {res.get('text_seen')}")}
    if ext.get("engine_stroke") is None and res.get("four_stroke") is not None:
        ext["engine_stroke"] = 4 if res["four_stroke"] else 2
    return ext


def _vision_tasks(ad: dict, force: bool = False) -> list[str]:
    """What the photos still have to answer: engine power and/or cabin vs. open boat."""
    ext = ad.get("extraction") or {}
    if ext.get("engine_type") not in ("outboard", None) or not ad.get("image_urls"):
        return []
    tasks = []
    hp_from_text = ext.get("engine_hp") and ext.get("engine_hp_source") != "bild"
    if not ad.get("motor_size") and (force or not hp_from_text) and (force or not ext.get("engine_hp")):
        tasks.append("hp")
    if not ext.get("hull_certain") or (force and ext.get("hull_source") == "bild"):
        tasks.append("hull")
    return tasks


def _needs_vision(ad: dict, force: bool = False) -> bool:
    return bool(_vision_tasks(ad, force))


def _public(ext: dict) -> dict:
    """Scrub evidence quotes before they go into the public git DB."""
    ext = dict(ext)
    for k in ("engine_year_evidence", "engine_type_evidence"):
        ext[k] = store.scrub(ext.get(k))
    ext["equipment"] = {k: {**v, "evidence": store.scrub(v.get("evidence"), 120)} for k, v in ext["equipment"].items()}
    return ext


def run(cfg: dict, use_llm: bool = True, llm_all: bool = False, limit: int | None = None, use_vision: bool = True) -> dict:
    ads = [a for a in store.all_ads() if a.get("status") == "active"]
    stats = {"extracted": 0, "no_cache": 0, "llm_queue": 0, "llm_done": 0, "llm_rejected_years": 0,
             "vision_queue": 0, "vision_hp_found": 0}
    queue: list[tuple[dict, dict, dict]] = []

    for ad in ads:
        text = cached_text(ad)
        if text is None:
            stats["no_cache"] += 1
            continue
        rule = rules.extract(text["heading"], text["description"], text["specs"], ad.get("year"), ad.get("motor_type"),
                             ad.get("boat_class"))
        sha = (ad.get("page") or {}).get("text_sha")
        cached_llm = ad.get("llm") if (ad.get("llm") or {}).get("text_sha") == sha else None
        if (rule["needs_llm"] or llm_all) and cached_llm is None and sha:
            queue.append((ad, rule, text))
        cached_vision = (ad.get("vision") or {}).get("result")
        ad["extraction"] = _public(_apply_vision(_merge(rule, (cached_llm or {}).get("result")), cached_vision,
                                                 ad.get("boat_class")))
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
    if use_vision and use_llm:
        run_vision(cfg, stats, limit)
    return stats


def run_vision(cfg: dict, stats: dict, limit: int | None = None, only: list[int] | None = None) -> None:
    """Last resort: photos, only for outboards whose power the text never gave."""
    queue = []
    ads = [store.load_ad(i) for i in only] if only else store.all_ads()
    for ad in ads:
        if not ad or ad.get("status") != "active" or not _needs_vision(ad, force=bool(only)):
            continue
        sha = vision.images_sha(ad["image_urls"])
        done = (ad.get("vision") or {}).get("images_sha") == sha and (ad.get("vision") or {}).get("tasks")
        todo = [t for t in _vision_tasks(ad, force=bool(only)) if only or not done or t not in done]
        if todo:
            queue.append((ad, sha, todo))
    stats["vision_queue"] = len(queue)
    if limit:
        queue = queue[:limit]
    if not queue:
        return
    model_name = expand(cfg["vision"]["model"]).stem
    with llm.server(cfg, "vision") as base:
        t0 = dt.datetime.now()
        for n, (ad, sha, todo) in enumerate(queue, 1):
            prev = (ad.get("vision") or {})
            res = dict(prev.get("result") or {}) if prev.get("images_sha") == sha else {}
            try:
                if "hp" in todo:
                    res.update(vision.read_engine(base, ad["image_urls"], cfg["vision"]["max_photos"]))
                    res["text_seen"] = store.scrub(res.get("text_seen"), 120)
                if "hull" in todo:
                    res.update(vision.classify_hull(base, ad["image_urls"], cfg["vision"]["max_photos"]))
            except Exception as e:  # one bad ad must not kill the batch
                print(f"  {ad['ad_id']}: vision failed: {e!r}", file=sys.stderr)
                continue
            tasks = sorted(set(prev.get("tasks") or []) | set(todo)) if prev.get("images_sha") == sha else todo
            rec = {"images_sha": sha, "model": model_name, "at": dt.date.today().isoformat(), "tasks": tasks, "result": res}
            store.patch_ad(ad["ad_id"], vision=rec, extraction=_apply_vision(ad["extraction"], res, ad.get("boat_class")))
            stats["vision_hp_found"] = stats.get("vision_hp_found", 0) + bool("hp" in todo and res.get("hp"))
            rate = (dt.datetime.now() - t0).total_seconds() / n
            print(f"  vision {n}/{len(queue)} ({rate:.0f}s/ad) {ad['ad_id']}: {todo} -> hp={res.get('hp')} hull={res.get('hull')}",
                  file=sys.stderr)
