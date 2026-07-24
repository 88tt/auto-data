"""
Export activity data from PostgreSQL to JSON files for static frontend consumption.
Output shape matches the API responses so the React app can switch from fetch(api_host/...)
to fetch(static JSON) with minimal changes.

Usage:
  Set SPORTS (or use env) for which programs to export. YEAR_AND_SEASON comes from config.
  python 4_db_to_json.py

  Optional latest-scrape-only export (keep all sessions in DB, but export only sessions
  present in latest scrape's dfcrs.csv: start_date>=STARTDATE_CUTOFF OR has_enroll_now):
  EXPORT_LATEST_SCRAPE_ONLY=1 python 4_db_to_json.py

  You can set EXPORT_LATEST_SCRAPE_ONLY in config.py or via env var per run.
  When enabled, missing/invalid latest dfcrs.csv will fall back to full-season export.

  Writes one file per (sport, yearAndSeason): e.g. Arts_2026s2To.json
  Also writes programs.json, centre_programs.json, manifest.json, export_counts.json.
  Default output directory is a sibling ../kebu-lite/public/data (flat — latest data only).
  Override with OUT_DIR=/path/to/public/data. Sport files contain:
  { "series", "courseNames", "sessionsWithCourses", "businessLocations" }

To run:
```
python 4_db_to_json.py

# Latest scrape only (export filter only; no DB deletion)
EXPORT_LATEST_SCRAPE_ONLY=1 python 4_db_to_json.py
```

"""

import json
import math
import os
import re
from datetime import datetime, date, time
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text

import config


def _json_serial(obj):
    """Convert non-JSON-serializable values for json.dump."""
    if isinstance(obj, (datetime, date, time)):
        return obj.isoformat()
    if hasattr(obj, "item") and callable(getattr(obj, "item", None)):
        return obj.item()  # numpy/pandas scalar (int64, float64, etc.)
    if pd.isna(obj):
        return None
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _is_missing(val) -> bool:
    """True for None, pandas/NumPy NA, or float nan/inf (invalid in strict JSON)."""
    if val is None:
        return True
    try:
        if pd.isna(val):
            return True
    except TypeError:
        pass
    if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
        return True
    return False


def _str_or_none(val):
    """DB/pandas string fields: missing -> None (never NaN in JSON)."""
    if _is_missing(val):
        return None
    s = str(val).strip()
    return s if s else None


def _float_or_none(val):
    """Latitude/longitude/ages: missing or non-finite -> None."""
    if _is_missing(val):
        return None
    x = float(val)
    if math.isnan(x) or math.isinf(x):
        return None
    return x


def _sanitize_json_value(obj):
    """
    Recursively replace NaN/inf and pandas NA so json.dump(..., allow_nan=False) works.
    Python's json encodes float('nan') as invalid JSON unless allow_nan=True (default).
    """
    if isinstance(obj, (datetime, date, time)):
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize_json_value(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_json_value(v) for v in obj]
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    try:
        if pd.isna(obj) and not isinstance(obj, (bool, type(None))):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(obj, "item") and callable(getattr(obj, "item", None)):
        try:
            v = obj.item()
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                return None
            return v
        except Exception:
            pass
    return obj


# ---------------------------------------------------------------------------
# Config (from config.py; override via env or edit config)
# ---------------------------------------------------------------------------
DB_URL = os.environ.get("DB_URL", config.DB_URL)
YEAR_AND_SEASON = os.environ.get("YEAR_AND_SEASON", config.YEAR_AND_SEASON)
# Programs to export. Override with env SPORTS (comma-separated).
SPORTS = [
    s.strip()
    for s in os.environ.get(
        "SPORTS",
        "Adapted Activities,Arts,CampTO,Dance,Early Years & After School,FitnessTO,Gymnastics,Leadership,Hobbies,Martial Arts,Music,Skate & Ski,Sports,Swim",
    ).split(",")
    if s.strip()
]
# Flat directory for static JSON (default: sibling kebu-lite/public/data).
try:
    _project_dir = Path(__file__).resolve().parent
except NameError:
    _project_dir = Path(os.getcwd()).resolve()
