"""
Insert cleaned course data (from script 2) into PostgreSQL. Uses config.py for paths and DB URL.

To run:
```
python 3_insert_to_db.py
``` 
"""
import datetime
import os

import pandas as pd
from sqlalchemy import create_engine, text

import config
import spider_utils as su

# ---------------------------------------------------------------------------
# Config (from config.py; override with env)
# ---------------------------------------------------------------------------
season = config.season
pk_prefix = config.pk_prefix
startdate_cutoff_date = pd.Timestamp(config.STARTDATE_CUTOFF)

dfcrs = pd.read_csv(f'{season}/dfcrs.csv')
dfcrs['start_date'] = pd.to_datetime(dfcrs['start_date'])
if "availability" not in dfcrs.columns:
    dfcrs["availability"] = "open"
else:
    dfcrs["availability"] = (
        dfcrs["availability"].fillna("open").astype(str).str.strip().str.lower()
    )
    _av_ok = {"full", "open", "unknown"}
    dfcrs.loc[~dfcrs["availability"].isin(_av_ok), "availability"] = "open"
if "has_enroll_now" not in dfcrs.columns:
    dfcrs["has_enroll_now"] = False
else:
    _truthy = {"1", "true", "t", "yes", "y"}
    dfcrs["has_enroll_now"] = (
        dfcrs["has_enroll_now"]
        .fillna(False)
        .apply(lambda v: str(v).strip().lower() in _truthy if not isinstance(v, bool) else v)
    )
startdate_cutoff = dfcrs['start_date'] >= startdate_cutoff_date  # mask: only current season
# Session insert scope:
# - keep existing season cutoff rows
# - additionally include rows that are still actively enrollable
session_insert_mask = startdate_cutoff | dfcrs["has_enroll_now"].fillna(False)

engine = create_engine(config.DB_URL, pool_pre_ping=True, pool_recycle=300)

############################################################################################################
# insert into 'centres' in db
############################################################################################################
dfcentres = dfcrs[session_insert_mask & (~dfcrs['Location'].isnull())][['Location']].drop_duplicates()
# replace ' Cmty ' with ' Community ' and ' Rec ' with ' Recreation ' in Location
dfcentres['search_name'] = dfcentres['Location'].str.replace(' Cmty ', ' Community ', case=False).str.replace(' Rec ', ' Recreation ', case=False)
dfcentres['name'] = pk_prefix + dfcentres['Location']

# Load existing centres for current year/season/city only (name starts with pk_prefix, e.g. 2026s2To_%_)
_like_escape = lambda s: s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
existing_centres_prefix = _like_escape(pk_prefix) + "%"
with engine.connect() as _conn:
    existing_centres_df = pd.read_sql(
        text(
            "SELECT name, address, latitude, longitude, fullname, url FROM activities_centres "
            "WHERE name LIKE :prefix ESCAPE '\\'"
        ),
        con=_conn,
        params={"prefix": existing_centres_prefix},
    )
existing_names = set(existing_centres_df["name"])
dfcentres_existing = dfcentres[dfcentres["name"].isin(existing_names)].merge(
    existing_centres_df, on="name", how="left", suffixes=("", "_db")
)
dfcentres_new = dfcentres[~dfcentres["name"].isin(existing_names)]

# Only call Google API for locations not already in DB
if len(dfcentres_new) > 0:
    dfcentres_new = dfcentres_new.copy()
    dfcentres_new["place_id"] = dfcentres_new["search_name"].apply(
        lambda x: su.get_place_id(f"entrance of {x}, Toronto, ON, Canada")
    )
    dfcentres_new["details"] = dfcentres_new["place_id"].apply(su.get_place_details)
    dfcentres_new[["fullname", "address", "latitude", "longitude", "url", "loc_type"]] = (
        dfcentres_new["details"].apply(lambda x: pd.Series(su.parse_place_details(x)))
    )
    dfcentres_new["created_at"] = datetime.datetime.now()
    dfcentres_new["Location"] = dfcentres_new["Location"].str.replace(" Community ", " Cmty ", case=False).str.replace(" Recreation ", " Rec ", case=False)

    # to db (skip rows that already exist to avoid UniqueViolation on re-run)
    dfcentres_clean = dfcentres_new[["name", "address", "latitude", "longitude", "fullname", "url", "created_at"]][dfcentres_new["address"].notnull()].drop_duplicates()
    to_insert = dfcentres_clean[~dfcentres_clean["name"].isin(existing_names)]
    if len(to_insert) > 0:
        with engine.begin() as conn:
            to_insert.to_sql('activities_centres', con=conn, if_exists='append', index=False)
        print(f'Inserted {len(to_insert)} new centres.')


