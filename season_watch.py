"""
Flag seasons on the City's registration site that this pipeline doesn't scrape.

SEASON_NAMES (daily-ingest.yml / 1_scrape_w_selenium.py) is a fixed list, so a
season the City adds later -- "Spring 2027", "CampTO 2027", "ARC 2027/2028" --
is silently skipped until someone edits it. That's how "Leadership 2027" went
unscraped: the run succeeded, just without it.

Reads available_seasons_*.json (the scraper writes the WHEN panel's full
{label: id} map there each run) and, for every season that no SEASON_NAMES
entry matches and that isn't in IGNORED_SEASONS below, asks the City's search
API how many courses it lists. Any such season that still has courses fails
this script (exit 1), naming it -- the season-watch job turns that into a
failed run + GitHub's failure email, without blocking the day's export.

Fix an alert by either adding the season to SEASON_NAMES (to scrape it) or to
IGNORED_SEASONS with a reason (to deliberately skip it). Seasons with zero
courses (expired) never alert.

Standard library only, so the job doesn't need requirements.txt.
"""
import glob
import json
import os
import sys
import urllib.request

# Exact WHEN-panel labels we deliberately don't scrape, with why.
IGNORED_SEASONS = {
    "Fall 2026": "Previous season -- kebu-lite moved to Winter 2027 (2027s1To) on 2026-11-17.",
    "Summer 2026": "Only Riverlea garden allotment waitlists remain (not programs).",
}

SITE_SLUG = os.environ.get("SITE_SLUG", "toronto")
LIST_URL = f"https://anc.ca.apm.activecommunities.com/{SITE_SLUG}/rest/activities/list?locale=en-US"


def course_count(season_id):
    """(total courses, sample date range) for one season, or (None, error)."""
    body = json.dumps({
        "activity_search_pattern": {"season_ids": [str(season_id)], "activity_select_param": 2},
        "activity_transfer_pattern": {},
    }).encode()
    req = urllib.request.Request(LIST_URL, data=body, method="POST", headers={
        "User-Agent": "Mozilla/5.0",
        "Content-Type": "application/json;charset=utf-8",
        "Accept": "application/json",
        "page_info": json.dumps({"order_by": "", "page_number": 1, "total_records_per_page": 5}),
    })
    try:
        d = json.load(urllib.request.urlopen(req, timeout=60))
        items = (d.get("body") or {}).get("activity_items") or []
        sample = ", ".join(sorted({i.get("date_range") or "" for i in items if i.get("date_range")})[:2])
        return d["headers"]["page_info"]["total_records"], sample
    except Exception as exc:  # network / API shape change -- report, don't crash
        return None, f"{type(exc).__name__}: {exc}"


def main(raw_dir="raw_data"):
    paths = sorted(glob.glob(os.path.join(raw_dir, "**", "available_seasons_*.json"), recursive=True))
    if not paths:
        print("No available_seasons_*.json found -- scraper didn't record the site's seasons.")
        return 1
    saved = json.load(open(paths[-1]))
    seasons, names = saved["seasons"], saved["season_names"]

    lines, alerts = [], []
    for label, sid in sorted(seasons.items(), key=lambda kv: kv[0]):
        covered_by = [n for n in names if n.lower() in label.lower()]
        if covered_by:
            lines.append(f"| {label} | {sid} | scraped | |")
        elif label in IGNORED_SEASONS:
            lines.append(f"| {label} | {sid} | ignored | {IGNORED_SEASONS[label]} |")
        else:
            total, detail = course_count(sid)
            if total == 0:
                lines.append(f"| {label} | {sid} | not scraped, 0 courses | expired |")
            else:
                shown = "count unavailable" if total is None else f"{total} courses"
                lines.append(f"| {label} | {sid} | **NOT SCRAPED** | {shown}; {detail} |")
                alerts.append(f"{label!r} (season_id {sid}): {shown} -- {detail}")
    unmatched = [n for n in names if not any(n.lower() in label.lower() for label in seasons)]

    report = ["## Season watch", "", "| Season on City site | ID | Status | Note |", "|---|---|---|---|", *lines, ""]
    if unmatched:
        report.append(f"SEASON_NAMES entries matching no season on the site (harmless if expired): {', '.join(unmatched)}")
        report.append("")
    if alerts:
        report += ["**Seasons on the City site with courses that this pipeline does NOT scrape:**", ""]
        report += [f"- {a}" for a in alerts]
        report += ["", "Add each to SEASON_NAMES (daily-ingest.yml + 1_scrape_w_selenium.py) to scrape it, "
                       "or to IGNORED_SEASONS in season_watch.py to skip it on purpose."]
    else:
        report.append("Every season on the City site with courses is scraped or deliberately ignored.")
    text = "\n".join(report)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(text + "\n")
    return 1 if alerts else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
