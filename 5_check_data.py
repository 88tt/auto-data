#!/usr/bin/env python3
"""
Data prep health: scrape (1) → clean CSV (2) → database (3).

  python 5_check_data.py           One-screen overview (default)
  python 5_check_data.py -v        Overview + integrity table + failure samples
  python 5_check_data.py --deep    -v + per-activity scrape table + artifact paths + export JSON diff

Environment: same as config.py (CITY, YEAR_AND_SEASON, RAW_DATA_DIR, DB_URL, …).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from sqlalchemy import create_engine, text

import config

pd.set_option("display.max_columns", None)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _resolve_raw_base_dir() -> tuple[str | None, list[str]]:
    """Return (base_dir, scraped_run_names) under RAW_DATA_DIR or cwd fallbacks."""
    cwd = os.getcwd()
    year_season = config.YEAR_AND_SEASON
    candidates = [
        os.path.abspath(config.RAW_DATA_DIR),
        os.path.abspath(os.path.join(cwd, "raw_data", config.CITY, year_season)),
        os.path.abspath(os.path.join(cwd, year_season)),
        os.path.abspath(os.path.join(cwd, "..", "raw_data", config.CITY, year_season)),
    ]
    base = None
    for cand in candidates:
        if os.path.isdir(cand):
            base = cand
            break
    if base is None:
        base = candidates[0]

    runs = [
        d
        for d in os.listdir(base)
        if d.startswith("scraped") and os.path.isdir(os.path.join(base, d))
    ]
    if not runs:
        parent = os.path.dirname(base)
        runs = [
            d
            for d in os.listdir(parent)
            if d.startswith("scraped") and os.path.isdir(os.path.join(parent, d))
        ]
        if runs:
            base = parent

    return base, sorted(runs)


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _session_barcode_pattern(pk_prefix: str) -> str:
    return "%" + _like_escape(pk_prefix) + "%"


# ---------------------------------------------------------------------------
# Scrape health (scrape_counts.json per activity)
# ---------------------------------------------------------------------------


@dataclass
class ScrapeHealthResult:
    base_dir: str | None = None
    latest_run: str | None = None
    prev_run: str | None = None
    df_latest: pd.DataFrame | None = None
    df_changes: pd.DataFrame | None = None
    summary_csv: str | None = None
    changes_csv: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        if self.error or self.df_latest is None or self.df_latest.empty:
            return False
        return bool(self.df_latest["match"].fillna(False).all())

    def mismatch_count(self) -> int:
        if self.df_latest is None or self.df_latest.empty:
            return 0
        return int((~self.df_latest["match"].fillna(False)).sum())

    def missing_json_count(self) -> int:
        if self.df_latest is None:
            return 0
        return int((~self.df_latest["found"].fillna(False)).sum())


def _build_scrape_counts_df(scraped_dir: str) -> pd.DataFrame:
    rows = []
    for activity_folder in os.listdir(scraped_dir):
        activity_path = os.path.join(scraped_dir, activity_folder)
        if not os.path.isdir(activity_path):
            continue
        counts_path = os.path.join(activity_path, "scrape_counts.json")
        row = {
            "activity_folder": activity_folder,
            "counts_path": counts_path,
            "found": True,
            "header_total": None,
            "parsed": None,
            "match": None,
        }
        try:
            with open(counts_path, "r", encoding="utf-8") as f:
                j = json.load(f)
            row["header_total"] = j.get("header_total")
            row["parsed"] = j.get("parsed")
            row["match"] = row["parsed"] == row["header_total"]
        except FileNotFoundError:
            row["found"] = False
            row["match"] = False
        rows.append(row)
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["activity_folder"])
    return df


def collect_scrape_health(*, write_csv: bool = True) -> ScrapeHealthResult:
    out = ScrapeHealthResult()
    base, runs = _resolve_raw_base_dir()
    out.base_dir = base

    if not runs:
        out.error = f"No scraped* folders under {base}"
        return out

    out.latest_run = runs[-1]
    out.prev_run = runs[-2] if len(runs) >= 2 else None
    latest_path = os.path.join(base, out.latest_run)
    out.df_latest = _build_scrape_counts_df(latest_path)

    if write_csv and out.df_latest is not None and not out.df_latest.empty:
        out.summary_csv = os.path.join(latest_path, "scrape_counts_summary.csv")
        out.df_latest.to_csv(out.summary_csv, index=False, encoding="utf-8")

    if write_csv and out.prev_run:
        prev_path = os.path.join(base, out.prev_run)
        df_prev = _build_scrape_counts_df(prev_path)
        df_latest_ren = out.df_latest.rename(
            columns={
                "counts_path": "counts_path_latest",
                "found": "found_latest",
                "header_total": "header_total_latest",
                "parsed": "parsed_latest",
                "match": "match_latest",
            }
        )
        df_prev_ren = df_prev.rename(
            columns={
                "counts_path": "counts_path_prev",
                "found": "found_prev",
                "header_total": "header_total_prev",
                "parsed": "parsed_prev",
                "match": "match_prev",
            }
        )
        df_changes = df_latest_ren.merge(
            df_prev_ren[
                [
                    "activity_folder",
                    "found_prev",
                    "header_total_prev",
                    "parsed_prev",
                    "match_prev",
                ]
            ],
            on="activity_folder",
            how="left",
        )
        for c in ("parsed_latest", "parsed_prev", "header_total_latest", "header_total_prev"):
            if c in df_changes.columns:
                df_changes[c] = pd.to_numeric(df_changes[c], errors="coerce")
        df_changes["parsed_delta"] = df_changes["parsed_latest"] - df_changes["parsed_prev"]
        df_changes["header_total_delta"] = (
            df_changes["header_total_latest"] - df_changes["header_total_prev"]
        )
        df_changes["match_changed"] = df_changes["match_latest"].fillna(False) != df_changes[
            "match_prev"
        ].fillna(False)
        df_changes["new_in_latest"] = df_changes["found_latest"].fillna(False) & (
            ~df_changes["found_prev"].fillna(False)
        )
        df_changes = df_changes.sort_values(
            ["match_changed", "parsed_delta"],
            ascending=[False, False],
        ).sort_values("activity_folder")
        out.df_changes = df_changes
        out.changes_csv = os.path.join(latest_path, "scrape_counts_changes_latest_vs_prev.csv")
        df_changes.to_csv(out.changes_csv, index=False, encoding="utf-8")

    return out


# ---------------------------------------------------------------------------
# DB + dfcrs integrity (aligned with 3_insert_to_db.py)
# ---------------------------------------------------------------------------


@dataclass
class IntegrityResult:
    engine_ok: bool = False
    dfcrs_path: str = ""
    dfcrs_exists: bool = False
    summary_rows: list[tuple[str, str, str]] = field(default_factory=list)
    # For verbose details
    df_orphan_centre: pd.DataFrame = field(default_factory=lambda: pd.DataFrame())
    df_orphan_course: pd.DataFrame = field(default_factory=lambda: pd.DataFrame())
    df_orphan_series: pd.DataFrame = field(default_factory=lambda: pd.DataFrame())
    df_dup_barcode: pd.DataFrame = field(default_factory=lambda: pd.DataFrame())
    only_csv: list[str] = field(default_factory=list)
    only_db: list[str] = field(default_factory=list)
    missing_centres: list[str] = field(default_factory=list)
    nulls: dict[str, Any] = field(default_factory=dict)
    sub_nrows: int = 0
    bad_av_n: int = 0
    csv_row_count: int = 0
    csv_slice_rows: int = 0
    distinct_csv_barcodes: int = 0
    distinct_db_barcodes: int = 0

    def fail_count(self) -> int:
        return sum(1 for s, _, _ in self.summary_rows if s == "!!")

    def skip_count(self) -> int:
        return sum(1 for s, _, _ in self.summary_rows if s == "--")


def collect_integrity() -> IntegrityResult:
    res = IntegrityResult()
    pk_prefix = config.pk_prefix
    cutoff = pd.Timestamp(config.STARTDATE_CUTOFF)
    pattern = _session_barcode_pattern(pk_prefix)
    dfcrs_path = os.path.join(config.season, "dfcrs.csv")
    res.dfcrs_path = dfcrs_path

    engine = None
    df_oc = pd.DataFrame()
    df_ocr = pd.DataFrame()
    df_os = pd.DataFrame()
    df_dup = pd.DataFrame()
    db_barcodes: set = set()
    only_csv: list[str] = []
    only_db: list[str] = []
    missing_centres: list[str] = []
    csv_barcodes: set = set()
    sub_nrows = 0
    nulls: dict = {}
    key_nulls = 0
    bad_av_n = 0

    try:
        # Avoid hanging when Postgres is down (default libpq wait can be very long).
        engine = create_engine(
            config.DB_URL,
            connect_args={"connect_timeout": 5},
        )
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            df_oc = pd.read_sql(
                text(
                    """
                    SELECT s.barcode, s.centre_id
                    FROM activities_sessions s
                    LEFT JOIN activities_centres c ON c.name = s.centre_id
                    WHERE s.barcode LIKE :prefix ESCAPE '\\'
                      AND (s.start_date >= :cutoff OR s.has_enroll_now = TRUE)
                      AND s.centre_id IS NOT NULL
                      AND c.name IS NULL
                    """
                ),
                con=conn,
                params={"prefix": pattern, "cutoff": cutoff},
            )
            df_ocr = pd.read_sql(
                text(
                    """
                    SELECT s.barcode, s.course_id
                    FROM activities_sessions s
                    LEFT JOIN activities_courses co ON co.name = s.course_id
                    WHERE s.barcode LIKE :prefix ESCAPE '\\'
                      AND (s.start_date >= :cutoff OR s.has_enroll_now = TRUE)
                      AND s.course_id IS NOT NULL
                      AND co.name IS NULL
                    """
                ),
                con=conn,
                params={"prefix": pattern, "cutoff": cutoff},
            )
            df_os = pd.read_sql(
                text(
                    """
                    SELECT c.name AS course_pk, c.series_id
                    FROM activities_courses c
                    LEFT JOIN activities_series ser ON ser.name = c.series_id
                    WHERE c.name LIKE :prefix ESCAPE '\\'
                      AND c.series_id IS NOT NULL
                      AND ser.name IS NULL
                    """
                ),
                con=conn,
                params={"prefix": pattern},
            )
            df_dup = pd.read_sql(
                text(
                    """
                    SELECT barcode, COUNT(*) AS cnt
                    FROM activities_sessions
                    WHERE barcode LIKE :prefix ESCAPE '\\'
                    GROUP BY barcode
                    HAVING COUNT(*) > 1
                    """
                ),
                con=conn,
                params={"prefix": pattern},
            )
            db_barcodes = set(
                pd.read_sql(
                    text(
                        """
                        SELECT barcode
                        FROM activities_sessions
                        WHERE barcode LIKE :prefix ESCAPE '\\'
                          AND (start_date >= :cutoff OR has_enroll_now = TRUE)
                        """
                    ),
                    con=conn,
                    params={"prefix": pattern, "cutoff": cutoff},
                )["barcode"].tolist()
            )
        res.engine_ok = True
    except Exception:
        engine = None
        res.engine_ok = False

    rows: list[tuple[str, str, str]] = []

    if not os.path.isfile(dfcrs_path):
        if not res.engine_ok:
            rows.extend(
                [
                    ("--", "Sessions → centres (orphan centre_id)", "no DB"),
                    ("--", "Sessions → courses (orphan course_id)", "no DB"),
                    ("--", "Courses → series (orphan series_id)", "no DB"),
                    ("--", "Duplicate session barcodes", "no DB"),
                    ("--", "Barcode set dfcrs vs DB", "no DB"),
                    ("--", "dfcrs locations missing in DB centres", "no DB"),
                ]
            )
        else:
            rows.extend(
                [
                    ("OK" if len(df_oc) == 0 else "!!", "Sessions → centres (orphan centre_id)", f"{len(df_oc)} rows"),
                    ("OK" if len(df_ocr) == 0 else "!!", "Sessions → courses (orphan course_id)", f"{len(df_ocr)} rows"),
                    ("OK" if len(df_os) == 0 else "!!", "Courses → series (orphan series_id)", f"{len(df_os)} rows"),
                    ("OK" if len(df_dup) == 0 else "!!", "Duplicate session barcodes", f"{len(df_dup)} barcode(s)"),
                    ("--", "Barcode set dfcrs vs DB", "no dfcrs file"),
                    ("--", "dfcrs locations missing in DB centres", "no dfcrs file"),
                ]
            )
        rows.append(("!!", "dfcrs.csv present", f"missing — {dfcrs_path}"))
        rows.append(("--", "dfcrs required-field nulls", "no dfcrs file"))
        rows.append(("--", "dfcrs availability invalid", "no dfcrs file"))
        res.summary_rows = rows
        res.df_orphan_centre = df_oc
        res.df_orphan_course = df_ocr
        res.df_orphan_series = df_os
        res.df_dup_barcode = df_dup
        res.dfcrs_exists = False
        return res

    res.dfcrs_exists = True
    dfcrs = pd.read_csv(dfcrs_path)
    res.csv_row_count = len(dfcrs)
    dfcrs["start_date"] = pd.to_datetime(dfcrs["start_date"])
    mask_season = dfcrs["start_date"] >= cutoff
    if "has_enroll_now" in dfcrs.columns:
        _truthy = {"1", "true", "t", "yes", "y"}
        has_enroll_now = dfcrs["has_enroll_now"].fillna(False).apply(
            lambda v: str(v).strip().lower() in _truthy if not isinstance(v, bool) else v
        )
    else:
        has_enroll_now = pd.Series(False, index=dfcrs.index)
    mask_insert_scope = mask_season | has_enroll_now
    res.csv_slice_rows = int(mask_season.sum())
    dfcrs_sess = dfcrs[mask_insert_scope & dfcrs["Location"].notna()].copy()
    dfcrs_sess["Course Number"] = dfcrs_sess["Course Number"].astype(str)
    dfcrs_sess["barcode"] = dfcrs_sess["program"] + pk_prefix + dfcrs_sess["Course Number"].str.replace(
        "#", "", regex=False
    )
    csv_barcodes = set(dfcrs_sess["barcode"].tolist())
    res.distinct_csv_barcodes = len(csv_barcodes)

    if res.engine_ok and engine is not None:
        only_csv = sorted(csv_barcodes - db_barcodes)
        only_db = sorted(db_barcodes - csv_barcodes)
        res.distinct_db_barcodes = len(db_barcodes)
        expected = set((pk_prefix + dfcrs_sess["Location"].astype(str)).unique())
        with engine.connect() as conn:
            db_centres = set(
                pd.read_sql(
                    text(
                        "SELECT name FROM activities_centres WHERE name LIKE :prefix ESCAPE '\\'"
                    ),
                    con=conn,
                    params={"prefix": pattern},
                )["name"].tolist()
            )
        missing_centres = sorted(expected - db_centres)

    sub = dfcrs[mask_insert_scope]
    sub_nrows = len(sub)
    nulls = {
        "program": sub["program"].isna().sum(),
        "series": sub["series"].isna().sum(),
        "Name": sub["Name"].isna().sum(),
        "Course Number": sub["Course Number"].isna().sum(),
        "Location": sub["Location"].isna().sum(),
        "Time": sub["Time"].isna().sum() if "Time" in sub.columns else None,
        "Date": sub["Date"].isna().sum() if "Date" in sub.columns else None,
        "start_date": sub["start_date"].isna().sum(),
    }
    key_nulls = sum(
        int(nulls[k])
        for k in ("program", "series", "Name", "Course Number", "Location", "start_date")
        if k in nulls
    )
    if "availability" in dfcrs.columns:
        av = dfcrs["availability"].fillna("open").astype(str).str.strip().str.lower()
        bad_av_n = int((~av.isin({"full", "open", "unknown"})).sum())

    if not res.engine_ok:
        rows.extend(
            [
                ("--", "Sessions → centres (orphan centre_id)", "no DB"),
                ("--", "Sessions → courses (orphan course_id)", "no DB"),
                ("--", "Courses → series (orphan series_id)", "no DB"),
                ("--", "Duplicate session barcodes", "no DB"),
                ("--", "Barcode set dfcrs vs DB", "no DB"),
                ("--", "dfcrs locations missing in DB centres", "no DB"),
            ]
        )
    else:
        n_mismatch = len(only_csv) + len(only_db)
        rows.extend(
            [
                ("OK" if len(df_oc) == 0 else "!!", "Sessions → centres (orphan centre_id)", f"{len(df_oc)} rows"),
                ("OK" if len(df_ocr) == 0 else "!!", "Sessions → courses (orphan course_id)", f"{len(df_ocr)} rows"),
                ("OK" if len(df_os) == 0 else "!!", "Courses → series (orphan series_id)", f"{len(df_os)} rows"),
                ("OK" if len(df_dup) == 0 else "!!", "Duplicate session barcodes", f"{len(df_dup)} barcode(s)"),
                (
                    "OK" if n_mismatch == 0 else "!!",
                    "Barcode set dfcrs vs DB",
                    f"csv_only={len(only_csv)} db_only={len(only_db)} "
                    f"(distinct csv={len(csv_barcodes)} db={len(db_barcodes)})",
                ),
                (
                    "OK" if len(missing_centres) == 0 else "!!",
                    "dfcrs locations missing in DB centres",
                    f"{len(missing_centres)} location(s)",
                ),
            ]
        )
    rows.append(
        (
            "OK" if key_nulls == 0 else "!!",
            "dfcrs required-field nulls",
            f"{key_nulls} total (rows in slice={sub_nrows})",
        )
    )
    rows.append(
        ("OK" if bad_av_n == 0 else "!!", "dfcrs availability invalid", f"{bad_av_n} row(s)")
    )

    res.summary_rows = rows
    res.df_orphan_centre = df_oc
    res.df_orphan_course = df_ocr
    res.df_orphan_series = df_os
    res.df_dup_barcode = df_dup
    res.only_csv = only_csv
    res.only_db = only_db
    res.missing_centres = missing_centres
    res.nulls = {k: v for k, v in nulls.items() if v is not None}
    res.sub_nrows = sub_nrows
    res.bad_av_n = bad_av_n
    return res


def _print_integrity_table(rows: list[tuple[str, str, str]]) -> None:
    label_w = max((len(r[1]) for r in rows), default=0)
    label_w = max(label_w, 42)
    print(f"\n  {'ST':<4}  {'CHECK':<{label_w}}  DETAIL")
    print(f"  {'-' * 3:<4}  {'-' * label_w}  {'-' * 28}")
    for status, label, detail in rows:
        print(f"  {status:<4}  {label:<{label_w}}  {detail}")
    n_fail = sum(1 for s, _, _ in rows if s == "!!")
    n_skip = sum(1 for s, _, _ in rows if s == "--")
    if n_fail:
        print(f"\n  Result: {n_fail} check(s) need attention (marked !!).")
    else:
        tail = f" ({n_skip} skipped)." if n_skip else "."
        print(f"\n  Result: all runnable checks passed{tail}")


def print_integrity_failures(r: IntegrityResult) -> None:
    print("\n--- Failure details ---")
    any_d = False
    if r.engine_ok:
        if len(r.df_orphan_centre) > 0:
            any_d = True
            print("\n  Sessions → centres (up to 20):\n", r.df_orphan_centre.head(20).to_string(), sep="")
        if len(r.df_orphan_course) > 0:
            any_d = True
            print("\n  Sessions → courses (up to 20):\n", r.df_orphan_course.head(20).to_string(), sep="")
        if len(r.df_orphan_series) > 0:
            any_d = True
            print("\n  Courses → series (up to 20):\n", r.df_orphan_series.head(20).to_string(), sep="")
        if len(r.df_dup_barcode) > 0:
            any_d = True
            print("\n  Duplicate barcodes:\n", r.df_dup_barcode.to_string(), sep="")
        if r.only_csv or r.only_db:
            any_d = True
            if r.only_csv[:15]:
                print("\n  Barcodes only in dfcrs (sample):", r.only_csv[:15])
            if r.only_db[:15]:
                print("  Barcodes only in DB (sample):", r.only_db[:15])
        if r.missing_centres:
            any_d = True
            print("\n  Missing centre names (sample):", r.missing_centres[:20])
    key_fail = sum(
        int(r.nulls.get(k, 0) or 0)
        for k in ("program", "series", "Name", "Course Number", "Location", "start_date")
    )
    if key_fail > 0:
        any_d = True
        print(f"\n  dfcrs null counts (slice rows={r.sub_nrows}):")
        for k, v in r.nulls.items():
            if int(v or 0) > 0:
                print(f"    {k}: {int(v)}")
    if r.bad_av_n > 0:
        any_d = True
        print(f"\n  Invalid availability rows: {r.bad_av_n}")
    if not any_d and r.dfcrs_exists:
        print("  (no failure samples to show)")
    elif not r.dfcrs_exists:
        print("  (dfcrs.csv missing — run script 2)")


# ---------------------------------------------------------------------------
# Optional: compare session barcodes between two latest export JSON dirs
# ---------------------------------------------------------------------------


def compare_output_export_barcodes(*, max_files: int = 12) -> None:
    """Under output/<CITY>/<YEAR_AND_SEASON>/, compare *_{season}.json between last two scraped* dirs."""
    root = os.path.join("output", config.CITY, config.YEAR_AND_SEASON)
    if not os.path.isdir(root):
        print(f"\n--- Export compare ---\n  No folder {root!r}")
        return
    subs = sorted(
        d
        for d in os.listdir(root)
        if d.startswith("scraped") and os.path.isdir(os.path.join(root, d))
    )
    if len(subs) < 2:
        print(f"\n--- Export compare ---\n  Need 2+ scraped* under {root}; found {len(subs)}")
        return
    a, b = subs[-2], subs[-1]
    pa, pb = os.path.join(root, a), os.path.join(root, b)
    suffix = f"_{config.YEAR_AND_SEASON}.json"
    files_a = {f for f in os.listdir(pa) if f.endswith(suffix)}
    files_b = {f for f in os.listdir(pb) if f.endswith(suffix)}
    common = sorted(files_a & files_b)[:max_files]
    print(f"\n--- Export JSON barcode diff ({a} vs {b}) ---")
    if not common:
        print(f"  No matching *{suffix} in both dirs.")
        return
    for name in common:
        try:
            with open(os.path.join(pa, name), encoding="utf-8") as fa:
                ja = json.load(fa)
            with open(os.path.join(pb, name), encoding="utf-8") as fb:
                jb = json.load(fb)
        except (OSError, json.JSONDecodeError) as e:
            print(f"  {name}: skip ({e})")
            continue
        sa = pd.DataFrame(ja.get("sessionsWithCourses") or [])
        sb = pd.DataFrame(jb.get("sessionsWithCourses") or [])
        if "barcode" not in sa.columns or "barcode" not in sb.columns:
            print(f"  {name}: no barcode column")
            continue
        ba, bb = set(sa["barcode"].dropna().astype(str)), set(sb["barcode"].dropna().astype(str))
        only_new = len(bb - ba)
        only_old = len(ba - bb)
        print(f"  {name}: sessions A={len(ba)} B={len(bb)} | only_in_{b}={only_new} only_in_{a}={only_old}")


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


def _tag(ok: bool | None) -> str:
    if ok is True:
        return "[OK]"
    if ok is False:
        return "[!!]"
    return "[--]"


def print_overview(scrape: ScrapeHealthResult, integrity: IntegrityResult) -> None:
    cutoff = pd.Timestamp(config.STARTDATE_CUTOFF).date()
    print("\n" + "=" * 76)
    print("  DATA PREP HEALTH")
    print("=" * 76)
    print(f"  City / season:  {config.CITY} / {config.YEAR_AND_SEASON}")
    print(f"  Latest scrape:  {config.season}")
    print(f"  dfcrs path:     {integrity.dfcrs_path}")
    print(f"  DB insert cutoff (start_date):  {cutoff}")
    print(f"  pk_prefix:      {config.pk_prefix!r}")

    # Layer 1: raw scrape
    if scrape.error:
        print(f"\n  {_tag(False)} SCRAPE   {scrape.error}")
    elif scrape.df_latest is None or scrape.df_latest.empty:
        print(f"\n  {_tag(False)} SCRAPE   No activity folders with scrape_counts data")
    else:
        n = len(scrape.df_latest)
        mm = scrape.mismatch_count()
        mj = scrape.missing_json_count()
        tail = f"{n} activities | header≠parsed: {mm} | missing scrape_counts.json: {mj}"
        print(f"\n  {_tag(scrape.ok)} SCRAPE   {tail}")
        if scrape.latest_run:
            print(f"           run: {scrape.latest_run}")

    # Layer 2: clean CSV
    if not integrity.dfcrs_exists:
        print(f"\n  {_tag(False)} CLEAN    dfcrs.csv not found (run script 2)")
    else:
        print(
            f"\n  {_tag(True)} CLEAN    dfcrs.csv rows={integrity.csv_row_count} "
            f"| rows with start_date>={cutoff}: {integrity.csv_slice_rows} "
            f"(barcode checks use start_date>={cutoff} OR has_enroll_now)"
        )

    # Layer 3: database
    if not integrity.engine_ok:
        print(f"\n  {_tag(None)} DATABASE Could not connect (set DB_URL / run Postgres)")
    else:
        fails = integrity.fail_count()
        print(
            f"\n  {_tag(fails == 0)} DATABASE Connected | integrity checks failing: {fails} "
            f"(skipped: {integrity.skip_count()})"
        )

    # Overall
    scrape_bad = scrape.error or (scrape.df_latest is not None and not scrape.ok)
    clean_bad = not integrity.dfcrs_exists
    db_bad = integrity.engine_ok and integrity.fail_count() > 0
    if scrape_bad or clean_bad or db_bad:
        print("\n  " + "-" * 72)
        print("  OVERALL  Needs attention — use  python 5_check_data.py -v  for tables & samples")
        print("  " + "-" * 72)
    else:
        if not integrity.engine_ok:
            print("\n  OVERALL  Clean path OK (DB not checked)")
        else:
            print("\n  OVERALL  Healthy on checked layers")
        print("           Deeper:  -v  (integrity)   --deep  (+ scrape table, paths, export diffs)")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Data prep health (scrape → clean → DB).")
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show integrity check table and failure samples.",
    )
    p.add_argument(
        "--deep",
        action="store_true",
        help="Verbose plus per-activity scrape table, artifact paths, export JSON barcode diff.",
    )
    p.add_argument(
        "--no-write-csv",
        action="store_true",
        help="Do not write scrape_counts_summary.csv / changes CSV under latest scrape dir.",
    )
    args = p.parse_args(argv)
    verbose = args.verbose or args.deep

    scrape = collect_scrape_health(write_csv=not args.no_write_csv)
    integrity = collect_integrity()

    print_overview(scrape, integrity)

    if verbose:
        print("\n" + "=" * 76)
        print("  INTEGRITY CHECK TABLE (same rules as 3_insert_to_db.py)")
        print("=" * 76)
        _print_integrity_table(integrity.summary_rows)
        print_integrity_failures(integrity)

    if args.deep:
        print("\n" + "=" * 76)
        print("  DEEP: scrape_counts per activity (latest run)")
        print("=" * 76)
        if scrape.df_latest is not None and not scrape.df_latest.empty:
            cols = ["activity_folder", "found", "header_total", "parsed", "match"]
            use = [c for c in cols if c in scrape.df_latest.columns]
            print(scrape.df_latest[use].to_string(index=False))
        if scrape.summary_csv:
            print(f"\n  Written: {scrape.summary_csv}")
        if scrape.changes_csv:
            print(f"  Written: {scrape.changes_csv}")
        compare_output_export_barcodes()

    print()
    # Exit code: 1 if any hard failure on checked layers
    scrape_bad = bool(scrape.error) or (
        scrape.df_latest is None or scrape.df_latest.empty or not scrape.ok
    )
    clean_bad = not integrity.dfcrs_exists
    db_bad = integrity.engine_ok and integrity.fail_count() > 0
    return 1 if (scrape_bad or clean_bad or db_bad) else 0


if __name__ == "__main__":
    sys.exit(main())
