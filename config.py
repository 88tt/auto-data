"""
Shared config for the pipeline (scripts 1, 2, 3). Override with env vars.
"""
import os

# ---------------------------------------------------------------------------
# Run / season (used by scripts 1, 2, 3)
# ---------------------------------------------------------------------------
CITY = os.environ.get("CITY", "To")
YEAR_AND_SEASON = os.environ.get("YEAR_AND_SEASON", os.environ.get("SEASON", "2026s2To"))
RAW_DATA_DIR = os.environ.get("RAW_DATA_DIR", f"raw_data/{CITY}/{YEAR_AND_SEASON}")
pk_prefix = os.environ.get("PK_PREFIX", f"{YEAR_AND_SEASON}_%_")
# Only insert sessions on or after this date (e.g. start of current season)
STARTDATE_CUTOFF = os.environ.get("STARTDATE_CUTOFF", "2026-03-01")


def _latest_scrape_dir():
    """Path to the latest scrapedYYYYMMDD subfolder under RAW_DATA_DIR. Falls back to RAW_DATA_DIR if none."""
    if not os.path.isdir(RAW_DATA_DIR):
        return RAW_DATA_DIR
    subs = [
        d for d in os.listdir(RAW_DATA_DIR)
        if d.startswith("scraped") and os.path.isdir(os.path.join(RAW_DATA_DIR, d))
    ]
    if not subs:
        return RAW_DATA_DIR
    return os.path.join(RAW_DATA_DIR, sorted(subs)[-1])


# Alias so scripts 2 & 3 use the latest scrape run; script 1 writes into a new scrapedYYYYMMDD.
season = _latest_scrape_dir()

# ---------------------------------------------------------------------------
# Script 3: database
# ---------------------------------------------------------------------------
# Support DATABASE_URL (Neon/CI format) or DB_URL; fall back to local dev default.
DB_URL = os.environ.get("DATABASE_URL", os.environ.get("DB_URL", "postgresql://localhost:5432/dev"))


def _env_bool(name: str, default: bool = False) -> bool:
    """Parse common truthy env-var values (1/true/yes/on)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "t", "yes", "y", "on"}


# Script 4 (4_db_to_json.py): default OUT_DIR is ../kebu-lite/public/data when that path exists;
# otherwise ./output. Override with env OUT_DIR. Writes flat JSON (latest export only).

# Script 4 export behavior:
# - False (default): export all sessions in DB matching season pattern.
# - True: export only sessions present in latest scrape run (config.season/dfcrs.csv).
EXPORT_LATEST_SCRAPE_ONLY = _env_bool("EXPORT_LATEST_SCRAPE_ONLY", True)
