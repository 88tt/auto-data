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
import time
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
    "Swim,Skate - ,Ski/Snowboard - ,Sports -,Hobbies,Arts -,Leadership,CampTO,Early Years,FitnessTO,After School,Adapted Activities,Adapted Virtual",
).strip().split(",")
ACTIVITIES = [a.strip() for a in ACTIVITIES if a.strip()]
# Single filter (optional): if set, only this filter is used instead of looping ACTIVITIES.
ACTIVITY_FILTER = os.environ.get("ACTIVITY_FILTER", "")
# Matrix group label (e.g. "group-a"), only set in CI -- used to give this
# run's skipped_seasons.json a unique filename. All 3 scrape matrix groups
# compute the SAME date-based output_dir on the same day, so a fixed
# filename would let whichever group's upload merges last silently clobber
# the others' skip reports.
GROUP = os.environ.get("GROUP", "default")
# Site slug: path segment in Active Communities URL (e.g. toronto, vaughan).
SITE_SLUG = os.environ.get("SITE_SLUG", "toronto")
# Season names to scrape — matched against the WHEN panel labels at runtime so IDs
# don't need to be updated each year.  Substring match (case-insensitive).
SEASON_NAMES = [
    s.strip()
    for s in os.environ.get(
        "SEASON_NAMES",
        "Fall 2026,CampTO 2026,Leadership 2026,ARC 2026/2027",
    ).split(",")
    if s.strip()
]
# SEASON_IDS: explicit override — skips name lookup. Use for testing or when a
# season name is ambiguous.  Takes precedence over SEASON_NAMES if set.
_season_ids_env = os.environ.get("SEASON_IDS", "").strip()
SEASON_IDS_OVERRIDE = [s.strip() for s in _season_ids_env.split(",") if s.strip()]

ENCODING = "utf-8"
MAX_SCRAPE_RETRIES = 3          # per-activity retries when parsed count < header count
SCRAPE_RETRY_WAIT = 8           # seconds to wait between retries


def _activity_folder_name(activity_name: str) -> str:
    return activity_name.replace("/", "-")


