"""Api of the Smashrun component."""

import asyncio
import io
import logging

from PIL import Image, ImageDraw, ImageFont
from staticmap import IconMarker, Line, StaticMap

from homeassistant.util import dt as dt_util

from .const import (
    NOMINATIN_GEOCODE_BASE_URL,
    NOMINATIN_GEOCODE_END_URL,
    SMASHRUN_ACCESS_TOKEN_QUERY,
    SMASHRUN_ACTIVITIES_URL,
    SMASHRUN_RUN_BASE,
    SMASHRUN_RUN_QUERY,
    SMASHRUN_STATS_BASE,
    SMASHRUN_STATS_URL,
    SMASHRUN_TRAILING_URL,
)

_LOGGER = logging.getLogger(__name__)


def compute_km_marks(activity: dict, km_interval: int = 1) -> list[dict]:
    """Build per-km checkpoints directly from Smashrun's recorded telemetry.

    Uses the device's own cumulative distance (already GPS-filtered) instead
    of recomputing it from raw lat/lon, so checkpoints line up with the
    official distance and the final partial km is never dropped.
    """
    keys = activity["recordingKeys"]
    values = activity["recordingValues"]
    idx = {k: i for i, k in enumerate(keys)}

    distances = values[idx["distance"]]
    lats = values[idx["latitude"]]
    lons = values[idx["longitude"]]

    def _valid_position(i):
        """Return the nearest valid (lon, lat) at or before index i."""
        j = i
        while j > 0 and (lats[j] == -1.0 or lons[j] == -1.0):
            j -= 1
        return lons[j], lats[j]

    n = len(distances)
    marks = []
    next_mark = km_interval

    for i in range(1, n):
        d_prev, d_curr = distances[i - 1], distances[i]

        while d_curr >= next_mark > d_prev:
            fraction = (next_mark - d_prev) / (d_curr - d_prev) if d_curr != d_prev else 0

            if lats[i] != -1.0 and lons[i] != -1.0 and lats[i - 1] != -1.0 and lons[i - 1] != -1.0:
                lat = lats[i - 1] + (lats[i] - lats[i - 1]) * fraction
                lon = lons[i - 1] + (lons[i] - lons[i - 1]) * fraction
            else:
                lon, lat = _valid_position(i)

            marks.append({
                "distance": next_mark,
                "lat": lat,
                "lon": lon,
            })

            next_mark += km_interval

    return marks


def _label_icon(text: str, bg: str) -> io.BytesIO:
    """Small circular PNG icon with a letter, used as a map marker."""
    size = 30
    icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(icon)
    draw.ellipse((0, 0, size - 1, size - 1), fill=bg, outline="white", width=2)

    font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    x = (size - text_w) / 2 - bbox[0]
    y = (size - text_h) / 2 - bbox[1]
    draw.text((x, y), text, fill="white", font_size=10)

    buf = io.BytesIO()
    icon.save(buf, format="PNG")
    buf.seek(0)  # rewind so Image.open can read it from the start
    return buf