_sibling_public_data = _project_dir.parent / "kebu-lite" / "public" / "data"
_default_out = _sibling_public_data if _sibling_public_data.is_dir() else (_project_dir / "output")
OUT_DIR = Path(os.environ.get("OUT_DIR", _default_out))
# Path to programs_desc.csv (optional); committed to repo root so it survives across scrape runs.
PROGRAMS_CSV = os.environ.get(
    "PROGRAMS_CSV",
    str(_project_dir / "programs_desc.csv"),
)
EXPORT_LATEST_SCRAPE_ONLY = bool(
    os.environ.get("EXPORT_LATEST_SCRAPE_ONLY", str(config.EXPORT_LATEST_SCRAPE_ONLY)).strip().lower()
    in {"1", "true", "t", "yes", "y", "on"}
)


def _time_fmt(t):
    """Format time like Django's 'g:i A' (e.g. 5:15 PM)."""
    if t is None or pd.isna(t):
        return None
    if isinstance(t, str):
        return t
    h, m = (t.hour, t.minute) if hasattr(t, "hour") else (0, 0)
    h12 = h % 12 or 12
    ampm = "AM" if h < 12 else "PM"
    return f"{h12}:{m:02d} {ampm}"


def _dt_fmt(dt):
    """ISO format for dates (frontend can parse)."""
    if dt is None or pd.isna(dt):
        return None
    if isinstance(dt, (datetime, pd.Timestamp)):
        return dt.isoformat()
    return str(dt)