def _insert_missing_courses_for_session_fk(conn, *, needed_course_ids, dfcrs, season_mask, pk_prefix, like_esc):
    """
    Insert minimal activities_series + activities_courses rows so `course_id` on sessions
    satisfies FK to activities_courses.name (courses are normally inserted later in this script).
    Uses `conn` only (caller owns transaction).
    """
    needed = sorted({x for x in needed_course_ids if pd.notna(x)})
    if not needed:
        return

    existing_courses_prefix = "%" + like_esc(pk_prefix) + "%"
    existing_courses_df = pd.read_sql(
        text(
            "SELECT name FROM activities_courses "
            "WHERE name LIKE :prefix ESCAPE '\\'"
        ),
        con=conn,
        params={"prefix": existing_courses_prefix},
    )
    existing_course_names = set(existing_courses_df["name"].tolist())
    missing_course_ids = [cid for cid in needed if cid not in existing_course_names]
    if not missing_course_ids:
        return

    dfcrs_season = dfcrs[season_mask].copy()
    dfcrs_season["series_id"] = dfcrs_season["program"] + pk_prefix + dfcrs_season["series"]
    dfcrs_season["course_id"] = (
        dfcrs_season["program"] + dfcrs_season["series"].str.upper() + pk_prefix + dfcrs_season["Name"]
    )

    df_needed_courses = dfcrs_season[dfcrs_season["course_id"].isin(missing_course_ids)].copy()
    needed_series_ids = sorted(set(df_needed_courses["series_id"].tolist()))

    existing_series_df = pd.read_sql(
        text(
            "SELECT name FROM activities_series "
            "WHERE name LIKE :prefix ESCAPE '\\'"
        ),
        con=conn,
        params={"prefix": existing_courses_prefix},
    )
    existing_series_names = set(existing_series_df["name"].tolist())
    missing_series_ids = [sid for sid in needed_series_ids if sid not in existing_series_names]

    if len(missing_series_ids) > 0:
        df_series_to_insert = (
            df_needed_courses[df_needed_courses["series_id"].isin(missing_series_ids)]
            .groupby(["program", "series"], as_index=False)
            .agg(
                min_age=("min_age", "min"),
                max_age=("max_age", "max"),
                session_count=("Course Number", "count"),
            )
        )
        df_series_to_insert["name"] = df_series_to_insert["program"] + pk_prefix + df_series_to_insert["series"]
        df_series_to_insert["description"] = ""
        df_series_to_insert["created_at"] = datetime.datetime.now()
        df_series_to_insert = df_series_to_insert[
            ["name", "min_age", "max_age", "description", "created_at", "session_count"]
        ]
        df_series_to_insert = df_series_to_insert.drop_duplicates(subset=["name"])
        if not df_series_to_insert.empty:
            df_series_to_insert.to_sql(
                "activities_series",
                con=conn,
                if_exists="append",
                index=False,
            )
            print(f"Inserted {len(df_series_to_insert)} missing series (for FK).")

    df_courses_grouped = (
        df_needed_courses.groupby(["program", "series", "Name", "crs_name"], as_index=False)
        .agg(
            min_age=("min_age", "min"),
            max_age=("max_age", "max"),
            num_sessions=("Course Number", "count"),
            description=("Description", "first"),
        )
    )
    df_courses_grouped["series_id"] = df_courses_grouped["program"] + pk_prefix + df_courses_grouped["series"]
    df_courses_grouped["name"] = (
        df_courses_grouped["program"]
        + df_courses_grouped["series"].str.upper()
        + pk_prefix
        + df_courses_grouped["Name"]
    )
    df_courses_grouped["crs_name"] = (
        df_courses_grouped["program"]
        + df_courses_grouped["series"].str.upper()
        + pk_prefix
        + df_courses_grouped["crs_name"]
    )
    df_courses_grouped["created_at"] = datetime.datetime.now()
    df_courses_grouped["crs_name_desc"] = ""
    df_courses_grouped["description"] = df_courses_grouped["description"].fillna("")
    df_courses_to_insert = df_courses_grouped[
        ["name", "crs_name", "description", "min_age", "max_age", "num_sessions", "created_at", "series_id", "crs_name_desc"]
    ]
    df_courses_to_insert = df_courses_to_insert[df_courses_to_insert["name"].isin(missing_course_ids)]
    if df_courses_to_insert.empty:
        print(
            "Warning: could not build activities_courses rows from dfcrs for missing course_ids; "
            "session INSERT/UPDATE may still fail FK:",
            missing_course_ids,
        )
        return
    df_courses_to_insert = df_courses_to_insert.drop_duplicates(subset=["name"])
    df_courses_to_insert.to_sql(
        "activities_courses",
        con=conn,
        if_exists="append",
        index=False,
    )
    print(f"Inserted {len(df_courses_to_insert)} missing courses (for FK).")


############################################################################################################
# insert into 'sessions' in db
############################################################################################################
dfsess = dfcrs[session_insert_mask][
    [
        "program",
        "series",
        "Name",
        "Location",
        "Course Number",
        "day_of_week",
        "start_time",
        "end_time",
        "start_date",
        "end_date",
        "min_age",
        "max_age",
        "URL",
        "availability",
        "has_enroll_now",
    ]
]
dfsess['barcode']       = dfsess['program'] + pk_prefix + dfsess['Course Number'].str.replace('#','')
dfsess['course_id']     = dfsess['program'] + dfsess['series'].str.upper() + pk_prefix + dfsess['Name']
dfsess['centre_id']     = pk_prefix + dfsess['Location']
dfsess['centre_url']    = 'na'

# drop/ rename columns as needed
dfsess.rename(columns={'URL':'session_url'}, inplace=True)
_sess_ts = datetime.datetime.now()
dfsess['created_at'] = _sess_ts
dfsess['updated_at'] = _sess_ts

