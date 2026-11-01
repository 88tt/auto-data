"""
Screen exported sessions for centres whose Google geocode is clearly wrong
(used by 4_db_to_json.py; never touches the DB).

Centre coordinates come from Google Places (3_insert_to_db.py). Usually right,
but some scraped "locations" aren't venues at all, e.g.
  - "Gymnasium - South": a room inside Etobicoke Olympium. Google matched it
    to U of T's Goldring Centre, 15 km away.
  - "Adapted and Inclusive Online Programming": online. Google matched it to
    an Etobicoke office.

Steps, per centre:
  1. Look up the City's location for the centre's sessions. Course ID (from
     session_url) -> Location ID (open data registered-programs.csv) -> City
     lat/lng (centres.json, else {id}/info.json). City coordinates outside
     Toronto are ignored: they have had typos, e.g. St. Michael Catholic
     School at latitude 45.6 instead of 43.6.
  2. Google and City within FLAG_KM of each other: keep.
  3. Otherwise -- City location more than FLAG_KM away, or no usable City
     location at all (course not in the open data, no City coordinates, or
     City coordinates outside Toronto) -- ask gpt-4o-mini (the model 3_insert_to_db.py already uses for
     descriptions) whether the name is a real venue, a room/sub-space or
     online:
       venue  -> keep the Google location
       room   -> drop the centre's sessions
       online -> keep them, flagged is_online with no address or coordinates
                 (kebu-lite lists them as "Online" instead of a map pin)
     Names with "online" or "virtual" in them are treated as online without
     asking. If OpenAI fails, or the open data can't be downloaded at all
     (so nothing can be compared), the centre is kept as is.

Verdicts are cached in location_verdicts.json, keyed by centre name (without
the season prefix) and Google address. CI doesn't commit the file back, so it
asks again each run (a handful of gpt-4o-mini calls at temperature 0); add an
entry to the committed file to pin or overrule a verdict. Every decision is
printed and written to the job summary.
"""

import csv
import io
import json
import math
import os
import re
from collections import Counter
from datetime import date
from pathlib import Path

import requests

REGISTERED_PROGRAMS_CSV = (
    "https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/1a5be46a-4039-48cd-a2d2-8e702abf9516"
    "/resource/eef9f05c-8488-429b-862c-0f2e2e0d92a4/download/registered-programs.csv"
)
CITY_CENTRES_JSON = "https://www.toronto.ca/data/parks/live/locations/centres.json"
CITY_INFO_JSON = "https://www.toronto.ca/data/parks/live/locations/{id}/info.json"
USER_AGENT = "kebu-data-prep/1.0"

VERDICTS_PATH = Path(os.environ.get("LOCATION_VERDICTS", Path(__file__).with_name("location_verdicts.json")))
FLAG_KM = 2.0
TORONTO_BOX = (43.55, 43.87, -79.65, -79.10)  # lat min/max, lng min/max
ONLINE_RE = re.compile(r"\b(online|virtual)\b", re.I)
COURSE_ID_RE = re.compile(r"/(\d+)(?:\?|$)")
VERDICTS = {"venue", "room", "online"}

LLM_PROMPT = """A City of Toronto recreation program lists its location as "{name}".
A Google Maps search for that name found: {google_address}.
{city_line}

Is "{name}" the name of a real venue (a community centre, school, park, arena,
library or other building you can travel to), a room or sub-space inside a
building (e.g. "Gymnasium - South", "Studio 2", "Pool Deck"), or an online
program?

Answer with JSON only: {{"verdict": "venue" | "room" | "online", "reason": "<one short sentence>"}}"""


def _strip_prefix(centre: str) -> str:
    """'2026s4To_%_Gymnasium - South' -> 'Gymnasium - South'."""
    return centre.split("_%_", 1)[-1].strip()


def _in_toronto(lat, lng) -> bool:
    return lat is not None and lng is not None and TORONTO_BOX[0] < lat < TORONTO_BOX[1] and TORONTO_BOX[2] < lng < TORONTO_BOX[3]


def _km(lat1, lng1, lat2, lng2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def _course_id(session_url):
    m = COURSE_ID_RE.search(session_url or "")
    return m.group(1) if m else None


def ask_llm(name, google_address, city=None, km=None):
    """{"verdict", "reason"} from gpt-4o-mini, or None if the call fails.
    city: {"name", "address"} of the City's location for the program, or None."""
    if not os.getenv("OPENAI_API_KEY"):
        print("location_screen: OPENAI_API_KEY not set -- keeping flagged centres as is")
        return None
    import openai
    from openai import OpenAI

    city_line = (
        f"The City's own records place this program at: {city['name']}, {city['address']}.\n"
        f"Those two places are {km:.0f} km apart."
        if city else "The City's own records have no location for this program."
    )
    prompt = LLM_PROMPT.format(name=name, google_address=google_address or "nothing", city_line=city_line)
    try:
        completion = OpenAI().chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
        )
        answer = json.loads(completion.choices[0].message.content)
    except (openai.OpenAIError, ValueError) as exc:
        # Same "log it, degrade gracefully" pattern as spider_utils.get_crs_name_desc.
        print(f"location_screen: OpenAI call failed for {name!r} ({type(exc).__name__}): {exc}")
        return None
    if answer.get("verdict") not in VERDICTS:
        print(f"location_screen: unexpected answer for {name!r}: {answer}")
        return None
    return {"verdict": answer["verdict"], "reason": str(answer.get("reason", "")).strip()}