def _build_latest_allowed_barcodes(engine=None) -> set[str] | None:
    """
    Return allowed session barcodes from the latest scrape.

    Primary source: dfcrs.csv produced by the current scrape run.
    Fallback (export_only mode): query the DB for all barcodes whose last_seen_at
    matches the most recent scrape date — no user input, parameterless query.

    Returns None only when both sources are unavailable (triggers full-season export).
    """
    dfcrs_path = Path(config.season) / "dfcrs.csv"
    if dfcrs_path.exists():
        try:
            df = pd.read_csv(dfcrs_path)
        except Exception as e:
            print(f"Warning: could not read {dfcrs_path} ({e}); trying DB fallback.")
            df = None
    else:
        df = None

    if df is not None:
        needed_cols = {"program", "Course Number", "start_date"}
        missing = needed_cols - set(df.columns)
        if missing:
            print(
                "Warning: dfcrs is missing columns "
                f"{sorted(missing)}; trying DB fallback."
            )
            df = None

    if df is not None:
        cutoff = pd.Timestamp(config.STARTDATE_CUTOFF)
        work = df[["program", "Course Number", "start_date"]].copy()
        work["start_date"] = pd.to_datetime(work["start_date"], errors="coerce")
        if "has_enroll_now" in df.columns:
            _truthy = {"1", "true", "t", "yes", "y"}
            enroll = df["has_enroll_now"].fillna(False).apply(
                lambda v: str(v).strip().lower() in _truthy if not isinstance(v, bool) else v
            )
            work["has_enroll_now"] = enroll.values
        else:
            work["has_enroll_now"] = False
        in_scope = (work["start_date"] >= cutoff) | work["has_enroll_now"]
        work = work[in_scope]
        work = work.dropna(subset=["program", "Course Number"])
        work["Course Number"] = work["Course Number"].astype(str).str.replace("#", "", regex=False).str.strip()
        work["program"] = work["program"].astype(str)
        work = work[work["Course Number"] != ""]
        barcodes = set((work["program"] + config.pk_prefix + work["Course Number"]).tolist())
        print(f"Loaded {len(barcodes)} allowed barcodes from dfcrs.csv")
        return barcodes

    # Fallback: derive latest scrape from DB updated_at (used in export_only mode).
    if engine is None:
        print("Warning: no dfcrs.csv and no DB engine available; exporting full season.")
        return None

    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT barcode
                    FROM activities_sessions
                    WHERE last_seen_at::date = (
                        SELECT MAX(last_seen_at)::date FROM activities_sessions
                    )
                    """
                )
            ).fetchall()
        barcodes = {r[0] for r in rows}
        print(f"Loaded {len(barcodes)} allowed barcodes from DB (latest last_seen_at date)")
        return barcodes if barcodes else None
    except Exception as e:
        print(f"Warning: DB fallback failed ({e}); exporting full season.")
        return None


# Programs the DB never tags with their own `program` value -- every row
# still comes in tagged under a broader parent program from
# 3_insert_to_db.py -- but that should still get their own exported JSON
# file/homepage tile. Keyed by the child's own program name, mapping to
# (parent program, a case-insensitive substring that appears ONLY in that
# child's own rows' series/crs_fam identifier text, never in a genuine
# parent-only row's).
#
# Music: 2_clean_data.py's categorize_music sets `series` independently of
# `program` to "music: <bucket>" (e.g. "music: piano") for every genuine
# Music row, while leaving `program` as 'Arts' -- so "music:" is the signal.
# A future 3_insert_to_db.py run could start tagging these rows
# program='Music' directly; nothing here depends on that happening.
#
# Martial Arts: no categorize_* step needed -- the City's own source data
# already tags these rows' series as "Martial Arts" verbatim (categorize
# courses: Sports only strips the "Sports -" prefix, it doesn't rename the
# series), so "martial arts" is the signal.
#
# Dance: same as Martial Arts -- the City's own source data already tags
# these rows' series as "dance" verbatim. Verified "dance" doesn't collide
# with any of Arts' other series names (visual arts, crafts, painting,
# workshops, pottery/sculpture, performing arts -- none contain "dance").
#
# Gymnastics: same pattern again -- series tagged "Gymnastics" verbatim.
# Verified it doesn't collide with any of Sports' other series names
# (Tennis and Table Tennis, Soccer, Basketball, Pickleball, Multi-Sport,
# Volleyball, Badminton, Ball Hockey, Baseball-Softball, Cricket, Adventure
# Sports -- none contain "gymnastics"). Sports now has two children split
# out of it (Martial Arts, Gymnastics) -- see
# _program_regex_and_split_signals' own docstring for why that needs
# collecting every child mapped to a parent, not just the first one found.
#
# Aquatic Leadership: series tagged "Aquatic Leadership" verbatim under
# Leadership (which has exactly two series: "Aquatic Leadership" and
# "Leadership and Employment Readiness" -- verified real data, no
# collision risk). Unlike every other split above, this child does NOT get
# its own program/tile -- see PROGRAM_ABSORPTIONS below, which merges it
# into Swim's export instead. It still needs to be registered here so
# Leadership's own export excludes it (displayed as "Leadership &
# Employment Readiness" post-split -- see _PROGRAM_DISPLAY_NAMES in
# main()) and so the recursive absorption call has a real split to resolve.
PROGRAM_SPLITS = {
    "Music": ("Arts", "music:"),
    "Martial Arts": ("Sports", "martial arts"),
    "Dance": ("Arts", "dance"),
    "Gymnastics": ("Sports", "gymnastics"),
    "Aquatic Leadership": ("Leadership", "aquatic leadership"),
}

# Programs that absorb an already-split-out child's rows into their OWN
# export, rather than that child getting its own separate program/tile --
# keyed by the absorbing program's name, listing which PROGRAM_SPLITS
# child(ren) to merge in. Reuses each child's own already-correct, fully
# isolated export (via a recursive export_sport_season call, see below)
# rather than re-deriving a new signal-matching rule for the merge itself.
PROGRAM_ABSORPTIONS = {
    "Swim": ["Aquatic Leadership"],
}


def _program_regex_and_split_signals(keyword: str) -> tuple[str, list[str], bool | None]:
    """DB program-prefix regex fragment for `keyword`, the split signal
    substring(s) to filter on, and whether to keep (True) or exclude (False)
    rows containing any of them -- see PROGRAM_SPLITS.

    Querying "(parent|child1|child2|...)" as the program-prefix regex
    (alternation) matches both a DB where every row is still tagged with the
    parent, and one where some rows have already been re-tagged with a
    child, in a single query -- no DB write required -- and the
    include/exclude filter this returns then keeps only the right rows from
    that combined result either way.

    A parent can have more than one child split out of it (Arts has both
    Music and Dance) -- this collects every child mapped to `keyword` when
    it's a parent, not just the first one found, so a second child doesn't
    silently leak back into the parent's own export.

    Returns (program_regex_fragment, signals, want_child): want_child is
    True to keep only rows matching `keyword`'s own signal (when `keyword`
    IS a child), False to exclude rows matching ANY of its children's
    signals (when `keyword` is the parent, so its own export doesn't
    double-count them), None for every other keyword (no filtering
    applied).
    """
    if keyword in PROGRAM_SPLITS:
        parent, signal = PROGRAM_SPLITS[keyword]
        return f"({parent}|{keyword})", [signal], True
    children = [(child, signal) for child, (parent, signal) in PROGRAM_SPLITS.items() if parent == keyword]
    if children:
        names = "|".join([keyword] + [child for child, _ in children])
        return f"({names})", [signal for _, signal in children], False
    return re.escape(keyword), [], None


def _apply_split_filter(df: pd.DataFrame, column: str, signals: list[str], want_child: bool | None) -> pd.DataFrame:
    if want_child is None or not signals:
        return df
    pattern = "|".join(re.escape(s) for s in signals)
    has_signal = df[column].str.contains(pattern, case=False, na=False)
    return df[has_signal] if want_child else df[~has_signal]


def export_series(engine, keyword, year_and_season, *, allowed_series_ids: set[str] | None = None):
    program_re, signals, want_child = _program_regex_and_split_signals(keyword)
    name_pattern = f"^{program_re}{re.escape(year_and_season)}_%_"
    q = text(
        """
        SELECT name, min_age, max_age, description, session_count, created_at
        FROM activities_series
        WHERE name ~ :pat
        ORDER BY session_count DESC
        """
    )
    df = pd.read_sql(q, engine, params={"pat": name_pattern})
    df = _apply_split_filter(df, "name", signals, want_child)
    if allowed_series_ids is not None:
        df = df[df["name"].isin(allowed_series_ids)]
    return df.to_dict(orient="records")


def export_coursenames(engine, keyword, year_and_season, *, allowed_coursename_ids: set[str] | None = None):
    program_re, signals, want_child = _program_regex_and_split_signals(keyword)
    pat = f"^{program_re}.*{re.escape(year_and_season)}_%_.*"
    q = text(
        """
        SELECT name, series_id AS series, crs_name_desc, num_sessions, min_age, max_age
        FROM activities_coursenames
        WHERE name ~ :pat
        """
    )
    df = pd.read_sql(q, engine, params={"pat": pat})
    df = _apply_split_filter(df, "name", signals, want_child)
    if allowed_coursename_ids is not None:
        df = df[df["name"].isin(allowed_coursename_ids)]
    return df.to_dict(orient="records")


def export_sessions_with_courses(engine, keyword, year_and_season, *, allowed_barcodes: set[str] | None = None):
    # barcode alone doesn't embed series (see 3_insert_to_db.py:
    # barcode = program + pk_prefix + Course Number) -- the split filter
    # below runs on crs_fam (= c.series_id, which DOES embed series) instead.
    program_re, signals, want_child = _program_regex_and_split_signals(keyword)
    barcode_pat = f"^{program_re}{re.escape(year_and_season)}_%_.*"
    q = text(
        """
        SELECT
            s.barcode,
            s.day_of_week,
            s.start_time,
            s.end_time,
            s.start_date,
            s.end_date,
            s.min_age,
            s.max_age,
            s.session_url,
            s.availability,
            s.has_enroll_now,
            c.name   AS crs_name_detailed,
            c.crs_name,
            c.description,
            c.series_id AS crs_fam,
            ct.name    AS centre,
            ct.latitude,
            ct.longitude,
            ct.address,
            ct.url    AS centre_url
        FROM activities_sessions s
        JOIN activities_courses c ON s.course_id = c.name
        LEFT JOIN activities_centres ct ON s.centre_id = ct.name
        WHERE s.barcode ~ :pat
        """
    )
    df = pd.read_sql(q, engine, params={"pat": barcode_pat})
    df = _apply_split_filter(df, "crs_fam", signals, want_child)
    if allowed_barcodes is not None:
        df = df[df["barcode"].isin(allowed_barcodes)]

    rows = []
    for _, r in df.iterrows():
        rows.append({
            "barcode": r["barcode"],
            "day_of_week": r["day_of_week"],
            "start_time": _time_fmt(r["start_time"]),
            "end_time": _time_fmt(r["end_time"]),
            "start_date": _dt_fmt(r["start_date"]),
            "end_date": _dt_fmt(r["end_date"]),
            "min_age": _float_or_none(r["min_age"]),
            "max_age": _float_or_none(r["max_age"]),
            "session_url": _str_or_none(r["session_url"]),
            "availability": (
                str(r["availability"]).strip()
                if pd.notna(r["availability"]) and str(r["availability"]).strip()
                else "open"
            ),
            "has_enroll_now": bool(r["has_enroll_now"]) if pd.notna(r["has_enroll_now"]) else False,
            "crs_name_detailed": _str_or_none(r["crs_name_detailed"]),
            "crs_name": _str_or_none(r["crs_name"]),
            "desc": _str_or_none(r["description"]),
            "crs_fam": _str_or_none(r["crs_fam"]),
            "centre": _str_or_none(r["centre"]),
            "latitude": _float_or_none(r["latitude"]),
            "longitude": _float_or_none(r["longitude"]),
            "address": _str_or_none(r["address"]),
            "centre_url": _str_or_none(r["centre_url"]),
        })
    return rows


def export_business_locations(engine, keyword):
    """Filter by activity containing keyword (e.g. swimming, skating)."""
    q = text(
        """
        SELECT
            bl.loc_name,
            bl.address,
            bl.latitude,
            bl.longitude,
            b.business,
            b.url,
            b.bus_info,
            b.programs,
            b.age,
            b.relevant_info
        FROM activities_businesslocations bl
        JOIN activities_businesses b ON bl.business_id = b.business
        WHERE bl.activity ILIKE :kw
        """
    )
    df = pd.read_sql(q, engine, params={"kw": f"%{keyword}%"})
    return df.to_dict(orient="records")


def export_sport_season(engine, keyword, year_and_season, *, allowed_barcodes: set[str] | None = None):
    sessions = export_sessions_with_courses(
        engine,
        keyword,
        year_and_season,
        allowed_barcodes=allowed_barcodes,
    )
    allowed_series_ids = None
    allowed_coursename_ids = None
    if allowed_barcodes is not None:
        allowed_series_ids = {r["crs_fam"] for r in sessions if r.get("crs_fam")}
        allowed_coursename_ids = {r["crs_name"] for r in sessions if r.get("crs_name")}

    payload = {
        "series": export_series(
            engine,
            keyword,
            year_and_season,
            allowed_series_ids=allowed_series_ids,
        ),
        "courseNames": export_coursenames(
            engine,
            keyword,
            year_and_season,
            allowed_coursename_ids=allowed_coursename_ids,
        ),
        "sessionsWithCourses": sessions,
        "businessLocations": export_business_locations(engine, keyword),
    }

    # See PROGRAM_ABSORPTIONS -- merges an already-split-out child's own
    # (correctly isolated) export straight into this program's payload,
    # rather than that child getting its own separate program/tile.
    for absorbed in PROGRAM_ABSORPTIONS.get(keyword, []):
        absorbed_payload = export_sport_season(
            engine, absorbed, year_and_season, allowed_barcodes=allowed_barcodes
        )
        payload["series"] += absorbed_payload["series"]
        payload["courseNames"] += absorbed_payload["courseNames"]
        payload["sessionsWithCourses"] += absorbed_payload["sessionsWithCourses"]
        payload["businessLocations"] += absorbed_payload["businessLocations"]

    return payload


def main():
    engine = create_engine(DB_URL)
    # Single flat folder (e.g. kebu-lite/public/data): latest export overwrites sport JSON files.
    season_dir = OUT_DIR
    season_dir.mkdir(parents=True, exist_ok=True)
    print(f"Export OUT_DIR={season_dir.resolve()}")

    allowed_barcodes: set[str] | None = None
    if EXPORT_LATEST_SCRAPE_ONLY:
        allowed_barcodes = _build_latest_allowed_barcodes(engine)
    print(
        f"Export mode: latest scrape only = {bool(EXPORT_LATEST_SCRAPE_ONLY and allowed_barcodes is not None)}"
    )
    if EXPORT_LATEST_SCRAPE_ONLY and allowed_barcodes is None:
        print("Latest-scrape-only requested, but no filter source available. Falling back to full-season export.")

    # Display name overrides: maps internal export program name → frontend display name.
    # Required when the DB program name differs from what the frontend constants (activityData.js) expect.
    _PROGRAM_DISPLAY_NAMES = {
        "Hobbies": "Hobbies and Interests",
        "Leadership": "Leadership & Employment Readiness",
    }

    # Load programs_desc.csv as description source (optional)
    programs_count = 0
    programs_csv = Path(PROGRAMS_CSV)
    _programs_desc_map: dict = {}
    if programs_csv.exists():
        _df_desc = pd.read_csv(programs_csv)
        for _, _r in _df_desc.iterrows():
            _programs_desc_map[str(_r.get("program", "")).strip()] = _r.to_dict()
    else:
        print(f"Note: programs_desc not found at {programs_csv}, will write programs.json from export counts.")

    export_counts = []
    centre_programs: dict[str, list[str]] = {}  # centre name -> [program, ...]
    for sport in SPORTS:
        sport = sport.strip()  # preserve exact case for DB lookup and filenames
        payload = export_sport_season(
            engine,
            sport,
            YEAR_AND_SEASON,
            allowed_barcodes=allowed_barcodes if EXPORT_LATEST_SCRAPE_ONLY else None,
        )
        # Sanitize for filename: slash would create a subdirectory (e.g. "Ski/Snowboard -" -> "Ski-Snowboard -")
        sport_safe = sport.replace("/", "-")
        out_path = season_dir / f"{sport_safe}_{YEAR_AND_SEASON}.json"
        payload = _sanitize_json_value(payload)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(
                payload,
                f,
                indent=2,
                ensure_ascii=False,
                default=_json_serial,
                allow_nan=False,
            )
        counts = {
            "program": sport,
            "series": len(payload["series"]),
            "courseNames": len(payload["courseNames"]),
            "sessions": len(payload["sessionsWithCourses"]),
            "businessLocations": len(payload["businessLocations"]),
        }
        export_counts.append(counts)
        print(f"Wrote {out_path} (series={counts['series']}, "
              f"courseNames={counts['courseNames']}, "
              f"sessions={counts['sessions']}, "
              f"businessLocations={counts['businessLocations']})")

        # Accumulate centre → programs index
        for s in payload["sessionsWithCourses"]:
            centre = s.get("centre")
            if centre and sport not in centre_programs.get(centre, []):
                centre_programs.setdefault(centre, []).append(sport)

    # Write programs.json — only include programs with sessions > 0; merge descriptions if available
    _programs_json: list = []
    for _ec in export_counts:
        if _ec["sessions"] == 0:
            continue
        _prog = _ec["program"]
        _display = _PROGRAM_DISPLAY_NAMES.get(_prog, _prog)
        _entry = dict(_programs_desc_map.get(_display, _programs_desc_map.get(_prog, {})))
        _entry["program"] = _display
        _programs_json.append(_entry)
    programs_count = len(_programs_json)
    with open(season_dir / "programs.json", "w", encoding="utf-8") as f:
        json.dump(_sanitize_json_value(_programs_json), f, indent=2, ensure_ascii=False, allow_nan=False)
    print(f"Wrote {season_dir / 'programs.json'} ({programs_count} programs, 0-session programs excluded)")

    # Write centre_programs.json — small index used by frontend activity selector
    centre_programs_path = season_dir / "centre_programs.json"
    with open(centre_programs_path, "w", encoding="utf-8") as f:
        json.dump(_sanitize_json_value(centre_programs), f, indent=2, ensure_ascii=False, allow_nan=False)
    print(f"Wrote {centre_programs_path} ({len(centre_programs)} centres)")

    # Save export record counts to file
    counts_path = season_dir / "export_counts.json"
    with open(counts_path, "w", encoding="utf-8") as f:
        json.dump(
            _sanitize_json_value(
                {
                    "yearAndSeason": YEAR_AND_SEASON,
                    "programs": programs_count,
                    "counts": export_counts,
                }
            ),
            f,
            indent=2,
            allow_nan=False,
        )
    print(f"Wrote {counts_path}")

    # Optional: write a small manifest so frontend knows available sport/season
    manifest = {
        "yearAndSeason": YEAR_AND_SEASON,
        "sports": [s.strip() for s in SPORTS],
    }
    with open(season_dir / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(_sanitize_json_value(manifest), f, indent=2, allow_nan=False)
    print(f"Wrote {season_dir / 'manifest.json'}")


if __name__ == "__main__":
    main()