# if centre_id is not in dfcentres, fill centre_id with null
dfsess = dfsess.merge(dfcentres[['name']], how='left', left_on='centre_id', right_on='name')
dfsess.loc[dfsess['centre_id'].isnull(),'centre_id'] = None
dfsess.drop(columns=['name'], inplace=True)

# if end_date is null, fill with start_date
dfsess['end_date'] = dfsess['end_date'].fillna(dfsess['start_date'])
#drop rows where centre_id is null
_n_missing_centre = dfsess['centre_id'].isnull().sum()
if _n_missing_centre > 0:
    _missing_locs = dfsess.loc[dfsess['centre_id'].isnull(), 'centre_id'].unique()
    print(f"WARNING: {_n_missing_centre} sessions dropped — centre not in DB (likely failed Google Maps lookup): {list(_missing_locs)}")
dfsess = dfsess[~dfsess['centre_id'].isnull()]

# TODO: need to fill null max age with 999
dfsess['max_age'] = dfsess['max_age'].fillna(999)
# dfsess['centre_id'] = dfsess['centre_id'].str.replace(' Community ', ' Cmty ', case=False).str.replace(' Recreation ', ' Rec ', case=False)

# to db (skip existing)
dfsess_clean = dfsess[
    [
        "barcode",
        "course_id",
        "centre_id",
        "day_of_week",
        "start_time",
        "end_time",
        "start_date",
        "end_date",
        "min_age",
        "max_age",
        "session_url",
        "availability",
        "has_enroll_now",
        "created_at",
        "updated_at",
    ]
].drop_duplicates()

# Insert new sessions; update existing sessions whose values changed.
# Note: `barcode` includes `pk_prefix` in the middle of the string, so we match by substring.
dfsess_clean = dfsess_clean.copy()
dfsess_clean["_idx"] = range(len(dfsess_clean))

existing_sessions_prefix = "%" + _like_escape(pk_prefix) + "%"
existing_sessions_df = pd.read_sql(
    text(
        "SELECT barcode, course_id, centre_id, day_of_week, start_time, end_time, "
        "start_date, end_date, min_age, max_age, session_url, availability "
        ", has_enroll_now "
        "FROM activities_sessions "
        "WHERE barcode LIKE :prefix ESCAPE '\\'"
    ),
    con=engine,
    params={"prefix": existing_sessions_prefix},
)

existing_barcodes_set = set(existing_sessions_df["barcode"].tolist())

to_insert = dfsess_clean[~dfsess_clean["barcode"].isin(existing_barcodes_set)].drop(columns=["_idx"])
if len(to_insert) > 0:
    _needed_ins = sorted(set(to_insert["course_id"].dropna().tolist()))
    with engine.begin() as conn:
        _insert_missing_courses_for_session_fk(
            conn,
            needed_course_ids=_needed_ins,
            dfcrs=dfcrs,
            season_mask=session_insert_mask,
            pk_prefix=pk_prefix,
            like_esc=_like_escape,
        )
        to_insert.to_sql("activities_sessions", con=conn, if_exists="append", index=False)
    print(f"Inserted {len(to_insert)} new sessions.")

# Build update set
if len(existing_barcodes_set) > 0:
    comp_cols = [
        "course_id",
        "centre_id",
        "day_of_week",
        "start_time",
        "end_time",
        "start_date",
        "end_date",
        "min_age",
        "max_age",
        "session_url",
        "availability",
        "has_enroll_now",
    ]

    def _norm_for_compare(s, *, as_date: bool = False, as_bool: bool = False):
        """
        Normalize values for robust comparisons.
        - For dates: compare by calendar date (YYYY-MM-DD) to avoid timezone/format diffs.
        - For booleans: normalize to "true"/"false" to avoid True/"True"/1 mismatches.
        - Otherwise: normalize missing to "" and cast to string.
        """
        if not hasattr(s, "fillna"):
            return str(s or "")

        if as_date:
            # `s` can contain tz-aware python datetimes from `read_sql`.
            # `utc=True` makes Pandas convert consistently to datetime64[ns, UTC].
            dt = pd.to_datetime(s, errors="coerce", utc=True)
            # NaT -> NaN; fill with "" so NULLs compare equal.
            return dt.dt.strftime("%Y-%m-%d").fillna("")

        if as_bool:
            return s.fillna(False).apply(lambda v: "true" if v else "false")

        return s.fillna("").astype(str)

    _bool_cols = {"has_enroll_now"}
    _date_cols = {"start_date", "end_date"}

    existing_norm = existing_sessions_df[["barcode"] + comp_cols].copy()
    for c in comp_cols:
        existing_norm[c] = _norm_for_compare(existing_norm[c], as_date=(c in _date_cols), as_bool=(c in _bool_cols))

    new_norm = dfsess_clean[["barcode"] + comp_cols + ["_idx"]].copy()
    for c in comp_cols:
        new_norm[c] = _norm_for_compare(new_norm[c], as_date=(c in _date_cols), as_bool=(c in _bool_cols))

    merged = new_norm.merge(existing_norm, on="barcode", how="left", suffixes=("", "_db"))

    exists_mask = merged["barcode"].isin(existing_barcodes_set)
    changed_mask = exists_mask & (
        (merged["course_id"] != merged["course_id_db"])
        | (merged["centre_id"] != merged["centre_id_db"])
        | (merged["day_of_week"] != merged["day_of_week_db"])
        | (merged["start_time"] != merged["start_time_db"])
        | (merged["end_time"] != merged["end_time_db"])
        | (merged["start_date"] != merged["start_date_db"])
        | (merged["end_date"] != merged["end_date_db"])
        | (merged["min_age"] != merged["min_age_db"])
        | (merged["max_age"] != merged["max_age_db"])
        | (merged["session_url"] != merged["session_url_db"])
        | (merged["availability"] != merged["availability_db"])
        | (merged["has_enroll_now"] != merged["has_enroll_now_db"])
    )

    to_update_idx = merged.loc[changed_mask, "_idx"].tolist()
    if len(to_update_idx) > 0:
        to_update = dfsess_clean[dfsess_clean["_idx"].isin(to_update_idx)].drop(columns=["_idx"])
        to_update_exec = to_update[comp_cols + ["barcode"]].copy()
        _needed_upd = sorted(set(to_update["course_id"].dropna().tolist()))
        # FK: activities_sessions.course_id -> activities_courses.name (courses inserted later in script).
        with engine.begin() as conn:
            _insert_missing_courses_for_session_fk(
                conn,
                needed_course_ids=_needed_upd,
                dfcrs=dfcrs,
                season_mask=session_insert_mask,
                pk_prefix=pk_prefix,
                like_esc=_like_escape,
            )
            conn.execute(
                text(
                    "UPDATE activities_sessions "
                    "SET course_id = :course_id, centre_id = :centre_id, day_of_week = :day_of_week, "
                    "start_time = :start_time, end_time = :end_time, start_date = :start_date, end_date = :end_date, "
                    "min_age = :min_age, max_age = :max_age, session_url = :session_url, "
                    "availability = :availability, has_enroll_now = :has_enroll_now, "
                    "updated_at = NOW() "
                    "WHERE barcode = :barcode"
                ),
                to_update_exec.to_dict(orient="records"),
            )
        print(f"Updated {len(to_update)} existing sessions.")

