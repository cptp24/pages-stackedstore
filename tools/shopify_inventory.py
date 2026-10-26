#!/usr/bin/env python3
# Copy of tools/shopify_inventory.py in the Claude Code Playground workspace (CP). Change both together.
"""shopify_inventory.py - current catalogue, stock and units sold per product for one brand.

Data acquisition, not analysis. Pulls every active product from the GraphQL Admin API and
units sold per product from ShopifyQL, for the last 30 days and the last 365 days, all
channels and Online Store only. Writes a dated snapshot to
<out>/shopify-inventory/<YYYY-MM-DD>/ with inventory.json (the page contract) and a manifest.

Definitions:
  stock        = Product.totalInventory, summed across every location (no per-store split:
                 the app has no read_locations scope)
  status       = "not_tracked" when tracksInventory is false, else "in_stock" when stock > 0,
                 else "out_of_stock"
  units        = ShopifyQL sales.net_items_sold (after returns), windows end yesterday in the
                 shop's timezone so every day is complete
  online units = WHERE sales_channel = 'Online Store' (excludes POS, Shop app, draft orders)

Units the page cannot show are not dropped silently. totals.units_store_total splits into
units_in_catalogue + units_not_in_catalogue (products no longer active) + units_no_product
(lines with no product: POS custom sales, gift cards).

Usage:
  python3 tools/shopify_inventory.py --brand stackedstore-my --out clients/stackedstore-my/exports
"""
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from shopify_common import (Snapshot, die, graphql, load_brand, load_env, money, resolve_shopify,
                            shop_info, write_json, write_manifest)

TOOL_VERSION = "1.1.0"
PAGE_SIZE = 250
ONLINE_CHANNEL = "Online Store"
WINDOWS = {"30d": 30, "12m": 365}

PRODUCTS_QUERY = """
query Products($first: Int!, $after: String) {
  products(first: $first, after: $after, query: "status:active") {
    pageInfo { hasNextPage endCursor }
    nodes {
      legacyResourceId title handle vendor productType createdAt tags onlineStoreUrl
      totalInventory tracksInventory
      variantsCount { count }
      priceRangeV2 { minVariantPrice { amount } maxVariantPrice { amount } }
      compareAtPriceRange { maxVariantCompareAtPrice { amount } }
      # Stacked Store's own switch for collection pages; null on stores without it.
      showc: metafield(namespace: "custom", key: "show_in_collections") { value }
    }
  }
}
"""

SHOPIFYQL = "query($q: String!) { shopifyqlQuery(query: $q) { tableData { rows } parseErrors } }"


def fetch_products(store, token):
    out, after = [], None
    while True:
        conn = graphql(store, token, PRODUCTS_QUERY, {"first": PAGE_SIZE, "after": after})["products"]
        out.extend(conn["nodes"])
        if not conn["pageInfo"]["hasNextPage"]:
            return out
        after = conn["pageInfo"]["endCursor"]


def shopifyql_rows(store, token, q):
    r = graphql(store, token, SHOPIFYQL, {"q": q})["shopifyqlQuery"]
    if r.get("parseErrors"):
        die(f"ShopifyQL rejected: {q}\n{r['parseErrors']}")
    return (r.get("tableData") or {}).get("rows") or []


def units_by_product(store, token, since, until, online):
    """Returns ({product_id: units}, store total). The total includes lines with no product."""
    where = f"WHERE sales_channel = '{ONLINE_CHANNEL}' " if online else ""
    span = f"SINCE {since} UNTIL {until}"
    rows = shopifyql_rows(store, token, f"FROM sales SHOW net_items_sold {where}GROUP BY product_id {span} LIMIT 100000")
    total = shopifyql_rows(store, token, f"FROM sales SHOW net_items_sold {where}{span}")
    by_pid = {row["product_id"]: int(float(row["net_items_sold"] or 0)) for row in rows if row.get("product_id")}
    return by_pid, int(float((total[0] if total else {}).get("net_items_sold") or 0))


def status_of(p):
    if not p["tracksInventory"]:
        return "not_tracked"
    return "in_stock" if (p["totalInventory"] or 0) > 0 else "out_of_stock"


