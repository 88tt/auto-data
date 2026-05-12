"""
Usage:
  python 1_scrape_w_selenium.py
  # Different activity: ACTIVITY_FILTER="Swim" python 1_scrape_w_selenium.py
  # Other city: CITY=Va SITE_SLUG=vaughan python 1_scrape_w_selenium.py
  # Multiple seasons: SEASON_IDS="21,23,14" python 1_scrape_w_selenium.py

"""
import json
import os
import re
import sys
import traceback
from datetime import datetime

import pandas as pd
from bs4 import BeautifulSoup

import config
import spider_utils as su

# ---------------------------------------------------------------------------
# Config: shared from config.py; scraper-specific below (or set env vars)
# ---------------------------------------------------------------------------
CITY = config.CITY
YEAR_AND_SEASON = config.YEAR_AND_SEASON
RAW_DATA_DIR = config.RAW_DATA_DIR
season = config.season
pk_prefix = config.pk_prefix

# Activities to scrape: loop through each (substring match in program name).
# Override with env ACTIVITIES="CampTO,Swim" (comma-separated) or ACTIVITY_FILTER="CampTO" for a single filter.
ACTIVITIES = os.environ.get(
    "ACTIVITIES",
    "Swim,Skate - ,Ski/Snowboard - ,Sports -,Hobbies,Arts -,Leadership,CampTO,Early Years,FitnessTO,After School,Adapted Activities",
).strip().split(",")
ACTIVITIES = [a.strip() for a in ACTIVITIES if a.strip()]
# Single filter (optional): if set, only this filter is used instead of looping ACTIVITIES.
ACTIVITY_FILTER = os.environ.get("ACTIVITY_FILTER", "")
# Site slug: path segment in Active Communities URL (e.g. toronto, vaughan).
SITE_SLUG = os.environ.get("SITE_SLUG", "toronto")
# Season IDs for the WHEN filter — comma-separated list to scrape multiple seasons in one run.
# Known Toronto IDs: 21=Summer 2026, 23=Leadership 2026, 14=ARC 2025/2026.
# SEASON_ID (singular) is kept for backward compat; SEASON_IDS takes precedence if set.
SEASON_ID = os.environ.get("SEASON_ID", "21")
_season_ids_env = os.environ.get("SEASON_IDS", "").strip()
SEASON_IDS = [s.strip() for s in _season_ids_env.split(",") if s.strip()] if _season_ids_env else [SEASON_ID]

ENCODING = "utf-8"


def _activity_folder_name(activity_name: str, season_id: str, multi_season: bool) -> str:
    """Return the folder name for this activity.  Prefixed with s{id}_ when scraping multiple seasons."""
    safe = activity_name.replace("/", "-")
    return f"s{season_id}_{safe}" if multi_season else safe


def _already_scraped(output_dir: str, activity_name: str, season_id: str, multi_season: bool) -> bool:
    """Skip only if we have a completed courses.csv for this activity/season combo."""
    folder = _activity_folder_name(activity_name, season_id, multi_season)
    return os.path.isfile(os.path.join(output_dir, folder, "courses.csv"))


def _parse_total_courses(raw: str) -> int:
    """Parse header count; may be '1,234' or '0'."""
    if not raw or not str(raw).strip():
        return 0
    return int(re.sub(r"[^\d]", "", str(raw)) or 0)


def _header_total_from_page_source(html: str) -> int:
    """Read current results-header total directly from page source."""
    if not html:
        return 0
    soup = BeautifulSoup(html, "html.parser")
    total = soup.select_one(".activity-results-header__total b")
    if total and total.get_text(strip=True):
        return _parse_total_courses(total.get_text(strip=True))
    return 0