# Stamp last_seen_at = NOW() for every barcode present in this scrape.
# This column tracks "when was this session last observed", independent of whether
# its data changed (updated_at). Used by export_only mode to identify the latest scrape.
seen_barcodes = dfsess_clean["barcode"].tolist()
with engine.begin() as conn:
    conn.execute(
        text(
            "ALTER TABLE activities_sessions "
            "ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ"
        )
    )
    conn.execute(
        text(
            "UPDATE activities_sessions "
            "SET last_seen_at = NOW() "
            "WHERE barcode = ANY(:barcodes)"
        ),
        {"barcodes": seen_barcodes},
    )
print(f"Stamped last_seen_at for {len(seen_barcodes)} seen sessions.")


############################################################################################################
# insert into 'series' in db
############################################################################################################
# Build dfcrs-equivalent using all sessions currently in the DB for this season.
# This includes: rows that already existed in DB + any sessions we just inserted above.
#
# For each DB session row, we want to recover (program, series, Name, crs_name, Description)
# so the existing series/course aggregation logic below can run unchanged.
df_sessions_all = pd.read_sql(
    text(
        "SELECT "
        "s.barcode, s.course_id, s.day_of_week, s.start_time, s.end_time, "
        "s.start_date, s.end_date, s.min_age, s.max_age, "
        "c.series_id, "
        "c.crs_name AS crs_name_db_prefixed, "
        "c.description AS description_db "
        "FROM activities_sessions s "
        "LEFT JOIN activities_courses c ON c.name = s.course_id "
        "WHERE s.barcode LIKE :prefix ESCAPE '\\' "
        "AND (s.start_date >= :cutoff OR s.has_enroll_now = TRUE)"
    ),
    con=engine,
    params={"prefix": existing_sessions_prefix, "cutoff": startdate_cutoff_date},
)

# Derive Name from course_id (everything after pk_prefix in course_id is the Name segment).
_name_split = df_sessions_all["course_id"].astype("string").str.split(pk_prefix, n=1, expand=True)
df_sessions_all["Name"] = _name_split[1]

# Derive program/series from series_id when available (series_id = program + pk_prefix + series).
_series_split = df_sessions_all["series_id"].astype("string").str.split(pk_prefix, n=1, expand=True)
df_sessions_all["program_db"] = _series_split[0]
df_sessions_all["series_db"] = _series_split[1]

# Derive base crs_name from activities_courses.crs_name (stored as program + SERIES.UPPER + pk_prefix + crs_name_base).
_crs_split = df_sessions_all["crs_name_db_prefixed"].astype("string").str.split(pk_prefix, n=1, expand=True)
df_sessions_all["crs_name_db"] = _crs_split[1]

# Scraped course metadata for the current run (used only to fill gaps where the course row
# doesn't exist in DB yet, e.g. brand new courses).
df_course_meta_scraped = dfcrs[session_insert_mask][["program", "series", "Name", "crs_name", "Description"]].copy()
df_course_meta_scraped["course_id"] = (
    df_course_meta_scraped["program"]
    + df_course_meta_scraped["series"].str.upper()
    + pk_prefix
    + df_course_meta_scraped["Name"]
)
df_course_meta_scraped = df_course_meta_scraped.groupby("course_id", as_index=False).agg(
    {"program": "first", "series": "first", "crs_name": "first", "Description": "first"}
)
df_course_meta_scraped = df_course_meta_scraped.rename(
    columns={"program": "program_scr", "series": "series_scr", "crs_name": "crs_name_scr", "Description": "Description_scr"}
)