def main():
    ap = argparse.ArgumentParser(description="Pull catalogue, stock and units sold for one brand.")
    ap.add_argument("--brand", required=True, help="registry slug, e.g. stackedstore-my")
    ap.add_argument("--date", help="snapshot folder date YYYY-MM-DD (default: today UTC)")
    ap.add_argument("--out", help="exports dir; the snapshot lands at <out>/shopify-inventory/<date>/ "
                                  "(default: clients/<slug>/exports)")
    args = ap.parse_args()

    cfg = load_brand(args.brand)
    out_base = Path(args.out) if args.out else Path("clients") / cfg["slug"] / "exports"
    store, token = resolve_shopify(cfg, load_env())
    shop = shop_info(store, token)
    tz = ZoneInfo(shop["timezone"])

    until = datetime.now(tz).date() - timedelta(days=1)
    windows = {k: {"since": str(until - timedelta(days=d - 1)), "until": str(until)} for k, d in WINDOWS.items()}

    units, store_totals = {}, {}
    for k, w in windows.items():
        for ch, online in (("all", False), ("online", True)):
            units[f"{k}_{ch}"], store_totals[f"{k}_{ch}"] = units_by_product(
                store, token, w["since"], w["until"], online)

    raw = fetch_products(store, token)
    products, ids = [], set()
    for p in raw:
        pid = p["legacyResourceId"]
        ids.add(pid)
        compare = money(((p.get("compareAtPriceRange") or {}).get("maxVariantCompareAtPrice") or {}).get("amount"))
        products.append({
            "id": pid,
            "title": p["title"],
            "handle": p["handle"],
            "brand": (p["vendor"] or "").strip(),
            "type": (p["productType"] or "").strip(),
            "created": p["createdAt"][:10],
            "tags": p["tags"],
            "on_online_store": bool(p["onlineStoreUrl"]),
            "show_in_collections": (p["showc"] or {}).get("value"),
            "price_min": money(p["priceRangeV2"]["minVariantPrice"]["amount"]),
            "price_max": money(p["priceRangeV2"]["maxVariantPrice"]["amount"]),
            "compare_at": compare or None,
            "variants": p["variantsCount"]["count"],
            "stock": p["totalInventory"] if p["tracksInventory"] else None,
            "status": status_of(p),
            **{k: v.get(pid, 0) for k, v in units.items()},
        })

    by_status = {s: sum(1 for p in products if p["status"] == s) for s in ("in_stock", "out_of_stock", "not_tracked")}
    totals = {
        "products": len(products),
        **by_status,
        "out_of_stock_sold_30d": sum(1 for p in products if p["status"] == "out_of_stock" and p["30d_all"] > 0),
        "units_store_total": store_totals,
        "units_in_catalogue": {k: sum(n for pid, n in v.items() if pid in ids) for k, v in units.items()},
        "units_not_in_catalogue": {k: sum(n for pid, n in v.items() if pid not in ids) for k, v in units.items()},
        "units_no_product": {k: store_totals[k] - sum(v.values()) for k, v in units.items()},
    }
    definitions = {
        "stock": "Product.totalInventory, all locations combined; null when inventory is not tracked",
        "status": "not_tracked if tracksInventory is false; in_stock if stock > 0; else out_of_stock",
        "units": "ShopifyQL sales.net_items_sold (after returns) per product_id",
        "online": f"sales_channel = '{ONLINE_CHANNEL}' only",
        "windows": "inclusive, ending yesterday in the shop timezone",
        "on_online_store": "true when the product is published to the Online Store (onlineStoreUrl set)",
        "show_in_collections": "metafield custom.show_in_collections as stored; null when unset",
        "tags": "Product.tags as stored",
    }
    data = {
        "pulled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "shop": {"name": shop["name"], "domain": shop["domain"], "currency": shop["currency"],
                 "timezone": shop["timezone"]},
        "windows": windows,
        "definitions": definitions,
        "totals": totals,
        "products": products,
    }

    with Snapshot(out_base, "shopify-inventory", args.date) as out:
        write_json(out / "inventory.json", data)
        write_manifest(out, "shopify_inventory.py", TOOL_VERSION, cfg, shop,
                       windows=windows, totals=totals, definitions=definitions)
    final = Snapshot(out_base, "shopify-inventory", args.date).final
    print(f"OK {cfg['slug']}: {totals['products']} active products, {by_status['in_stock']} in stock, "
          f"{by_status['out_of_stock']} out of stock, {by_status['not_tracked']} not tracked -> {final}")


if __name__ == "__main__":
    main()
