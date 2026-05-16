"""
Reverse-geocode audit for activities_centres.

For each centre with stored lat/lng, calls the Google Geocoding API in reverse
and flags rows where the returned coordinates differ from the stored ones by
more than --distance-threshold metres (default 500m), or where the API call failed.

location_type (ROOFTOP / RANGE_INTERPOLATED / GEOMETRIC_CENTER / APPROXIMATE) is
recorded for reference but does not drive flagging.

Output: audit_geocoding_results.csv

Usage:
  python audit_geocoding.py [--prefix 2026s3To] [--distance-threshold 500]
"""

import argparse
import math
import os
import time

from dotenv import load_dotenv
load_dotenv()  # must run before config imports so DATABASE_URL is set

import pandas as pd
from sqlalchemy import create_engine, text

import config
from spider_utils import _google_api_get

SLEEP_BETWEEN_CALLS = 0.15  # seconds; well within the 50 QPS free-tier limit


def reverse_geocode(lat: float, lng: float) -> dict:
    """Call reverse geocoding API. Returns the first result dict, or {}."""
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {
        "latlng": f"{lat},{lng}",
        "key": os.getenv("GOOGLE_MAP_API_KEY"),
    }
    resp = _google_api_get(url, params, label=f"reverse-geocode ({lat:.5f},{lng:.5f})")
    if resp is None:
        return {}
    data = resp.json()
    if data.get("status") != "OK" or not data.get("results"):
        return {}
    return data["results"][0]


def haversine_m(lat1, lng1, lat2, lng2) -> float:
    """Straight-line distance in metres between two WGS-84 points."""
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def audit(prefix: str | None, distance_threshold_m: float, new_only: bool = False) -> pd.DataFrame:
    engine = create_engine(config.DB_URL, pool_pre_ping=True)

    query = "SELECT name, fullname, address, latitude, longitude FROM activities_centres WHERE latitude IS NOT NULL AND longitude IS NOT NULL"
    params: dict = {}
    if prefix:
        query += " AND name LIKE :prefix"
        params["prefix"] = prefix + "%"
    if new_only:
        query += " AND created_at >= NOW() - INTERVAL '48 hours'"
    query += " ORDER BY name"

    with engine.connect() as conn:
        df = pd.read_sql(text(query), conn, params=params)

    if df.empty:
        print("No new centres to audit." if new_only else "No centres found in DB matching the filter.")
        return df

    print(f"Auditing {len(df)} centre(s) — flag threshold: {distance_threshold_m}m")

    rows = []
    for i, row in df.iterrows():
        print(f"  [{i + 1}/{len(df)}] {row['name']}")
        result = reverse_geocode(row["latitude"], row["longitude"])
        time.sleep(SLEEP_BETWEEN_CALLS)

        if not result:
            rows.append({
                **row.to_dict(),
                "reverse_address": None,
                "location_type": None,
                "reverse_lat": None,
                "reverse_lng": None,
                "distance_m": None,
                "flag": True,
                "flag_reason": "reverse geocode failed",
            })
            continue

        reverse_address = result.get("formatted_address", "")
        geo = result.get("geometry", {})
        location_type = geo.get("location_type", "")
        reverse_lat = geo.get("location", {}).get("lat")
        reverse_lng = geo.get("location", {}).get("lng")

        distance_m = None
        if reverse_lat is not None and reverse_lng is not None:
            distance_m = haversine_m(row["latitude"], row["longitude"], reverse_lat, reverse_lng)

        flag = distance_m is None or distance_m > distance_threshold_m
        flag_reason = ""
        if distance_m is None:
            flag_reason = "reverse coords unavailable"
        elif distance_m > distance_threshold_m:
            flag_reason = f"distance {distance_m:.0f}m > threshold {distance_threshold_m:.0f}m"

        rows.append({
            **row.to_dict(),
            "reverse_address": reverse_address,
            "location_type": location_type,
            "reverse_lat": reverse_lat,
            "reverse_lng": reverse_lng,
            "distance_m": round(distance_m, 1) if distance_m is not None else None,
            "flag": flag,
            "flag_reason": flag_reason,
        })

    out = pd.DataFrame(rows)
    out = out.rename(columns={"address": "stored_address"})
    col_order = [
        "name", "fullname", "stored_address", "reverse_address",
        "location_type", "distance_m", "flag", "flag_reason",
        "latitude", "longitude", "reverse_lat", "reverse_lng",
    ]
    out = out[[c for c in col_order if c in out.columns]]

    flagged = out[out["flag"]]
    print(f"\nDone. {len(flagged)} / {len(out)} centres flagged.")
    if not flagged.empty:
        print(flagged[["name", "stored_address", "reverse_address", "distance_m", "flag_reason"]].to_string(index=False))

    return out


def reprocess(csv_path: str, distance_threshold_m: float) -> pd.DataFrame:
    """Re-apply flagging logic to an existing results CSV without any API calls."""
    df = pd.read_csv(csv_path)

    def _flag(row):
        d = row.get("distance_m")
        if d is None or (isinstance(d, float) and math.isnan(d)):
            return True, "reverse coords unavailable"
        if d > distance_threshold_m:
            return True, f"distance {d:.0f}m > threshold {distance_threshold_m:.0f}m"
        return False, ""

    df[["flag", "flag_reason"]] = df.apply(lambda r: pd.Series(_flag(r)), axis=1)
    df = df.drop(columns=["city_match"], errors="ignore")

    col_order = [
        "name", "fullname", "stored_address", "reverse_address",
        "location_type", "distance_m", "flag", "flag_reason",
        "latitude", "longitude", "reverse_lat", "reverse_lng",
    ]
    df = df[[c for c in col_order if c in df.columns]]

    flagged = df[df["flag"]]
    print(f"Done. {len(flagged)} / {len(df)} centres flagged.")
    if not flagged.empty:
        print(flagged[["name", "stored_address", "reverse_address", "distance_m", "flag_reason"]].to_string(index=False))
    return df


def main():
    parser = argparse.ArgumentParser(description="Reverse-geocode audit for activities_centres")
    parser.add_argument("--prefix", default=None, help="Filter centres by name prefix, e.g. 2026s3To (default: all)")
    parser.add_argument("--out", default="audit_geocoding_results.csv", help="Output CSV path")
    parser.add_argument("--distance-threshold", type=float, default=500, help="Flag if reverse coords differ by more than this many metres (default: 500)")
    parser.add_argument("--reprocess", metavar="CSV", default=None, help="Re-apply flagging to an existing results CSV instead of calling the API")
    parser.add_argument("--new-only", action="store_true", help="Only audit centres inserted in the last 48 hours")
    args = parser.parse_args()

    if args.reprocess:
        results = reprocess(csv_path=args.reprocess, distance_threshold_m=args.distance_threshold)
    else:
        results = audit(prefix=args.prefix, distance_threshold_m=args.distance_threshold, new_only=args.new_only)

    if not results.empty:
        results.to_csv(args.out, index=False)
        print(f"\nResults written to {args.out}")


if __name__ == "__main__":
    main()
