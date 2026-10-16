#!/usr/bin/env python3
# Copy of tools/ga4_pull.py in the Claude Code Playground workspace (CP). Change both together.
"""ga4_pull.py - GA4 Data API puller for the CRO engine.

Data acquisition, not analysis: fetches live GA4 figures and writes them to disk
for read-ga4 to format. Uses GA4 relative date strings so no system clock is
baked in; the API resolves the window server-side.

Auth is a service account with Google Workspace domain-wide delegation,
impersonating whichever shared account holds the property. No browser, no human
credential, no per-brand grants. Setup:
  tools/setup/ga4-setup.md

Brand config lives in config/brands/<slug>.yaml (committed; property ids are not
secrets). Credentials come from GA4_SA_KEY_PATH in .env. Without a key file, the
tool signs as the service account through the caller's own gcloud login
(`gcloud auth application-default login`), which needs Service Account Token
Creator on SA_EMAIL.

Usage:
  python3 tools/ga4_pull.py --brand thermos-my --md
  python3 tools/ga4_pull.py --brand thermos-my --range 30d
  python3 tools/ga4_pull.py --property 438906410 --subject analytics@z21studio.com
  python3 tools/ga4_pull.py --brand thermos-my --out ./somewhere
"""
import argparse
import csv
import json
import os
import sys
from datetime import date
from pathlib import Path

import yaml

SCOPES = ["https://www.googleapis.com/auth/analytics.readonly"]
DEFAULT_KEY_PATH = "~/.config/z21/ga4-sa.json"
SA_EMAIL = "cro-engine-convx@cro-engine-convx.iam.gserviceaccount.com"
TOKEN_URI = "https://oauth2.googleapis.com/token"
BRANDS_DIR = Path("config/brands")

FUNNEL_EVENTS = ["session_start", "view_item", "add_to_cart", "begin_checkout", "purchase"]

# keyEvents sits alongside ecommercePurchases on purpose. Lead-gen and booking
# brands record no ecommerce events at all, so an ecommerce-only CR reads 0.00%
# and looks like a catastrophic finding when it is a tracking-model difference.
CONV_METS = ["ecommercePurchases", "purchaseRevenue", "keyEvents"]

# Countries excluded from every report in this run (set by --exclude-country). The audit rule
# for bot traffic on some brands is "drop sessions from country X"; applying it here keeps the
# pull comparable with the figures the client has already seen on slides.
EXCLUDE_COUNTRIES = []


def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def load_env(path=".env"):
    env = {}
    p = Path(path)
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def load_brand(slug):
    p = BRANDS_DIR / f"{slug}.yaml"
    if not p.exists():
        available = sorted(x.stem for x in BRANDS_DIR.glob("*.yaml")) if BRANDS_DIR.exists() else []
        die(f"No registry entry at {p}.\nKnown brands: {', '.join(available) or '(none)'}\n"
            f"Or pass --property and --subject directly.")
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def get_credentials(env, subject):
    from google.oauth2 import service_account
    key_path = os.path.expanduser(env.get("GA4_SA_KEY_PATH", DEFAULT_KEY_PATH))
    if Path(key_path).exists():
        creds = service_account.Credentials.from_service_account_file(key_path, scopes=SCOPES)
        return creds.with_subject(subject)
    # No key file: sign as the service account using the caller's own gcloud login.
    # Needs Service Account Token Creator on SA_EMAIL.
    import google.auth
    from google.auth import exceptions, iam
    from google.auth.transport.requests import Request
    try:
        source, _ = google.auth.default()
    except exceptions.DefaultCredentialsError:
        die(f"No service account key at {key_path} and no gcloud login.\n"
            "Run: gcloud auth application-default login")
    signer = iam.Signer(Request(), source, SA_EMAIL)
    return service_account.Credentials(signer, SA_EMAIL, TOKEN_URI, scopes=SCOPES, subject=subject)


# --------------------------------------------------------------------------- #
# Data API
# --------------------------------------------------------------------------- #
def make_data_client(creds):
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
    return BetaAnalyticsDataClient(credentials=creds)