class LocationScreen:
    def __init__(self, ask=ask_llm, verdicts_path: Path = VERDICTS_PATH, session=None):
        self.ask = ask
        self.verdicts_path = verdicts_path
        self.verdicts = json.loads(verdicts_path.read_text()) if verdicts_path.exists() else {}
        self.http = session or requests.Session()
        self.http.headers.setdefault("User-Agent", USER_AGENT)
        self._course_locations = None
        self._open_data_ok = False
        self._city_locations = None
        self._decided = {}  # centre -> "keep" | "room" | "online"
        self.log = []  # lines for the report

    # -- City data (loaded on first use; any failure means "no City match") --

    def course_locations(self) -> dict:
        if self._course_locations is None:
            self._course_locations = {}
            try:
                r = self.http.get(REGISTERED_PROGRAMS_CSV, timeout=120)
                r.raise_for_status()
                for row in csv.DictReader(io.StringIO(r.content.decode("utf-8-sig"))):
                    if row.get("Course_ID") and row.get("Location ID"):
                        self._course_locations[row["Course_ID"].strip()] = row["Location ID"].strip()
                self._open_data_ok = bool(self._course_locations)
            except (requests.RequestException, csv.Error) as exc:
                print(f"location_screen: registered-programs.csv unavailable ({exc}) -- screening skipped")
        return self._course_locations

    def city_location(self, location_id: str):
        """{"name", "address", "lat", "lng"} or None."""
        if self._city_locations is None:
            self._city_locations = {}
            try:
                r = self.http.get(CITY_CENTRES_JSON, timeout=60)
                r.raise_for_status()
                for c in r.json().get("all", []):
                    self._city_locations[str(c["ID"])] = {
                        "name": c.get("Name"), "address": " ".join(str(c.get("Address", "")).split()),
                        "lat": float(c["lat"]), "lng": float(c["lng"]),
                    }
            except (requests.RequestException, ValueError, KeyError) as exc:
                print(f"location_screen: centres.json unavailable ({exc})")
        if location_id not in self._city_locations:
            loc = None
            try:
                r = self.http.get(CITY_INFO_JSON.format(id=location_id), timeout=30)
                if r.ok:
                    info = r.json()
                    loc = {"name": info.get("title"), "address": " ".join(str(info.get("address", "")).split()),
                           "lat": float(info["lat"]), "lng": float(info["lng"])}
            except (requests.RequestException, ValueError, KeyError, TypeError):
                pass
            self._city_locations[location_id] = loc
        return self._city_locations[location_id]

    # -- Screening --

    def _decide(self, centre: str, rows: list) -> str:
        name = _strip_prefix(centre)
        first = rows[0]
        lat, lng, address = first.get("latitude"), first.get("longitude"), first.get("address")
        key = f"{name} | {address or ''}"

        if ONLINE_RE.search(name):
            self.log.append(f"online   {name} (name says online)")
            return "online"
        if key in self.verdicts:
            v = self.verdicts[key]["verdict"]
            self.log.append(f"{v:8} {name} (cached: {self.verdicts[key].get('reason', '')})")
            return "keep" if v == "venue" else v
        if lat is None or lng is None:
            return "keep"

        locations = self.course_locations()
        if not self._open_data_ok:
            return "keep"  # open data unavailable: nothing to compare, don't ask about every centre
        ids = Counter(locations[c] for c in (_course_id(r.get("session_url")) for r in rows) if c in locations)
        location_id = ids.most_common(1)[0][0] if ids else None
        city = self.city_location(location_id) if location_id else None
        if city and not _in_toronto(city["lat"], city["lng"]):
            city = None
        km = _km(lat, lng, city["lat"], city["lng"]) if city else None
        if km is not None and km <= FLAG_KM:
            return "keep"

        why = f"{km:.0f} km from City's {city['name']}" if city else "no City location"
        answer = self.ask(name, address, city, km)
        if answer is None:
            self.log.append(f"keep     {name} ({why}; LLM unavailable)")
            return "keep"
        self.verdicts[key] = {
            **answer,
            "google_address": address,
            "city_location": f"{city['name']}, {city['address']} (Location ID {location_id})" if city else None,
            "km": round(km, 1) if city else None,
            "checked": date.today().isoformat(),
        }
        self.log.append(f"{answer['verdict']:8} {name} ({why}: {answer['reason']})")
        return "keep" if answer["verdict"] == "venue" else answer["verdict"]

    def apply(self, sessions: list) -> list:
        """Sessions with room-name centres dropped and online ones flagged."""
        by_centre = {}
        for s in sessions:
            if s.get("centre"):
                by_centre.setdefault(s["centre"], []).append(s)
        for centre, rows in by_centre.items():
            if centre not in self._decided:
                self._decided[centre] = self._decide(centre, rows)

        out = []
        for s in sessions:
            decision = self._decided.get(s.get("centre"), "keep")
            if decision == "room":
                continue
            if decision == "online":
                s = {**s, "is_online": True, "latitude": None, "longitude": None, "address": None}
            out.append(s)
        return out

    def save(self) -> None:
        """Write new verdicts back to the cache file and print the report."""
        self.verdicts_path.write_text(json.dumps(self.verdicts, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
        dropped = sorted(_strip_prefix(c) for c, d in self._decided.items() if d == "room")
        online = sorted(_strip_prefix(c) for c, d in self._decided.items() if d == "online")
        report = "\n".join([f"dropped (room names): {', '.join(dropped) or 'none'}",
                            f"online: {', '.join(online) or 'none'}", *self.log])
        print("Location screen:\n" + report)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a") as f:
                f.write("## Location screen\n\n```\n" + report + "\n```\n")
