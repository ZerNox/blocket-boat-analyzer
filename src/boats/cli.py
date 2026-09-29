"""boats — fetch, extract, rank and publish Blocket boat ads."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

from . import config, market, marketplace, models_list, pipeline, rank, scrape, site, store


def cmd_fetch(cfg, args):
    print(json.dumps(scrape.update(cfg, fetch_pages=not args.no_pages, limit=args.limit)))


def cmd_extract(cfg, args):
    print(json.dumps(pipeline.run(cfg, use_llm=not args.no_llm, llm_all=args.llm_all, limit=args.limit,
                                  use_vision=not args.no_vision)))


def cmd_vision(cfg, args):
    stats: dict = {}
    pipeline.run_vision(cfg, stats, only=args.ad or None)
    print(json.dumps(stats))
    for ad_id in args.ad or []:
        ad = store.load_ad(ad_id)
        ext = ad.get("extraction") or {}
        print(ad_id, ad.get("heading"), "|", ext.get("engine_hp"), "hk via", ext.get("engine_hp_source"),
              "|", (ad.get("vision") or {}).get("result"))


def cmd_import_marketplace(cfg, args):
    files = [Path(f) for f in args.files] or marketplace.inbox_files()
    if not files:
        print(f"no marketplace-*.json in {marketplace.INBOX} or {marketplace.INBOX_FALLBACK}")
        return
    print(json.dumps(marketplace.import_files(files, cfg)))


def cmd_models(cfg, args):
    rows = models_list.derive()
    models_list.write(rows)
    kept = models_list.load()
    print(f"{len(rows)} cabin-boat model families derived, {len(kept)} kept -> {models_list.MODELS_CSV.name}")


def cmd_market(cfg, args):
    print(json.dumps(market.update(cfg)))


def cmd_rank(cfg, args):
    ranked, model = rank.rank(store.all_ads(), cfg)
    max_km = cfg["search"].get("max_km")
    if max_km:
        ranked = [r for r in ranked if r["distance_km"] is None or r["distance_km"] <= max_km]
    print(f"{model['n']} eligible ads, fit on {model['n_fit']}, residual sd {model['resid_sd_log']} (log price)\n")
    print(f"{'#':>3} {'value':>6} {'price':>8} {'fair':>8} {'year':>4} {'eng':>6} {'hp':>3}  extras / flags  heading")
    for r in ranked[: args.top]:
        eng = f"{r['engine_year'] or '?'}{'*' if (r['engine_year_source'] or '').startswith('llm') else ''}"
        extras = ",".join(r["extras"]) + (" !" + ",".join(r["red_flags"]) if r["red_flags"] else "")
        print(f"{r['rank']:>3} {r['value']:>+6.0%} {r['price']:>8} {r['fair_price']:>8} {r['boat_year'] or '?':>4} {eng:>6} "
              f"{r['hp']:>3}  {extras[:28]:<28} {r['heading'][:40]} ({r['distance_km']} km)  {r['url']}")


def cmd_audit(cfg, args):
    """Rule vs LLM engine year where both exist (run `extract --llm-all` first)."""
    agree = disagree = 0
    for ad in store.all_ads():
        llm_res = (ad.get("llm") or {}).get("result") or {}
        ext = ad.get("extraction") or {}
        src = ext.get("engine_year_source") or ""
        if not src.startswith("rule") or src.endswith("?") or not llm_res.get("engine_year"):
            continue
        if llm_res["engine_year"] == ext["engine_year"]:
            agree += 1
        else:
            disagree += 1
            print(f"{ad['ad_id']}: rule {ext['engine_year']} ({ext.get('engine_year_evidence')!r}) "
                  f"vs llm {llm_res['engine_year']} ({llm_res.get('engine_year_evidence')!r})")
    total = agree + disagree
    print(f"\nrule/LLM agreement: {agree}/{total}" + (f" = {agree / total:.0%}" if total else ""))


def cmd_site(cfg, args):
    print(json.dumps(site.build(cfg, Path(args.out)), ensure_ascii=False))


def cmd_publish(cfg, args):
    root = config.ROOT
    subprocess.run(["git", "-C", str(root), "add", "db"], check=True)
    if subprocess.run(["git", "-C", str(root), "diff", "--cached", "--quiet"]).returncode == 0:
        print("no data changes")
        return
    ads = store.all_ads()
    active = sum(1 for a in ads if a.get("status") == "active")
    msg = f"data: {dt.date.today().isoformat()} snapshot ({active} active ads)"
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", msg], check=True)
    subprocess.run(["git", "-C", str(root), "pull", "-q", "--no-rebase", "origin", "main"], check=True)
    subprocess.run(["git", "-C", str(root), "push", "-q", "origin", "HEAD:main"], check=True)
    print(msg)


def cmd_update(cfg, args):
    if marketplace.inbox_files():  # listings saved with the Marketplace bookmarklet
        print(json.dumps(marketplace.import_files(marketplace.inbox_files(), cfg)))
    cmd_fetch(cfg, argparse.Namespace(no_pages=False, limit=None))
    cmd_extract(cfg, argparse.Namespace(no_llm=args.no_llm, llm_all=False, limit=None, no_vision=False))
    if args.publish:
        cmd_publish(cfg, args)


def main(argv=None):
    p = argparse.ArgumentParser(prog="boats", description=__doc__)
    p.add_argument("--config", type=Path)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("fetch", help="search + download new/changed ad pages")
    s.add_argument("--no-pages", action="store_true")
    s.add_argument("--limit", type=int)
    s.set_defaults(fn=cmd_fetch)

    s = sub.add_parser("extract", help="rules over cached ads, local LLM for the unresolved")
    s.add_argument("--no-llm", action="store_true")
    s.add_argument("--llm-all", action="store_true", help="also run the LLM on rule-resolved ads (for `audit`)")
    s.add_argument("--no-vision", action="store_true", help="skip reading engine power from photos")
    s.add_argument("--limit", type=int, help="max ads sent to the LLM")
    s.set_defaults(fn=cmd_extract)

    s = sub.add_parser("vision", help="read engine power from photos (last resort) for all or given ads")
    s.add_argument("--ad", type=int, action="append", help="ad id (repeatable); re-reads even if cached")
    s.set_defaults(fn=cmd_vision)

    s = sub.add_parser("import-marketplace", help="import JSON saved with the Marketplace bookmarklet (default: ~/Hämtningar, ~/Downloads)")
    s.add_argument("files", nargs="*")
    s.set_defaults(fn=cmd_import_marketplace)

    s = sub.add_parser("models", help="(re)derive cabin_models.csv from the data; hand edits (keep=no) are kept")
    s.set_defaults(fn=cmd_models)

    s = sub.add_parser("market", help="snapshot every boat ad on Blocket (listing only) as comparables")
    s.set_defaults(fn=cmd_market)

    s = sub.add_parser("rank", help="print the value ranking")
    s.add_argument("--top", type=int, default=30)
    s.set_defaults(fn=cmd_rank)

    s = sub.add_parser("audit", help="rule vs LLM engine-year agreement")
    s.set_defaults(fn=cmd_audit)

    s = sub.add_parser("site", help="build the static site")
    s.add_argument("--out", default="_site")
    s.set_defaults(fn=cmd_site)

    s = sub.add_parser("publish", help="commit db/ and push (GitHub Actions rebuilds the site)")
    s.set_defaults(fn=cmd_publish)

    s = sub.add_parser("update", help="fetch + extract (+ publish)")
    s.add_argument("--no-llm", action="store_true")
    s.add_argument("--publish", action="store_true")
    s.set_defaults(fn=cmd_update)

    args = p.parse_args(argv)
    args.fn(config.load(args.config), args)


if __name__ == "__main__":
    sys.exit(main())