df_sessions_all = df_sessions_all.merge(df_course_meta_scraped, on="course_id", how="left")

# Final dfcrs-equivalent (session-level rows) so downstream groupbys work as before.
dfcrs_equiv = df_sessions_all.copy()
dfcrs_equiv["Course Number"] = dfcrs_equiv["barcode"]
dfcrs_equiv["program"] = dfcrs_equiv["program_db"].fillna(dfcrs_equiv["program_scr"])
dfcrs_equiv["series"] = dfcrs_equiv["series_db"].fillna(dfcrs_equiv["series_scr"])
dfcrs_equiv["crs_name"] = dfcrs_equiv["crs_name_db"].fillna(dfcrs_equiv["crs_name_scr"])
dfcrs_equiv["Description"] = dfcrs_equiv["description_db"].fillna(dfcrs_equiv["Description_scr"])

dfcrs_equiv = dfcrs_equiv[
    [
        "program",
        "series",
        "Name",
        "crs_name",
        "Course Number",
        "day_of_week",
        "start_time",
        "end_time",
        "start_date",
        "end_date",
        "min_age",
        "max_age",
        "Description",
    ]
]

dfseries = dfcrs_equiv
dfseries = dfseries.groupby(["program", "series"], as_index=False).agg(
    {"Course Number": "count", "min_age": "min", "max_age": "max"}
)

# Create series PK early (used to check DB + to upsert)
dfseries["name"] = dfseries["program"] + pk_prefix + dfseries["series"]
dfseries = dfseries.rename(columns={"Course Number": "session_count"})

# Load existing series rows for this season prefix (so we can reuse description + update changed fields)
_like_escape = lambda s: s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
# Series `name` is `program + pk_prefix + series`, so pk_prefix appears in the middle.
# Match by substring (escaped) instead of prefix match.
existing_series_prefix = "%" + _like_escape(pk_prefix) + "%"
existing_series_df = pd.read_sql(
    text(
        "SELECT name, min_age, max_age, session_count, description "
        "FROM activities_series "
        "WHERE name LIKE :prefix ESCAPE '\\'"
    ),
    con=engine,
    params={"prefix": existing_series_prefix},
)
existing_series_df["description"] = existing_series_df["description"].fillna("")
existing_series_by_name = existing_series_df.set_index("name", drop=False)

# Series-level description prompts (only needed for new series / missing descriptions)
dfseriesdesc = dfcrs_equiv[["program", "series", "Name", "Description"]].drop_duplicates()
dfseriesdesc["llm_prompt"] = dfseriesdesc["Name"] + ": " + dfseriesdesc["Description"]
dfseriesdesc = (
    dfseriesdesc[["program", "series", "llm_prompt"]]
    .groupby(["program", "series"])
    .agg({"llm_prompt": lambda x: list(x)})
    .reset_index()
)
dfseriesdesc["llm_prompt"] = dfseriesdesc.apply(
    lambda x: (
        f'''I want a brief & concise description/ summary for the "{x.series}" series of courses. Please don't make it too wordy. Keep it around 50 words ish.
        The summary should be brief/concise/easy-to-read yet succinct that captures essence of the series. Tone should be casual, human and verbal.
        Please don't use first person since these descriptions are not written by me.\n'''
        + str(x.llm_prompt)
    ),
    axis=1,
)
# Build target descriptions:
# - If series exists in DB and has non-empty description -> reuse DB (skip LLM)
# - If series is new OR DB description is empty -> generate via LLM
dfseries = dfseries.merge(
    existing_series_df[["name", "description"]].rename(columns={"description": "description_db"}),
    on="name",
    how="left",
)
dfseries["description_db"] = dfseries["description_db"].fillna("")
needs_llm = (dfseries["description_db"].str.strip() == "")

series_desc = dfseries.loc[needs_llm, ["program", "series"]].merge(
    dfseriesdesc[["program", "series", "llm_prompt"]],
    on=["program", "series"],
    how="left",
)
if len(series_desc) > 0:
    # LLM call per series (requires OPENAI_API_KEY); only for new/missing descriptions
    series_desc = series_desc.copy()
    series_desc["description"] = series_desc["llm_prompt"].apply(lambda p: su.get_crs_name_desc(p).content if isinstance(p, str) and p.strip() else "")
    series_desc = series_desc[["program", "series", "description"]]
else:
    series_desc = pd.DataFrame(columns=["program", "series", "description"])

# Persist generated descriptions for auditing / reuse
series_desc.to_csv(f"{season}/series_desc.csv", index=False)

# Merge: prefer DB description when present; otherwise use generated description
dfseries = dfseries.merge(series_desc, on=["program", "series"], how="left")
dfseries["description"] = dfseries["description_db"].where(dfseries["description_db"].str.strip() != "", dfseries["description"].fillna(""))
dfseries = dfseries.drop(columns=["description_db"])