def _already_scraped(output_dir: str, activity_name: str, season_id: str) -> bool:
    """Return True if this (activity, season_id) pair has already been scraped."""
    counts_path = os.path.join(output_dir, _activity_folder_name(activity_name), "scrape_counts.json")
    if not os.path.isfile(counts_path):
        return False
    try:
        data = json.load(open(counts_path))
        scraped = data.get("scraped_seasons", [str(data["season_id"])] if "season_id" in data else [])
        return str(season_id) in [str(s) for s in scraped]
    except Exception:
        return False


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

    _, driver = su.initiate_and_get_all_activities(SITE_SLUG)
    try:
        if SEASON_IDS_OVERRIDE:
            season_ids = SEASON_IDS_OVERRIDE
            print(f"Using explicit SEASON_IDS override: {season_ids}")
        else:
            season_map = su.get_season_id_map(driver, SITE_SLUG)
            print(f"Available seasons on site: {season_map}")
            season_ids = []
            for name in SEASON_NAMES:
                matches = {k: v for k, v in season_map.items() if name.lower() in k.lower()}
                if matches:
                    season_ids.extend(matches.values())
                    print(f"  Resolved {name!r} → {matches}")
                else:
                    print(f"  Warning: no season found matching {name!r} — skipping")
            if not season_ids:
                print("No seasons resolved. Check SEASON_NAMES matches labels in the WHEN panel.", file=sys.stderr)
                sys.exit(1)

        print(f"City: {CITY}, site: {SITE_SLUG}, seasons: {season_ids}, output: {output_dir}")
        print(f"Activity filters: {filters_to_use}")
        mismatches: list[tuple[str, str, int, int]] = []  # (activity, season_id, expected, got)
        skipped_seasons: list[tuple[str, str]] = []  # (season_id, error message)
        scrape_errors: list[tuple[str, str, str]] = []  # (activity, season_id, error message)

        for season_id in season_ids:
            season_url = (
                f"https://anc.ca.apm.activecommunities.com/{SITE_SLUG}/activity/search"
                f"?onlineSiteId=0&season_ids={season_id}&viewMode=list"
            )
            print(f"\n--- Season {season_id} ---")

            # Discover activities available for this season from the panel itself.
            # Real failure (2026-07-25): this call had no error handling at all,
            # unlike the per-activity scrape loop below (see its own `except
            # Exception as e:`) -- a single season whose Activities filter panel
            # doesn't behave as expected (timed out waiting for checkboxes to
            # render after clicking the filter button; season 22, "ARC
            # 2026/2027") crashed the ENTIRE run, discarding whatever seasons
            # were still queued after it even though they have nothing to do
            # with whatever went wrong on this one. Same "log it, skip this
            # unit, keep going" resilience the per-activity loop already has,
            # just one level up.
            try:
                available = su.get_activities_for_season(driver, season_url)
            except Exception as e:
                err_type = type(e).__name__
                err_msg = str(e).strip() if str(e).strip() else "<empty message>"
                print(
                    f"  Error opening activities panel for season {season_id}: [{err_type}] {err_msg} -- skipping this season.",
                    file=sys.stderr,
                )
                print(traceback.format_exc(), file=sys.stderr)
                skipped_seasons.append((season_id, f"[{err_type}] {err_msg}"))
                continue
            print(f"  {len(available)} activities in panel for season {season_id}")

            # Keep only those matching at least one of our activity filters
            matched = [a for a in available if any(f in a for f in filters_to_use)]
            to_scrape = [a for a in matched if not _already_scraped(output_dir, a, season_id)]

            if not to_scrape:
                already_done = len(matched) - len(to_scrape)
                print(f"  Nothing to scrape: {len(matched)} matched filters, {already_done} already done.")
                continue

            print(f"  Scraping {len(to_scrape)}/{len(matched)} matched activities: {to_scrape}")

            for activity_name in to_scrape:
                folder = os.path.join(output_dir, _activity_folder_name(activity_name))
                os.makedirs(folder, exist_ok=True)

                try:
                    best_df: pd.DataFrame | None = None
                    best_dfdesc: pd.DataFrame | None = None
                    best_n = 0
                    header_total_final = 0
                    header_total_initial: int | None = None
                    activity_in_panel = True

                    for attempt in range(1, MAX_SCRAPE_RETRIES + 1):
                        raw_initial = su.choose_activity_by_filter_checkbox(
                            driver, activity_name, season_url
                        )
                        if raw_initial is None:
                            print(f"    [{season_id}] {activity_name}: not in panel, skipping.")
                            if not os.listdir(folder):
                                os.rmdir(folder)
                            activity_in_panel = False
                            break

                        if attempt == 1:
                            header_total_initial = _parse_total_courses(raw_initial)

                        # Real failure (2026-08-20): an exception raised anywhere in this
                        # block (e.g. click_view_more_until_exhausted hitting a stale/
                        # vanished "View more" button) used to escape the retry loop
                        # entirely and land in the outer `except Exception` below, which
                        # abandons the WHOLE activity after a single attempt -- unlike the
                        # count-mismatch case just below, which gets MAX_SCRAPE_RETRIES
                        # tries. Give mid-scrape exceptions the same retry treatment;
                        # only give up (re-raise to the outer handler) once retries are
                        # exhausted.
                        try:
                            su.click_view_more_until_exhausted(driver)
                            df_attempt = su.get_course_info(driver)
                            dfdesc_attempt = su.get_course_description(driver, df_attempt)
                            header_total_final = _header_total_from_page_source(driver.page_source)
                            n_attempt = df_attempt.shape[0]
                        except Exception as attempt_exc:
                            if attempt < MAX_SCRAPE_RETRIES:
                                print(
                                    f"    Attempt {attempt}/{MAX_SCRAPE_RETRIES} failed for "
                                    f"{activity_name} (season {season_id}): "
                                    f"[{type(attempt_exc).__name__}] {attempt_exc}. "
                                    f"Retrying in {SCRAPE_RETRY_WAIT}s…"
                                )
                                time.sleep(SCRAPE_RETRY_WAIT)
                                continue
                            raise

                        if n_attempt > best_n:
                            best_n = n_attempt
                            best_df = df_attempt
                            best_dfdesc = dfdesc_attempt

                        if header_total_final == n_attempt:
                            print(f"    OK. {header_total_final} found for {activity_name}; {n_attempt} saved.")
                            break
                        elif attempt < MAX_SCRAPE_RETRIES:
                            print(
                                f"    MISMATCH attempt {attempt}/{MAX_SCRAPE_RETRIES}. "
                                f"Header said {header_total_final}, parsed {n_attempt} for {activity_name}. "
                                f"Retrying in {SCRAPE_RETRY_WAIT}s…"
                            )
                            time.sleep(SCRAPE_RETRY_WAIT)
                        else:
                            mismatches.append((activity_name, season_id, header_total_final, best_n))
                            print(
                                f"    MISMATCH. Header said {header_total_final}, parsed {best_n} for "
                                f"{activity_name} (season {season_id}) after {MAX_SCRAPE_RETRIES} attempts."
                            )

                    if not activity_in_panel:
                        continue

                    df = best_df
                    dfdesc = best_dfdesc
                    n = best_n

                    if header_total_initial is not None and header_total_initial != header_total_final:
                        print(
                            f"    Note. Header drifted during scrape for {activity_name}: "
                            f"{header_total_initial} -> {header_total_final}."
                        )

                    # Load existing scrape tracking (if activity was scraped in a prior season)
                    counts_path = os.path.join(folder, "scrape_counts.json")
                    try:
                        existing_counts = json.load(open(counts_path))
                        scraped_seasons = existing_counts.get("scraped_seasons", [str(existing_counts.get("season_id", ""))])
                    except Exception:
                        scraped_seasons = []
                    scraped_seasons = [s for s in scraped_seasons if s]
                    if str(season_id) not in scraped_seasons:
                        scraped_seasons.append(str(season_id))

                    with open(counts_path, "w", encoding=ENCODING) as f:
                        json.dump(
                            {
                                "scraped_seasons": scraped_seasons,
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

                    # Append if folder already has data from a previous season
                    courses_path = os.path.join(folder, "courses.csv")
                    if os.path.isfile(courses_path):
                        existing = pd.read_csv(courses_path, encoding=ENCODING)
                        df = pd.concat([existing, df], ignore_index=True).drop_duplicates()
                    df.to_csv(courses_path, index=False, encoding=ENCODING)

                    desc_path = os.path.join(folder, "descriptions.csv")
                    if os.path.isfile(desc_path):
                        existing_desc = pd.read_csv(desc_path, encoding=ENCODING)
                        dfdesc = pd.concat([existing_desc, dfdesc], ignore_index=True).drop_duplicates(subset=["Name"])
                    dfdesc.to_csv(desc_path, index=False, encoding=ENCODING)

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
                    # Real failure (2026-08-20): this activity produced NO courses.csv
                    # today (CampTO Extended Hours, season 24) and previously nothing
                    # forced the run to fail, so no notification ever fired -- the gap
                    # was only found by visually checking kebu.ca. Collected here (like
                    # mismatches below) so the run keeps scraping everything else, but
                    # still fails at the end if any activity is left with missing data.
                    scrape_errors.append((activity_name, season_id, f"[{err_type}] {err_msg}"))
                    continue

        print("\nDone.")
        # Written unconditionally (even when empty) so the export job's step
        # summary can tell "checked this group, found no skips" apart from
        # "this file never showed up at all" -- same convention as
        # notes_extraction_attempted elsewhere in this pipeline family.
        # Previously this was ONLY printed to stderr, buried in per-group scrape
        # logs the daily step summary never surfaced -- confirmed via a live
        # occurrence (2026-08-10, group-a, season 24/CampTO 2026 timed out) that
        # nobody would notice without manually digging through raw job logs.
        skipped_path = os.path.join(output_dir, f"skipped_seasons_{GROUP}.json")
        try:
            with open(skipped_path, "w") as f:
                json.dump(
                    [{"season_id": sid, "error": msg} for sid, msg in skipped_seasons],
                    f,
                    indent=2,
                )
        except Exception as write_err:
            print(f"Warning: could not write {skipped_path}: {write_err}", file=sys.stderr)
        if skipped_seasons:
            # Deliberately NOT sys.exit(1) here, unlike the mismatches/csv_count
            # checks below -- the whole point of catching this per-season instead
            # of letting it crash the run is that a season's activities panel
            # failing to open doesn't mean anything about whether OTHER seasons'
            # data (already scraped and saved to disk by this point) is any
            # good. Still printed clearly, not just buried in per-season output,
            # so a persistently-broken season doesn't go unnoticed indefinitely.
            print(f"\nSKIPPED SEASONS — {len(skipped_seasons)} season(s) could not be opened:", file=sys.stderr)
            for sid, msg in skipped_seasons:
                print(f"  [{sid}] {msg}", file=sys.stderr)
        if scrape_errors:
            # Unlike skipped_seasons above, a scrape error means an activity that WAS
            # attempted came back with nothing (not just "couldn't check this season").
            # That's a real gap in what ships to kebu.ca, so this fails the run --
            # deliberately unlike skipped_seasons, which doesn't say anything about
            # whether other seasons' data is any good.
            print(f"\nSCRAPE ERRORS — {len(scrape_errors)} activit(ies) produced no data:", file=sys.stderr)
            for activity, sid, msg in scrape_errors:
                print(f"  [{sid}] {activity}: {msg}", file=sys.stderr)
        if mismatches:
            print(f"\nSCRAPE COUNT MISMATCH — {len(mismatches)} activit(ies) did not parse all sessions:", file=sys.stderr)
            for activity, sid, expected, got in mismatches:
                print(f"  [{sid}] {activity}: expected {expected}, got {got} (missing {expected - got})", file=sys.stderr)
        if scrape_errors or mismatches:
            sys.exit(1)

        csv_count = sum(
            1 for name in os.listdir(output_dir)
            if os.path.isfile(os.path.join(output_dir, name, "courses.csv"))
        )
        if csv_count == 0:
            print(f"\nSCRAPE FAILED: 0 activities produced courses.csv in {output_dir}. Check errors above.", file=sys.stderr)
            sys.exit(1)
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
