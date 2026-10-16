#!/usr/bin/env python3
# Copy of tools/ga4_site_search.py in the Claude Code Playground workspace (CP). Change both together.
"""ga4_site_search.py - on-site search terms and what they lead to, for one brand.

Data acquisition, not analysis. From GA4, per search term: visits that searched it, product
opens from it, and basket adds from it, plus the products each term opened. Writes a dated
snapshot to <out>/ga4-site-search/<YYYY-MM-DD>/site_search.json with a manifest.

How a product open is tied to a term (Shopify storefront URLs):
  results page  view_item whose page referrer is the site's own /search?q=<term> page
  dropdown      view_item whose URL carries _psq=<typed text> and whose referrer is the site
Both tags travel with shared links (Instagram, WhatsApp), so the referrer must be the site's
own host; a tagged view arriving from elsewhere is not a search. Google's /search referrer is
excluded the same way. Basket adds use the same two routes, plus adds made on the results
page itself (quick add), where the term comes from the page URL.

Every figure is GA4 sessions: paging through results re-fires `search`, so events overcount.
A visit that searched two spellings of a term that normalise the same counts twice.

Usage:
  python3 tools/ga4_site_search.py --brand stackedstore-my --exclude-country China --out clients/stackedstore-my/exports
"""
import argparse
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote_plus, urlsplit
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ga4_pull as g  # noqa: E402
from shopify_common import ROOT, Snapshot, die, load_brand, write_json  # noqa: E402

TOOL_VERSION = "1.0.0"
PAGE = 100000


def F():
    from google.analytics.data_v1beta import types
    return types


def f_in(field, values):
    t = F()
    return t.FilterExpression(filter=t.Filter(field_name=field, in_list_filter=t.Filter.InListFilter(values=values)))


def f_str(field, value, match):
    t = F()
    mt = getattr(t.Filter.StringFilter.MatchType, match)
    return t.FilterExpression(filter=t.Filter(field_name=field, string_filter=t.Filter.StringFilter(match_type=mt, value=value)))


def AND(*e):
    t = F()
    return t.FilterExpression(and_group=t.FilterExpressionList(expressions=list(e)))


class Puller:
    def __init__(self, client, pid, since, until, exclude):
        self.client, self.pid, self.since, self.until = client, pid, since, until
        self.exclude = exclude
        self.thresholded = {}

    def report(self, name, dims, mets, expr=None):
        t = F()
        exprs = [e for e in [expr] if e is not None]
        if self.exclude:
            exprs.append(t.FilterExpression(not_expression=f_in("country", self.exclude)))
        rows, offset = [], 0
        while True:
            req = t.RunReportRequest(property=f"properties/{self.pid}", dimensions=[t.Dimension(name=d) for d in dims],
                                     metrics=[t.Metric(name=m) for m in mets],
                                     date_ranges=[t.DateRange(start_date=self.since, end_date=self.until)],
                                     limit=PAGE, offset=offset)
            if exprs:
                req.dimension_filter = exprs[0] if len(exprs) == 1 else AND(*exprs)
            r = self.client.run_report(req)
            self.thresholded[name] = self.thresholded.get(name, False) or bool(r.metadata.subject_to_thresholding)
            rows += [{**{d: x.dimension_values[i].value for i, d in enumerate(dims)},
                      **{m: int(float(x.metric_values[j].value)) for j, m in enumerate(mets)}} for x in r.rows]
            offset += PAGE
            if offset >= r.row_count:
                return rows


def norm(term):
    return re.sub(r"\s+", " ", unquote_plus(term or "")).strip().lower()


def handle_of(path):
    tail = urlsplit(path).path.rsplit("/products/", 1)
    return tail[1].strip("/").split("/")[0] if len(tail) == 2 else None


def q_of(url, key):
    v = parse_qs(urlsplit(url).query).get(key)
    return norm(v[0]) if v else ""


def property_timezone(creds, pid):
    from google.analytics.admin_v1beta import AnalyticsAdminServiceClient
    return AnalyticsAdminServiceClient(credentials=creds).get_property(name=f"properties/{pid}").time_zone


