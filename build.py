#!/usr/bin/env python3
"""Build one client page: latest snapshots + page template -> build/<site>/index.html (not yet encrypted).

  inventory  exports/shopify-inventory/<date>/inventory.json
  search     exports/ga4-site-search/<date>/site_search.json (top 5 products per term)

Data and logos are inlined so the page is one file that staticrypt can encrypt whole.
Refuses when the two 30-day windows differ, so both tabs always cover the same days.

Usage: python3 build.py --brand stackedstore-my --site stackedstore
"""
import argparse
import base64
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOP_PRODUCTS = 5


def latest(exports, source, name):
    snaps = sorted((exports / source).glob(f"[0-9]*/{name}"))
    if not snaps:
        raise SystemExit(f"No {source} snapshot in {exports}. Run the pull first.")
    return snaps[-1], json.loads(snaps[-1].read_text(encoding="utf-8"))


def inline_json(el_id, data):
    # < keeps "</script>" and "<!--" out of the block and is still valid JSON.
    text = json.dumps(data, separators=(",", ":"), ensure_ascii=False).replace("<", "\\u003c")
    return f'<script type="application/json" id="{el_id}">{text}</script>\n'


def main():
    ap = argparse.ArgumentParser(description="Build one client page.")
    ap.add_argument("--brand", required=True, help="registry slug, e.g. stackedstore-my")
    ap.add_argument("--site", required=True, help="folder under sites/, e.g. stackedstore")
    ap.add_argument("--exports", default="exports")
    ap.add_argument("--out", default="build")
    args = ap.parse_args()

    exports = ROOT / args.exports
    site = ROOT / "sites" / args.site
    inv_path, inv = latest(exports, "shopify-inventory", "inventory.json")
    sea_path, sea = latest(exports, "ga4-site-search", "site_search.json")
    if inv["windows"]["30d"] != sea["window"]:
        raise SystemExit(f"Windows differ: inventory {inv['windows']['30d']} vs search {sea['window']}. "
                         f"Re-run both pulls on the same day.")

    handles = {p["handle"] for p in inv["products"]}
    opens = [(p["handle"], p["opens"]) for t in sea["terms"] for p in t["products"]]
    joined = sum(n for h, n in opens if h in handles) / max(1, sum(n for _, n in opens))
    sea["terms"] = [{**t, "products": t["products"][:TOP_PRODUCTS]} for t in sea["terms"]]

    html = (site / "index.html").read_text(encoding="utf-8")
    for img in sorted((site / "assets").glob("*.png")):
        uri = "data:image/png;base64," + base64.b64encode(img.read_bytes()).decode()
        html = html.replace(f'src="assets/{img.name}"', f'src="{uri}"')
    if re.search(r'src="assets/', html):
        raise SystemExit("A page asset is not inlined; only PNGs in assets/ are supported.")
    marker = "<script>\n(() => {"
    if html.count(marker) != 1:
        raise SystemExit("Page script marker not found exactly once.")
    html = html.replace(marker, inline_json("data-inventory", inv) + inline_json("data-search", sea) + marker)

    out = ROOT / args.out / args.site / "index.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    t, s = inv["totals"], sea["totals"]
    print(f"{args.site}: inventory {inv_path.parent.name} ({t['products']} products, {t['in_stock']} in stock), "
          f"search {sea_path.parent.name} ({s['search_sessions']} search visits, {joined:.1%} of opens match "
          f"an active product) -> {out.relative_to(ROOT)} ({out.stat().st_size / 1e6:.2f} MB)")


if __name__ == "__main__":
    main()