# Insert new + update existing when fields changed (and only fill description when DB was empty)
dfseries["created_at"] = datetime.datetime.now()
dfseries_clean = dfseries[["name", "min_age", "max_age", "description", "session_count", "created_at"]].drop_duplicates()

existing_names = set(existing_series_df["name"])
to_insert = dfseries_clean[~dfseries_clean["name"].isin(existing_names)]
if len(to_insert) > 0:
    to_insert.to_sql("activities_series", con=engine, if_exists="append", index=False)
    print(f"Inserted {len(to_insert)} new series.")

# Updates: only for rows that already exist, and only if something changed.
to_compare = dfseries_clean[dfseries_clean["name"].isin(existing_names)].merge(
    existing_series_df,
    on="name",
    how="left",
    suffixes=("", "_db"),
)
if len(to_compare) > 0:
    # Only update description when the DB description is empty and we now have one.
    db_desc_empty = to_compare["description_db"].fillna("").str.strip() == ""
    desc_changed = db_desc_empty & (to_compare["description"].fillna("").str.strip() != "")

    fields_changed = (
        (to_compare["min_age"] != to_compare["min_age_db"])
        | (to_compare["max_age"] != to_compare["max_age_db"])
        | (to_compare["session_count"] != to_compare["session_count_db"])
        | desc_changed
    )
    to_update = to_compare[fields_changed][["name", "min_age", "max_age", "session_count", "description"]]

    if len(to_update) > 0:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE activities_series "
                    "SET min_age = :min_age, max_age = :max_age, session_count = :session_count, description = :description "
                    "WHERE name = :name"
                ),
                to_update.to_dict(orient="records"),
            )
        print(f"Updated {len(to_update)} existing series.")

############################################################################################################
# insert into 'courses' in db
############################################################################################################
# dfcourse = dfcrs.groupby(['program','Name','crs_name','series','Description'],as_index=False).agg({'Course Number':'count','min_age':'min','max_age':'max'})
dfcourse = dfcrs_equiv.groupby(['program','Name','crs_name','series'],as_index=False).agg({'Course Number':'count','min_age':'min','max_age':'max','Description':'first'})

# TODO: crs_name level description for crs_name with multiple descriptions
# count unique descriptions for each crs_name
dfgb = dfcourse.groupby(['program','series','crs_name'])['Description'].count().reset_index().sort_values('Description', ascending=False)

# get crs_name with >1 descriptions
dfcrsdesc = dfcourse.merge(dfgb[dfgb['Description']>1][['program','series','crs_name']], on=['program','series','crs_name'], how='inner')

# group mulitple descriptions into a list into a new column 'crs_name_desc'
dfcrsdesc['llm_prompt'] = dfcrsdesc['Name'] + ': ' + dfcrsdesc['Description']

dfcrsdesc = dfcrsdesc[['program','series','crs_name','llm_prompt']]\
    .groupby(['program','series','crs_name']).agg({'llm_prompt': lambda x: list(x)}).reset_index()
dfcrsdesc['llm_prompt']= dfcrsdesc.apply(lambda x: f'create a paragraph summarizing the following list of course descriptions under the "{x.crs_name}" group of related courses:\n' + str(x.llm_prompt), axis=1) 
# dfcrsdesc.to_csv(f'{season}/dfcrsdesc.csv', index=False)
# make API calls to get crs_name_desc
#
# Optimization: if a given `crs_name` group's `crs_name_desc` already exists in the DB,
# skip regenerating it via LLM. Only call LLM for missing/empty DB values.
#
# NOTE: In DB, `activities_courses.crs_name` is stored with the same prefixing pattern
# used later for dfcourse['crs_name'] (program + SERIES.UPPER + pk_prefix + crs_name).
_existing_courses_prefix_for_llm = "%" + _like_escape(pk_prefix) + "%"
existing_crsdesc_df = pd.read_sql(
    text(
        "SELECT crs_name, crs_name_desc "
        "FROM activities_courses "
        "WHERE name LIKE :prefix ESCAPE '\\'"
    ),
    con=engine,
    params={"prefix": _existing_courses_prefix_for_llm},
)
existing_crsdesc_df["crs_name_desc"] = existing_crsdesc_df["crs_name_desc"].fillna("")

# Pick the first non-empty description per crs_name
def _pick_first_nonempty(series: pd.Series) -> str:
    for v in series.tolist():
        if isinstance(v, str) and v.strip():
            return v
    return ""

existing_crsdesc_map = (
    existing_crsdesc_df.groupby("crs_name")["crs_name_desc"]
    .apply(_pick_first_nonempty)
    .to_dict()
)

dfcrsdesc["crs_name_db_key"] = (
    dfcrsdesc["program"] + dfcrsdesc["series"].str.upper() + pk_prefix + dfcrsdesc["crs_name"]
)
dfcrsdesc["crs_name_desc"] = dfcrsdesc["crs_name_db_key"].map(existing_crsdesc_map).fillna("")

needs_llm = dfcrsdesc["crs_name_desc"].str.strip() == ""
if needs_llm.any():
    dfcrsdesc.loc[needs_llm, "crs_name_desc"] = dfcrsdesc.loc[needs_llm, "llm_prompt"].apply(
        lambda p: su.get_crs_name_desc(p).content
        if isinstance(p, str) and p.strip()
        else ""
    )