def run_report(client, pid, dims, mets, start, end, limit=None, order_metric=None,
               dim_in_filter=None):
    from google.analytics.data_v1beta.types import (
        DateRange, Dimension, Metric, RunReportRequest, OrderBy, Filter, FilterExpression,
        FilterExpressionList,
    )
    req = RunReportRequest(
        property=f"properties/{pid}",
        dimensions=[Dimension(name=d) for d in dims],
        metrics=[Metric(name=m) for m in mets],
        date_ranges=[DateRange(start_date=start, end_date=end)],
    )
    if limit:
        req.limit = limit
    if order_metric:
        req.order_bys = [OrderBy(metric=OrderBy.MetricOrderBy(metric_name=order_metric), desc=True)]
    exprs = []
    if dim_in_filter:
        field, values = dim_in_filter
        exprs.append(FilterExpression(
            filter=Filter(field_name=field, in_list_filter=Filter.InListFilter(values=values))))
    if EXCLUDE_COUNTRIES:
        exprs.append(FilterExpression(not_expression=FilterExpression(
            filter=Filter(field_name="country", in_list_filter=Filter.InListFilter(values=EXCLUDE_COUNTRIES)))))
    if len(exprs) == 1:
        req.dimension_filter = exprs[0]
    elif exprs:
        req.dimension_filter = FilterExpression(and_group=FilterExpressionList(expressions=exprs))
    resp = client.run_report(req)
    rows = []
    for r in resp.rows:
        row = {d: r.dimension_values[i].value for i, d in enumerate(dims)}
        row.update({m: r.metric_values[j].value for j, m in enumerate(mets)})
        rows.append(row)
    return rows, bool(resp.metadata.subject_to_thresholding)


def f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def pct(num, den):
    return round(num / den * 100, 2) if den else None


def money(x):
    return round(f(x), 2)


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #
def build_overview(client, pid, start, end):
    mets = ["sessions", "totalUsers", "newUsers", "averageSessionDuration"] + CONV_METS
    rows, thresholded = run_report(client, pid, [], mets, start, end)
    r = rows[0] if rows else {}
    sessions, users = f(r.get("sessions")), f(r.get("totalUsers"))
    purchases, revenue = f(r.get("ecommercePurchases")), f(r.get("purchaseRevenue"))
    key_events = f(r.get("keyEvents"))
    return {
        "sessions": int(sessions),
        "users": int(users),
        "new_users": int(f(r.get("newUsers"))),
        "sessions_per_user": round(sessions / users, 2) if users else None,
        "avg_session_duration_s": round(f(r.get("averageSessionDuration")), 1),
        "transactions": int(purchases),
        "revenue": money(revenue),
        "cr_pct": pct(purchases, sessions),
        "aov": round(revenue / purchases, 2) if purchases else None,
        "key_events": int(key_events),
        "key_event_rate_pct": pct(key_events, sessions),
        # >100% means low-intent events (page_view, view_item) are flagged as key
        # events, so GA4's own conversion reporting is unusable for this property.
        "key_events_misconfigured": bool(sessions and key_events > sessions),
        "thresholded": thresholded,
    }


def build_breakdown(client, pid, dim, start, end, limit=None):
    mets = ["sessions"] + CONV_METS
    rows, _ = run_report(client, pid, [dim], mets, start, end, limit=limit,
                         order_metric="sessions")
    total_rev = sum(f(x.get("purchaseRevenue")) for x in rows)
    out = []
    for x in rows:
        s, p, rev = f(x.get("sessions")), f(x.get("ecommercePurchases")), f(x.get("purchaseRevenue"))
        out.append({
            dim: x.get(dim),
            "sessions": int(s),
            "transactions": int(p),
            "cr_pct": pct(p, s),
            "revenue": money(rev),
            "revenue_share_pct": round(rev / total_rev * 100, 1) if total_rev else None,
            "key_events": int(f(x.get("keyEvents"))),
            "key_event_rate_pct": pct(f(x.get("keyEvents")), s),
        })
    return out


def build_monthly(client, pid):
    rows, _ = run_report(client, pid, ["yearMonth"], ["sessions"] + CONV_METS,
                         "395daysAgo", "yesterday")
    rows.sort(key=lambda x: x.get("yearMonth", ""))
    out = []
    for x in rows:
        s, p = f(x.get("sessions")), f(x.get("ecommercePurchases"))
        ym = x.get("yearMonth", "")
        out.append({
            "month": f"{ym[:4]}-{ym[4:]}" if len(ym) == 6 else ym,
            "sessions": int(s),
            "cr_pct": pct(p, s),
            "revenue": money(f(x.get("purchaseRevenue"))),
            "orders": int(p),
            "key_events": int(f(x.get("keyEvents"))),
        })
    return out[-13:]