def main():
    os.makedirs(RAW_DATA_DIR, exist_ok=True)
    scrape_run = "scraped" + datetime.now().strftime("%Y%m%d")
    output_dir = os.path.join(RAW_DATA_DIR, scrape_run)
    os.makedirs(output_dir, exist_ok=True)

    # Which filters to use: one (ACTIVITY_FILTER) or loop through ACTIVITIES
    filters_to_use = [ACTIVITY_FILTER] if ACTIVITY_FILTER.strip() else ACTIVITIES
    if not filters_to_use:
        print("No activity filters configured (ACTIVITIES or ACTIVITY_FILTER).")
        return

    multi_season = len(SEASON_IDS) > 1
    programs, driver = su.initiate_and_get_all_activities(SITE_SLUG)
    try:
        print(f"City: {CITY}, site: {SITE_SLUG}, seasons: {SEASON_IDS}, output: {output_dir}")
        print(f"Looping through {len(filters_to_use)} activity filter(s): {filters_to_use}")

        for season_id in SEASON_IDS:
            season_url = (
                f"https://anc.ca.apm.activecommunities.com/{SITE_SLUG}/activity/search"
                f"?onlineSiteId=0&season_ids={season_id}&viewMode=list"
            )
            print(f"\n--- Season {season_id} ---")

            for activity_filter in filters_to_use:
                all_crs = [f for f in programs if activity_filter in f]
                to_scrape = [
                    f for f in all_crs
                    if not _already_scraped(output_dir, f, season_id, multi_season)
                ]

                if not to_scrape:
                    print(f"  [{activity_filter}] Nothing to scrape: all {len(all_crs)} matching already done.")
                    continue

                print(f"  [{activity_filter}] Scraping {len(to_scrape)} activities: {to_scrape}")

                for activity_name in to_scrape:
                    folder_name = _activity_folder_name(activity_name, season_id, multi_season)
                    folder = os.path.join(output_dir, folder_name)
                    os.makedirs(folder, exist_ok=True)

                    try:
                        header_total_initial = su.choose_activity_by_filter_checkbox(
                            driver, activity_name, season_url
                        )
                        if header_total_initial is None:
                            print(f"    [{season_id}] {activity_name}: not offered this season, skipping.")
                            os.rmdir(folder)  # remove the empty dir we just created
                            continue
                        header_total_initial = _parse_total_courses(header_total_initial)

                        df = su.get_course_info(driver)
                        dfdesc = su.get_course_description(driver, df)
                        su.click_view_more_until_exhausted(driver)
                        df = su.get_course_info(driver)  # Re-parse after tooltips: DOM can have more cards
                        header_total_final = _header_total_from_page_source(driver.page_source)

                        n = df.shape[0]
                        if header_total_final == n:
                            print(f"    OK. {header_total_final} found for {activity_name}; {n} saved.")
                        else:
                            print(f"    Warning. Header said {header_total_final}, parsed {n} for {activity_name}.")
                        if header_total_initial != header_total_final:
                            print(
                                f"    Note. Header drifted during scrape for {activity_name}: "
                                f"{header_total_initial} -> {header_total_final}."
                            )

                        counts_path = os.path.join(folder, "scrape_counts.json")
                        with open(counts_path, "w", encoding=ENCODING) as f:
                            json.dump(
                                {
                                    "season_id": season_id,
                                    "header_total": header_total_final,
                                    "header_total_initial": header_total_initial,
                                    "header_total_final": header_total_final,
                                    "parsed": n,
                                    "header_drift": header_total_initial != header_total_final,
                                },
                                f,
                                indent=2,
                            )

                        with open(os.path.join(folder, "page_source.txt"), "w", encoding=ENCODING) as f:
                            f.write(BeautifulSoup(driver.page_source, "html.parser").prettify())

                        df.to_csv(os.path.join(folder, "courses.csv"), index=False, encoding=ENCODING)
                        dfdesc.to_csv(os.path.join(folder, "descriptions.csv"), index=False, encoding=ENCODING)

                    except Exception as e:
                        err_type = type(e).__name__
                        err_msg = str(e).strip() if str(e).strip() else "<empty message>"
                        err_tb = traceback.format_exc()
                        print(
                            f"    Error scraping {activity_name} (season {season_id}): [{err_type}] {err_msg}",
                            file=sys.stderr,
                        )
                        print(err_tb, file=sys.stderr)
                        err_path = os.path.join(folder, "scrape_error.json")
                        try:
                            with open(err_path, "w", encoding=ENCODING) as ef:
                                json.dump(
                                    {
                                        "season_id": season_id,
                                        "activity_name": activity_name,
                                        "error_type": err_type,
                                        "error_message": err_msg,
                                        "error_repr": repr(e),
                                        "traceback": err_tb,
                                        "captured_at": datetime.now().isoformat(),
                                    },
                                    ef,
                                    indent=2,
                                )
                        except Exception as write_err:
                            print(
                                f"    Warning: could not write {err_path}: {write_err}",
                                file=sys.stderr,
                            )
                        continue

        print("\nDone.")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
