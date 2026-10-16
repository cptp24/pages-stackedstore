#!/usr/bin/env python3
# Copy of tools/shopify_common.py in the Claude Code Playground workspace (CP). Change both together.
"""shopify_common.py - plumbing shared by the Shopify pull tools.

Brand config: config/brands/<slug>.yaml (committed; no secrets, only *_ref key names).
Secrets: .env at the workspace root.
Snapshots: <out>/<source>/<YYYY-MM-DD>/ with a manifest.json each, where <out> is the
project folder's exports/ directory (the pull tools take --out).

Copied as plain code from a verified build (live on Thermos MY, Sep 2026) and refitted
to this workspace's paths. No other dependency.
"""
import csv
import json
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
BRANDS_DIR = ROOT / "config" / "brands"

API_VERSION = "2026-07"

# Every app requests the full set up front: adding a scope later means re-authorising
# every store. read_all_orders needs Shopify approval; until it is granted the orders API
# silently returns the last 60 days only.
SCOPES = [
    "read_orders",
    "read_all_orders",
    "read_products",
    "read_inventory",
    "read_customers",
    "read_themes",
]

# GraphQL cost bucket restores at 50 points/s (standard) or 100 (Plus). Sleeping when the
# bucket is low avoids THROTTLED errors on long paginated pulls.
COST_FLOOR = 300


# --------------------------------------------------------------------------- #
# Generic plumbing
# --------------------------------------------------------------------------- #
def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def load_env(path=None):
    p = Path(path) if path else ROOT / ".env"
    env = {}
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def load_brand(slug):
    p = BRANDS_DIR / f"{slug}.yaml"
    if not p.exists():
        known = sorted(x.stem for x in BRANDS_DIR.glob("*.yaml") if not x.stem.startswith("_"))
        die(f"No registry entry at {p}.\nKnown brands: {', '.join(known) or '(none)'}")
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    cfg.setdefault("slug", slug)
    return cfg


class AtomicDir:
    """Write into a sibling temp dir; rename into place only on success.

    A failed step must never leave a half-written folder behind, because downstream
    readers treat "the folder exists" as "the contents are complete".
    """

    def __init__(self, final):
        self.final = Path(final)
        self.tmp = self.final.parent / f".tmp-{self.final.name}"

    def __enter__(self):
        if self.tmp.exists():
            shutil.rmtree(self.tmp)
        self.tmp.mkdir(parents=True)
        return self.tmp

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            shutil.rmtree(self.tmp, ignore_errors=True)
            return False
        if self.final.exists():
            shutil.rmtree(self.final)
        self.tmp.rename(self.final)
        return False


class Snapshot(AtomicDir):
    """Dated snapshot: <out>/<source>/<YYYY-MM-DD>/."""

    def __init__(self, out, source, date=None):
        self.date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        super().__init__(Path(out) / source / self.date)


def write_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")


def write_csv(path, rows):
    if not rows:
        return
    with Path(path).open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def money(x):
    try:
        return round(float(x), 2)
    except (TypeError, ValueError):
        return 0.0


def _write_manifest(dirpath, tool, tool_version, cfg, source, **extra):
    manifest = {
        "pulled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tool": tool,
        "tool_version": tool_version,
        "brand": cfg["slug"],
        "registry_notes": cfg.get("notes") or [],
    }
    manifest.update(source)
    manifest.update(extra)
    manifest["files"] = sorted(str(p.relative_to(dirpath)) for p in Path(dirpath).rglob("*")
                               if p.is_file() and p.name != "manifest.json")
    (Path(dirpath) / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


# --------------------------------------------------------------------------- #
# Shopify
# --------------------------------------------------------------------------- #
def shopify_cfg(cfg):
    sh = cfg.get("shopify") or {}
    store = (sh.get("store") or "").strip()
    if not store or not store.endswith(".myshopify.com"):
        die(f"config/brands/{cfg['slug']}.yaml: shopify.store must be <shop>.myshopify.com, got '{store}'.")
    return sh


def resolve_shopify(cfg, env):
    """Return (store, token). Dies with the unblock step when the token is missing."""
    sh = shopify_cfg(cfg)
    ref = sh.get("token_ref")
    if not ref:
        die(f"config/brands/{cfg['slug']}.yaml: shopify.token_ref is not set.")
    token = env.get(ref)
    if not token:
        die(f"{ref} is not in .env. Run: python3 tools/shopify_authorize.py --brand {cfg['slug']}")
    return sh["store"], token


class ShopifyError(Exception):
    pass


def graphql(store, token, query, variables=None, retries=3):
    url = f"https://{store}/admin/api/{API_VERSION}/graphql.json"
    headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    body = {"query": query, "variables": variables or {}}
    for attempt in range(retries):
        r = requests.post(url, json=body, headers=headers, timeout=60)
        if r.status_code in (401, 403):
            die(f"Shopify returned {r.status_code} for {store}. The token was revoked or the app "
                f"was uninstalled. Re-run: python3 tools/shopify_authorize.py --brand <slug>")
        if r.status_code == 429:
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code != 200:
            raise ShopifyError(f"HTTP {r.status_code} from {store}: {r.text[:300]}")
        payload = r.json()
        errors = payload.get("errors") or []
        if any((e.get("extensions") or {}).get("code") == "THROTTLED" for e in errors):
            time.sleep(2 * (attempt + 1))
            continue
        if errors:
            raise ShopifyError("GraphQL errors: " + "; ".join(e.get("message", str(e)) for e in errors))
        _respect_cost(payload.get("extensions") or {})
        return payload.get("data") or {}
    raise ShopifyError(f"Throttled {retries} times in a row on {store}; giving up.")


def _respect_cost(extensions):
    status = (extensions.get("cost") or {}).get("throttleStatus") or {}
    avail = status.get("currentlyAvailable")
    rate = status.get("restoreRate") or 50
    if avail is not None and avail < COST_FLOOR:
        time.sleep(max(1.0, (COST_FLOOR - avail) / rate))


def shop_info(store, token):
    q = """{ shop { name myshopifyDomain primaryDomain { host } ianaTimezone currencyCode
                    plan { displayName shopifyPlus } } }"""
    s = graphql(store, token, q)["shop"]
    return {
        "name": s["name"],
        "store": s["myshopifyDomain"],
        "domain": (s.get("primaryDomain") or {}).get("host"),
        "timezone": s["ianaTimezone"],
        "currency": s["currencyCode"],
        "plan": (s.get("plan") or {}).get("displayName"),
        "shopify_plus": bool((s.get("plan") or {}).get("shopifyPlus")),
    }


def amount(price_set):
    """shopMoney amount from a MoneyBag, as float; 0.0 when absent."""
    return money(((price_set or {}).get("shopMoney") or {}).get("amount"))


def write_manifest(dirpath, tool, tool_version, cfg, shop, **extra):
    """Shopify manifests cite the store, its timezone and currency."""
    return _write_manifest(dirpath, tool, tool_version, cfg,
                           {"store": shop["store"], "timezone": shop["timezone"], "currency": shop["currency"]},
                           **extra)