def main():
    ap = argparse.ArgumentParser(description="Pull on-site search terms from GA4 for one brand.")
    ap.add_argument("--brand", required=True, help="registry slug, e.g. stackedstore-my")
    ap.add_argument("--since", help="YYYY-MM-DD (default: 30 days ending yesterday, property timezone)")
    ap.add_argument("--until", help="YYYY-MM-DD (default: yesterday, property timezone)")
    ap.add_argument("--exclude-country", action="append", default=[], metavar="COUNTRY",
                    help="drop sessions from this GA4 country in every report (repeatable), e.g. China")
    ap.add_argument("--date", help="snapshot folder date YYYY-MM-DD (default: today UTC)")
    ap.add_argument("--out", help="exports dir; the snapshot lands at <out>/ga4-site-search/<date>/ "
                                  "(default: clients/<slug>/exports)")
    args = ap.parse_args()

    cfg = load_brand(args.brand)
    ga = cfg.get("ga4") or {}
    pid, subject, domain = ga.get("property_id"), ga.get("subject"), cfg.get("site_domain")
    if not pid or not subject:
        die(f"config/brands/{cfg['slug']}.yaml: ga4.property_id and ga4.subject are required.")
    if not domain:
        die(f"config/brands/{cfg['slug']}.yaml: site_domain is required (the storefront host, e.g. example.com).")
    out_base = Path(args.out) if args.out else ROOT / "clients" / cfg["slug"] / "exports"

    creds = g.get_credentials(g.load_env(str(ROOT / ".env")), subject)
    client = g.make_data_client(creds)
    tz = property_timezone(creds, pid)
    yesterday = datetime.now(ZoneInfo(tz)).date() - timedelta(days=1)
    until = args.until or str(yesterday)
    since = args.since or str(datetime.fromisoformat(until).date() - timedelta(days=29))
    p = Puller(client, pid, since, until, args.exclude_country)

    host = r"https?://(www\.)?" + re.escape(domain)
    on_site = f_str("pageReferrer", host + r"/.*", "FULL_REGEXP")
    search_ref = f_str("pageReferrer", host + r"/([^/?]+/)?search\?.*", "FULL_REGEXP")
    on_product = f_str("pagePath", "/products/", "CONTAINS")
    has_psq = f_str("pagePathPlusQueryString", "_psq=", "CONTAINS")
    on_search_page = f_str("pagePath", r"/([^/]+/)?search", "FULL_REGEXP")

    total_sessions = p.report("total_sessions", [], ["sessions", "totalUsers"])
    search_sessions = p.report("search_sessions", [], ["sessions", "totalUsers"], f_in("eventName", ["search"]))
    by_term = p.report("search_by_term", ["searchTerm"], ["sessions", "totalUsers"], f_in("eventName", ["search"]))
    views_results = p.report("views_results", ["pagePath", "pageReferrer"], ["sessions"],
                             AND(f_in("eventName", ["view_item"]), on_product, search_ref))
    views_dropdown = p.report("views_dropdown", ["pagePathPlusQueryString"], ["sessions"],
                              AND(f_in("eventName", ["view_item"]), has_psq, on_site))
    adds_results = p.report("adds_results", ["pagePath", "pageReferrer"], ["sessions"],
                            AND(f_in("eventName", ["add_to_cart"]), on_product, search_ref))
    adds_dropdown = p.report("adds_dropdown", ["pagePathPlusQueryString"], ["sessions"],
                             AND(f_in("eventName", ["add_to_cart"]), has_psq, on_site))
    adds_on_results = p.report("adds_on_results_page", ["pagePathPlusQueryString"], ["sessions"],
                               AND(f_in("eventName", ["add_to_cart"]), on_search_page))

    terms = defaultdict(lambda: {"searches": 0, "users": 0, "opens_results": 0, "opens_dropdown": 0,
                                 "adds": 0, "products": defaultdict(int)})
    for r in by_term:
        t = norm(r["searchTerm"])
        if t and t != "(not set)":
            terms[t]["searches"] += r["sessions"]
            terms[t]["users"] += r["totalUsers"]
    for r in views_results:
        t, h = q_of(r["pageReferrer"], "q"), handle_of(r["pagePath"])
        if t and h:
            terms[t]["opens_results"] += r["sessions"]
            terms[t]["products"][h] += r["sessions"]
    for r in views_dropdown:
        url = r["pagePathPlusQueryString"]
        t, h = q_of(url, "_psq"), handle_of(url)
        if t and h:
            terms[t]["opens_dropdown"] += r["sessions"]
            terms[t]["products"][h] += r["sessions"]
    for rows, key, field in ((adds_results, "pageReferrer", "q"), (adds_dropdown, "pagePathPlusQueryString", "_psq"),
                             (adds_on_results, "pagePathPlusQueryString", "q")):
        for r in rows:
            t = q_of(r[key], field)
            if t:
                terms[t]["adds"] += r["sessions"]

    term_rows = []
    for t, v in terms.items():
        prods = sorted(v["products"].items(), key=lambda kv: -kv[1])
        term_rows.append({"term": t, "searches": v["searches"], "users": v["users"],
                          "opens": v["opens_results"] + v["opens_dropdown"],
                          "opens_results": v["opens_results"], "opens_dropdown": v["opens_dropdown"],
                          "adds": v["adds"], "products": [{"handle": h, "opens": n} for h, n in prods]})
    term_rows.sort(key=lambda r: (-r["searches"], -r["opens"], r["term"]))

    totals = {
        "sessions": total_sessions[0]["sessions"] if total_sessions else 0,
        "search_sessions": search_sessions[0]["sessions"] if search_sessions else 0,
        "users": total_sessions[0]["totalUsers"] if total_sessions else 0,
        "search_users": search_sessions[0]["totalUsers"] if search_sessions else 0,
        "terms_searched": sum(1 for r in term_rows if r["searches"] > 0),
        "searches_by_term_sum": sum(r["searches"] for r in term_rows),
        "opens": sum(r["opens"] for r in term_rows),
        "adds": sum(r["adds"] for r in term_rows),
    }
    definitions = {
        "searches": "sessions with a `search` event for the term (results page loads)",
        "opens": "sessions that opened the product from the term: results page (referrer is the site's "
                 "/search?q=) or dropdown (_psq= in the URL, referrer on the site); summed over products",
        "adds": "sessions with add_to_cart from the term by the same two routes, or on the results page itself",
        "term": "URL-decoded, lowercased, whitespace collapsed",
        "search_sessions": "distinct sessions with any search; lower than searches_by_term_sum because a "
                           "visit can search several terms",
        "search_users": "GA4 totalUsers with any search in the window, against users for all traffic",
    }
    data = {
        "pulled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "property": pid, "timezone": tz, "site_domain": domain,
        "window": {"since": since, "until": until},
        "filters": {"exclude_countries": args.exclude_country},
        "thresholded": p.thresholded,
        "definitions": definitions,
        "totals": totals,
        "terms": term_rows,
    }
    with Snapshot(out_base, "ga4-site-search", args.date) as out:
        write_json(out / "site_search.json", data)
        write_json(out / "manifest.json", {
            "pulled_at": data["pulled_at"], "tool": "ga4_site_search.py", "tool_version": TOOL_VERSION,
            "brand": cfg["slug"], "property": pid, "timezone": tz, "window": data["window"],
            "filters": data["filters"], "thresholded": p.thresholded, "totals": totals,
            "definitions": definitions, "files": ["site_search.json"],
        })
    final = Snapshot(out_base, "ga4-site-search", args.date).final
    flag = " THRESHOLDED: " + ", ".join(k for k, v in p.thresholded.items() if v) if any(p.thresholded.values()) else ""
    print(f"OK {cfg['slug']} {since} to {until}: {totals['search_sessions']:,} search visits of "
          f"{totals['sessions']:,}, {totals['terms_searched']:,} terms, {totals['opens']:,} product opens, "
          f"{totals['adds']:,} basket adds{flag} -> {final}")


if __name__ == "__main__":
    main()