dfcrsdesc = dfcrsdesc.drop(columns=["crs_name_db_key"])

# for crs_name with only 1 description, use the description; for crs_name with multiple descriptions, use the LLM generated description
dfcourse = dfcourse.merge(dfcrsdesc[['program','series','crs_name','crs_name_desc']], on=['program','series','crs_name'], how='left')

dfcourse['crs_name_desc'] = dfcourse['crs_name_desc'].fillna(dfcourse['Description'])

# create primary key; 
dfcourse['name']        = dfcourse['program'] + dfcourse['series'].str.upper()+ pk_prefix + dfcourse['Name']
dfcourse['series_id']   = dfcourse['program'] + pk_prefix + dfcourse['series']
# drop/ rename columns as needed
dfcourse.rename(columns={'Course Number':'num_sessions','Description':'description', 'min_age':'min_age','max_age':'max_age'}, inplace=True)
dfcourse['created_at'] = datetime.datetime.now()

# fill max_age with 999 if null
dfcourse['max_age'] = dfcourse['max_age'].fillna(999)

# add prefix to crs_name
dfcourse['crs_name'] = dfcourse['program'] + dfcourse['series'].str.upper() + pk_prefix + dfcourse['crs_name']

# to db: reuse existing descriptions where present; insert new; update changed
dfcourse_clean = dfcourse[
    ["name", "series_id", "crs_name", "description", "min_age", "max_age", "num_sessions", "created_at", "crs_name_desc"]
].drop_duplicates()

existing_courses_prefix = "%" + _like_escape(pk_prefix) + "%"
existing_courses_df = pd.read_sql(
    text(
        "SELECT name, series_id, crs_name, description, min_age, max_age, num_sessions, crs_name_desc "
        "FROM activities_courses "
        "WHERE name LIKE :prefix ESCAPE '\\'"
    ),
    con=engine,
    params={"prefix": existing_courses_prefix},
)
existing_courses_df[["description", "crs_name_desc"]] = existing_courses_df[["description", "crs_name_desc"]].fillna("")

# Prefer existing DB descriptions / crs_name_desc when present
dfcourse_clean = dfcourse_clean.merge(
    existing_courses_df[["name", "description", "crs_name_desc"]].rename(
        columns={"description": "description_db", "crs_name_desc": "crs_name_desc_db"}
    ),
    on="name",
    how="left",
)
dfcourse_clean[["description_db", "crs_name_desc_db"]] = dfcourse_clean[["description_db", "crs_name_desc_db"]].fillna("")

desc_from_db = dfcourse_clean["description_db"].str.strip() != ""
crs_name_desc_from_db = dfcourse_clean["crs_name_desc_db"].str.strip() != ""

dfcourse_clean["description"] = dfcourse_clean["description_db"].where(
    desc_from_db, dfcourse_clean["description"].fillna("")
)
dfcourse_clean["crs_name_desc"] = dfcourse_clean["crs_name_desc_db"].where(
    crs_name_desc_from_db, dfcourse_clean["crs_name_desc"].fillna("")
)
dfcourse_clean = dfcourse_clean.drop(columns=["description_db", "crs_name_desc_db"])

existing_course_names = set(existing_courses_df["name"])

# Inserts: only for entirely new courses
to_insert = dfcourse_clean[~dfcourse_clean["name"].isin(existing_course_names)]
if len(to_insert) > 0:
    to_insert.to_sql("activities_courses", con=engine, if_exists="append", index=False)
    print(f"Inserted {len(to_insert)} new courses.")

# Updates: for existing courses where fields changed; avoid overwriting non-empty DB descriptions
to_compare = dfcourse_clean[dfcourse_clean["name"].isin(existing_course_names)].merge(
    existing_courses_df,
    on="name",
    how="left",
    suffixes=("", "_db"),
)
if len(to_compare) > 0:
    db_desc_empty = to_compare["description_db"].fillna("").str.strip() == ""
    db_crs_name_desc_empty = to_compare["crs_name_desc_db"].fillna("").str.strip() == ""

    desc_changed = db_desc_empty & (to_compare["description"].fillna("").str.strip() != "")
    crs_name_desc_changed = db_crs_name_desc_empty & (
        to_compare["crs_name_desc"].fillna("").str.strip() != ""
    )

    fields_changed = (
        (to_compare["series_id"] != to_compare["series_id_db"])
        | (to_compare["crs_name"] != to_compare["crs_name_db"])
        | (to_compare["min_age"] != to_compare["min_age_db"])
        | (to_compare["max_age"] != to_compare["max_age_db"])
        | (to_compare["num_sessions"] != to_compare["num_sessions_db"])
        | desc_changed
        | crs_name_desc_changed
    )

    to_update = to_compare[fields_changed][
        ["name", "series_id", "crs_name", "description", "min_age", "max_age", "num_sessions", "crs_name_desc"]
    ]

    if len(to_update) > 0:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE activities_courses "
                    "SET series_id = :series_id, crs_name = :crs_name, description = :description, "
                    "min_age = :min_age, max_age = :max_age, num_sessions = :num_sessions, crs_name_desc = :crs_name_desc "
                    "WHERE name = :name"
                ),
                to_update.to_dict(orient="records"),
            )
        print(f"Updated {len(to_update)} existing courses.")