def build_top_pages(client, pid, start, end, limit=10):
    rows, _ = run_report(client, pid, ["landingPage"], ["sessions"] + CONV_METS,
                         start, end, limit=limit, order_metric="sessions")
    return [{
        "landing_page": x.get("landingPage"),
        "sessions": int(f(x.get("sessions"))),
        "cr_pct": pct(f(x.get("ecommercePurchases")), f(x.get("sessions"))),
        "revenue": money(f(x.get("purchaseRevenue"))),
        "key_events": int(f(x.get("keyEvents"))),
    } for x in rows]


def build_browser_by_device(client, pid, start, end):
    rows, _ = run_report(client, pid, ["deviceCategory", "browser"],
                         ["sessions", "ecommercePurchases"], start, end,
                         order_metric="sessions")
    return [{
        "device": x.get("deviceCategory"),
        "browser": x.get("browser"),
        "sessions": int(f(x.get("sessions"))),
        "transactions": int(f(x.get("ecommercePurchases"))),
        "cr_pct": pct(f(x.get("ecommercePurchases")), f(x.get("sessions"))),
    } for x in rows]


def build_sources(client, pid, start, end, limit=25):
    rows, _ = run_report(client, pid, ["sessionSourceMedium"], ["sessions"] + CONV_METS,
                         start, end, limit=limit, order_metric="sessions")
    return [{
        "source_medium": x.get("sessionSourceMedium"),
        "sessions": int(f(x.get("sessions"))),
        "transactions": int(f(x.get("ecommercePurchases"))),
        "cr_pct": pct(f(x.get("ecommercePurchases")), f(x.get("sessions"))),
        "revenue": money(f(x.get("purchaseRevenue"))),
    } for x in rows]