async def fetch_image_data(run: dict):
    """Fetch Smashrun route coords and render a map image using OpenStreetMap tiles."""
    keys = run["recordingKeys"]
    values = run["recordingValues"]
    idx = {k: i for i, k in enumerate(keys)}

    lats = values[idx["latitude"]]
    lons = values[idx["longitude"]]

    route = [(lon, lat) for lon, lat in zip(lons, lats) if (lon, lat) != (-1.0, -1.0)]

    start, finish = route[0], route[-1]

    m = StaticMap(
        1200,
        800,
        url_template="https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    )
    m.add_line(Line(route, "#3388ff", 6))

    for mark in compute_km_marks(run):
        label = f"{mark['distance']}"  # "1", "2", ...
        m.add_marker(
            IconMarker(
                (mark["lon"], mark["lat"]), _label_icon(label, "#3388ff"), 15, 15
            )
        )

    m.add_marker(IconMarker(start, _label_icon("S", "#e74c3c"), 15, 15))
    m.add_marker(IconMarker(finish, _label_icon("F", "#2ecc71"), 15, 15))

    # m.render() does blocking network I/O for tile fetching, so push it
    # off the event loop instead of blocking the whole async app
    image = await asyncio.to_thread(m.render)

    buf = io.BytesIO()
    image.save(buf, format="PNG")

    run["map_image"] = buf.getvalue()
    run["map_image_last_updated"] = dt_util.now()


async def fetch_latest_run(client, token):
    """Fetch the most recent run data."""
    resp = await client.get(f"{SMASHRUN_ACTIVITIES_URL}{token}")
    data = resp.json()
    activity_id = data[0]["activityId"]
    run_resp = await client.get(
        f"{SMASHRUN_RUN_BASE}{activity_id}{SMASHRUN_RUN_QUERY}{token}"
    )
    run = run_resp.json()
    run["startDateTimeLocal"] = dt_util.parse_datetime(run["startDateTimeLocal"])
    return run


async def enrich_with_stats(client, token, run: dict):
    """Add distance statistics to the run."""
    stats_resp = await client.get(f"{SMASHRUN_STATS_URL}{token}")
    stats = stats_resp.json()
    run["totalDistance"] = round(stats["totalDistance"])
    run["run_count"] = stats["runCount"]

    now = dt_util.now()
    cy = f"/{now.year}"
    cm = f"/{now.month}"

    cm_resp = await client.get(
        f"{SMASHRUN_STATS_BASE}{cy}{cm}{SMASHRUN_ACCESS_TOKEN_QUERY}{token}"
    )
    run["cmDistance"] = round(cm_resp.json()["totalDistance"])

    cy_resp = await client.get(
        f"{SMASHRUN_STATS_BASE}{cy}{SMASHRUN_ACCESS_TOKEN_QUERY}{token}"
    )
    run["cyDistance"] = round(cy_resp.json()["totalDistance"])


async def enrich_with_vo2(run: dict):
    """Add VO2 calculation to the run."""
    keys = run["recordingKeys"]
    values = run["recordingValues"]

    if keys and "distance" in keys and "clock" in keys and "elevation" in keys:
        distance = values[keys.index("distance")]
        clock = values[keys.index("clock")]
        elevation = values[keys.index("elevation")]

        vo2_total = 0

        for i in range(len(distance) - 1):
            dc = clock[i + 1] - clock[i]
            dd = distance[i + 1] - distance[i]
            de = elevation[i + 1] - elevation[i]

            if dc == 0 or dd == 0:
                continue

            s = 1000 * dd / (dc / 60)
            g = de / (1000 * dd)
            v = 0.2 * s + 0.9 * s * g + 3.5

            vo2_total += v * (dc / 60)

        run["vo2_max"] = round(vo2_total / (clock[-1] / 60), 2)


async def get_latest_run_data(client, token):
    """Fetch the most recent run data."""
    resp_run = await client.get(f"{SMASHRUN_ACTIVITIES_URL}{token}")
    run = resp_run.json()[0]

    url_geo = (
        NOMINATIN_GEOCODE_BASE_URL
        + str(run["startLatitude"])
        + "&lon="
        + str(run["startLongitude"])
        + NOMINATIN_GEOCODE_END_URL
    )
    geo_resp = await client.get(url_geo)
    result = geo_resp.json()["address"]

    country = result["country"]

    if "city" in result:
        locality = result["city"]
    elif "town" in result:
       locality = result["town"]
    elif "village" in result:
       locality = result["village"]
    elif "suburb" in result:
       locality = result["suburb"]

    if "neighbourhood" in result:
        hood = result["neighbourhood"]
    else:
        hood = locality

    run.update({"Hood": hood, "Location": locality, "Country": country})

    return run


async def add_trailings(client, token, run: dict):
    """Add trailings calculation to the run."""
    now = dt_util.now()
    now_ts = now.timestamp()
    one_year_ago_ts = now_ts - 365 * 86400

    tra_resp = await client.get(
        f"{SMASHRUN_TRAILING_URL}{one_year_ago_ts}&access_token={token}"
    )
    activities = tra_resp.json()

    for act in activities:
        act_date = dt_util.parse_datetime(act["startDateTimeLocal"]).date()
        act["days_ago"] = (now.date() - act_date).days

    periods = [7, 30, 90, 365]

    for days in periods:
        total_distance = sum(
            act["distance"] for act in activities if act["days_ago"] < days
        )
        run[f"trailing_{days}_days"] = total_distance