###################################################################################################################################
# insert into 'course_name' in db (this is a level up from courses) (e.g. Guardian Swim 1, instead of Guardian Swim 1 (girls))
#############################################################################################################1#####################
dfcrsname = dfcourse.copy()
# dfcrsname['name'] = dfcrsname['program'] + dfcrsname['series'].str.upper() + pk_prefix + dfcrsname['crs_name']
dfcrsname['name'] =  dfcrsname['crs_name']

dfcrsname = dfcrsname.groupby(['program','series','name','crs_name_desc'],as_index=False)\
    .agg({'num_sessions':'sum','min_age':'min','max_age':'max'}).rename(columns={'Course Number':'num_sessions'})

# pk should be crs_name
dfcrsname['series_id'] = dfcrsname['program'] + pk_prefix + dfcrsname['series']

# fill max_age with 999 if null
dfcrsname['max_age'] = dfcrsname['max_age'].fillna(999)

# to db (skip existing)
dfcrsname_clean = dfcrsname[['name','series_id','crs_name_desc','min_age','max_age','num_sessions']].drop_duplicates()

# Query existing coursenames for this pk_prefix region (pk_prefix is embedded mid-string in `name`)
existing_coursenames_prefix = "%" + _like_escape(pk_prefix) + "%"
existing_coursenames_df = pd.read_sql(
    text(
        "SELECT name, series_id, crs_name_desc, min_age, max_age, num_sessions "
        "FROM activities_coursenames "
        "WHERE name LIKE :prefix ESCAPE '\\'"
    ),
    con=engine,
    params={"prefix": existing_coursenames_prefix},
)
existing_coursenames_df[["crs_name_desc"]] = existing_coursenames_df[["crs_name_desc"]].fillna("")

existing_coursenames_set = set(existing_coursenames_df["name"])

# Keep the newly computed description, but only fall back to the DB description
# when the newly computed one is empty (so we don't overwrite with blank).
dfcrsname_clean = dfcrsname_clean.merge(
    existing_coursenames_df[["name", "crs_name_desc"]].rename(columns={"crs_name_desc": "crs_name_desc_db"}),
    on="name",
    how="left",
)
dfcrsname_clean["crs_name_desc_db"] = dfcrsname_clean["crs_name_desc_db"].fillna("")
dfcrsname_clean["crs_name_desc"] = dfcrsname_clean["crs_name_desc"].fillna("")
dfcrsname_clean["crs_name_desc"] = dfcrsname_clean["crs_name_desc"].where(
    dfcrsname_clean["crs_name_desc"].str.strip() != "",
    dfcrsname_clean["crs_name_desc_db"],
)
dfcrsname_clean = dfcrsname_clean.drop(columns=["crs_name_desc_db"])

# Inserts
to_insert = dfcrsname_clean[~dfcrsname_clean["name"].isin(existing_coursenames_set)]
if len(to_insert) > 0:
    to_insert.to_sql("activities_coursenames", con=engine, if_exists="append", index=False)
    print(f"Inserted {len(to_insert)} new course names.")

# Updates (only if fields changed; write description when it changed)
to_compare = dfcrsname_clean[dfcrsname_clean["name"].isin(existing_coursenames_set)].merge(
    existing_coursenames_df,
    on="name",
    how="left",
    suffixes=("", "_db"),
)
if len(to_compare) > 0:
    to_compare["crs_name_desc_db"] = to_compare["crs_name_desc_db"].fillna("")
    # Only overwrite when the new value is non-empty and actually different.
    desc_should_write = (
        (to_compare["crs_name_desc"].fillna("").str.strip() != "")
        & (to_compare["crs_name_desc"].fillna("").str.strip() != to_compare["crs_name_desc_db"].str.strip())
    )

    fields_changed = (
        (to_compare["series_id"] != to_compare["series_id_db"])
        | (to_compare["min_age"] != to_compare["min_age_db"])
        | (to_compare["max_age"] != to_compare["max_age_db"])
        | (to_compare["num_sessions"] != to_compare["num_sessions_db"])
        | desc_should_write
    )

    to_update = to_compare[fields_changed][
        ["name", "series_id", "crs_name_desc", "min_age", "max_age", "num_sessions"]
    ]

    if len(to_update) > 0:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE activities_coursenames "
                    "SET series_id = :series_id, crs_name_desc = :crs_name_desc, "
                    "min_age = :min_age, max_age = :max_age, num_sessions = :num_sessions "
                    "WHERE name = :name"
                ),
                to_update.to_dict(orient="records"),
            )
        print(f"Updated {len(to_update)} existing course names.")


# series level description
# '''
# PROMPT: 
#   I want a brief description/ summary for each program. Using series_desc.csv in spring26 folder, create a paragraph summarizing the list of program descriptions into a description for that program.
#   Save the result to programs_desc.csv with columns program & description.  Do not use external API. 
#   Use Cursor AI to generate the description. I don't want a script - I want you (the AI) to generate the description row by row.
#   Create summary by reading each description fully that corresponds to a program then generate a summary for that program.
#   The summary should be a single paragraph that captures the essence of the program. Tone should be casual, human and verbal.
#   Ensure to read the FULL description for each series and DO NOT truncate any description before reading the next description. Donnot use first person pronouns.
# '''