def build_funnel(client, pid, start, end):
    rows, _ = run_report(client, pid, ["eventName"], ["eventCount", "totalUsers"],
                         start, end, dim_in_filter=("eventName", FUNNEL_EVENTS))
    by = {x.get("eventName"): x for x in rows}
    out, prev = [], None
    for ev in FUNNEL_EVENTS:
        x = by.get(ev)
        users = int(f(x.get("totalUsers"))) if x else 0
        # A step absent from the property is a tracking gap, not a 100% drop-off.
        # Mark it and carry the previous baseline rather than reporting a fake cliff.
        missing = x is None
        drop = None if (missing or prev in (None, 0)) else round((prev - users) / prev * 100, 1)
        out.append({
            "step": ev,
            "users": users,
            "event_count": int(f(x.get("eventCount"))) if x else 0,
            "drop_off_pct": drop,
            "missing": missing,
        })
        if not missing and users:
            prev = users
    return out


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def write_csv(path, rows):
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def md_digest(b):
    L = [f"# GA4 pull: {b['name']} ({b['slug']})",
         f"property {b['property_id']} via {b['subject']}", ""]
    for rng in ("30d", "90d"):
        o = b["ranges"].get(rng)
        if not o:
            continue
        L.append(f"## Overview {rng}")
        L.append(f"- Sessions {o['sessions']:,} | Users {o['users']:,} | "
                 f"CR {o['cr_pct']}% | Orders {o['transactions']:,} | "
                 f"Revenue {o['revenue']:,} | AOV {o['aov']}")
        if o["key_events_misconfigured"]:
            L.append(f"- Key events {o['key_events']:,} vs {o['sessions']:,} sessions "
                     f"({o['key_event_rate_pct']}%). MISCONFIGURED: low-intent events are "
                     f"flagged as key events, so GA4 conversion reporting is unusable here")
        elif o["key_events"] == 0:
            L.append("- Key events: none recorded. No conversion tracking on this property")
        else:
            L.append(f"- Key events {o['key_events']:,} ({o['key_event_rate_pct']}% of sessions)")
        if o["thresholded"]:
            L.append("- WARNING: response subject to data thresholding, rows withheld")
    for label, key, dim in [("Device (90d)", "device", "deviceCategory"),
                            ("Channels (90d)", "channels", "sessionDefaultChannelGroup")]:
        L += ["", f"## {label}"]
        for r in b[key]:
            L.append(f"- {r[dim]}: {r['sessions']:,} sess, CR {r['cr_pct']}%, "
                     f"key events {r['key_events']:,}, rev {r['revenue']:,}")
    L += ["", "## Top source / medium (90d)"]
    for s in b["sources"][:8]:
        L.append(f"- {s['source_medium']}: {s['sessions']:,} sess, CR {s['cr_pct']}%")
    L += ["", f"## Funnel ({b.get('funnel_range', '90d')}, users)"
              + (f", excluding {', '.join(b['filters']['exclude_countries'])}" if b.get('filters', {}).get('exclude_countries') else "")]
    for s in b["funnel"]:
        if s["missing"]:
            L.append(f"- {s['step']}: NOT TRACKED")
        else:
            d = "" if s["drop_off_pct"] is None else f" (-{s['drop_off_pct']}%)"
            L.append(f"- {s['step']}: {s['users']:,}{d}")
    m = b["monthly"]
    L += ["", f"## Monthly series: {len(m)} months "
              f"({m[0]['month']} .. {m[-1]['month']})" if m else "## Monthly series: none"]
    for note in b.get("notes") or []:
        L += ["", f"## Registry note\n{note.strip()}"]
    L += ["", f"Exports: {b['exports_dir']}"]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="Pull GA4 data for the CRO engine.")
    ap.add_argument("--brand", help="registry slug, e.g. thermos-my")
    ap.add_argument("--property", help="numeric property id (overrides registry)")
    ap.add_argument("--subject", help="account to impersonate (overrides registry)")
    ap.add_argument("--range", choices=["30d", "90d", "both"], default="both")
    ap.add_argument("--md", action="store_true", help="print a human digest instead of JSON")
    ap.add_argument("--out", help="exports dir (default: clients/<slug>/exports/ga4-<today>)")
    ap.add_argument("--exclude-country", action="append", default=[], metavar="COUNTRY",
                    help="drop sessions from this GA4 country value in every report (repeatable), e.g. China")
    ap.add_argument("--funnel-range", choices=["30d", "90d"], default="90d",
                    help="window for the funnel section (default 90d)")
    args = ap.parse_args()
    EXCLUDE_COUNTRIES[:] = args.exclude_country

    if not args.brand and not args.property:
        die("Provide --brand <slug>, or --property with --subject.")

    cfg = load_brand(args.brand) if args.brand else {}
    ga4 = cfg.get("ga4") or {}
    slug = args.brand or f"property-{args.property}"
    pid = args.property or ga4.get("property_id")
    subject = args.subject or ga4.get("subject")
    if not pid:
        die(f"No property_id for '{slug}'. Set it in config/brands/{slug}.yaml.")
    if not subject:
        die(f"No subject for '{slug}'. Set ga4.subject in config/brands/{slug}.yaml, "
            f"or pass --subject.")

    creds = get_credentials(load_env(), subject)
    client = make_data_client(creds)

    ranges = {"30d": ("30daysAgo", "yesterday"), "90d": ("90daysAgo", "yesterday")}
    want = ["30d", "90d"] if args.range == "both" else [args.range]

    data = {
        "slug": slug,
        "name": cfg.get("name", slug),
        "market": cfg.get("market"),
        "property_id": pid,
        "subject": subject,
        "notes": cfg.get("notes") or [],
        "filters": {"exclude_countries": list(EXCLUDE_COUNTRIES)},
        "funnel_range": args.funnel_range,
        "ranges": {},
    }
    for rng in want:
        data["ranges"][rng] = build_overview(client, pid, *ranges[rng])

    s, e = "90daysAgo", "yesterday"
    data["device"] = build_breakdown(client, pid, "deviceCategory", s, e)
    data["channels"] = build_breakdown(client, pid, "sessionDefaultChannelGroup", s, e)
    data["monthly"] = build_monthly(client, pid)
    data["top_landing_pages"] = build_top_pages(client, pid, s, e)
    data["browser_by_device"] = build_browser_by_device(client, pid, s, e)
    data["sources"] = build_sources(client, pid, s, e)
    data["funnel"] = build_funnel(client, pid, *ranges[args.funnel_range])

    exports = Path(args.out) if args.out else Path("clients") / slug / "exports" / f"ga4-{date.today().isoformat()}"
    exports.mkdir(parents=True, exist_ok=True)
    data["exports_dir"] = str(exports)
    (exports / "ga4_pull.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    for name, key in [("device", "device"), ("channels", "channels"), ("monthly", "monthly"),
                      ("top_landing_pages", "top_landing_pages"),
                      ("browser_by_device", "browser_by_device"),
                      ("sources", "sources"), ("funnel", "funnel")]:
        write_csv(exports / f"ga4_{name}.csv", data[key])

    print(md_digest(data) if args.md else json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
