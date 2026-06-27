import json
import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

TRACKS_GEO_PATH = Path(__file__).with_name("tracks_geo.json")
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSRM_TABLE_URL = "https://router.project-osrm.org/table/v1/driving"
GEOCODE_SESSION = requests.Session()
GEOCODE_SESSION.headers.update({"User-Agent": "moscow_bot/1.0"})
ROUTE_CACHE_TTL = 300
_route_cache: Dict[Tuple[float, float], Tuple[datetime, Dict[str, "RouteInfo"]]] = {}


@dataclass(frozen=True)
class GeoPoint:
    lat: float
    lon: float
    label: Optional[str] = None


@dataclass(frozen=True)
class RouteInfo:
    distance_km: float
    duration_min: Optional[float] = None


def load_tracks_geo() -> Dict[str, Dict[str, Any]]:
    """Load full track metadata from tracks_geo.json."""
    with TRACKS_GEO_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def load_track_coords() -> Dict[str, GeoPoint]:
    """Load track coordinates from tracks_geo.json."""
    return {
        name: GeoPoint(lat=data["lat"], lon=data["lon"], label=name)
        for name, data in load_tracks_geo().items()
    }


def yandex_drive_url(
    lat: float,
    lon: float,
    from_geo: Optional[GeoPoint] = None,
) -> str:
    """Yandex Maps driving route to a track (from user point or current location)."""
    if from_geo:
        rtext = f"{from_geo.lat},{from_geo.lon}~{lat},{lon}"
    else:
        rtext = f"~{lat},{lon}"
    return f"https://yandex.ru/maps/?rtext={quote(rtext, safe=',~')}&rtt=auto"


def format_track_name_link(
    track_name: str,
    user_geo: Optional[GeoPoint] = None,
    max_len: Optional[int] = None,
) -> str:
    """Markdown link with the track name (same label as before, opens Yandex directions)."""
    tracks = load_tracks_geo()
    meta = tracks.get(track_name)

    label = track_name
    if max_len and len(label) > max_len:
        label = label[: max_len - 3] + "..."

    if not meta:
        return f"`{label}`"

    url = yandex_drive_url(meta["lat"], meta["lon"], user_geo)
    return f"[{label}]({url})"


def haversine_km(a: GeoPoint, b: GeoPoint) -> float:
    """Great-circle distance between two points in kilometers."""
    r = 6371.0
    lat1, lon1 = math.radians(a.lat), math.radians(a.lon)
    lat2, lon2 = math.radians(b.lat), math.radians(b.lon)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def format_distance_km(km: Optional[float]) -> str:
    if km is None:
        return "—"
    if km < 1:
        return f"{int(round(km * 1000))} м"
    return f"{km:.1f} км"


def format_drive_time(minutes: Optional[float]) -> str:
    if minutes is None:
        return "—"
    total = int(round(minutes))
    if total < 60:
        return f"{total} мин"
    hours, mins = divmod(total, 60)
    if mins:
        return f"{hours} ч {mins} мин"
    return f"{hours} ч"


def format_route_label(route: Optional[RouteInfo]) -> str:
    if route is None:
        return "—"
    if route.duration_min is not None:
        return f"{format_distance_km(route.distance_km)} · 🚗 {format_drive_time(route.duration_min)}"
    return format_distance_km(route.distance_km)


def geocode_query(query: str) -> Optional[GeoPoint]:
    """Resolve a free-text address to coordinates via Nominatim."""
    try:
        resp = GEOCODE_SESSION.get(
            NOMINATIM_URL,
            params={"q": query, "format": "json", "limit": 1},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return None
        item = data[0]
        return GeoPoint(
            lat=float(item["lat"]),
            lon=float(item["lon"]),
            label=item.get("display_name", query),
        )
    except Exception as e:
        logger.error(f"Geocoding failed for '{query}': {e}")
        return None


def geo_from_env() -> Optional[GeoPoint]:
    """Optional fixed user location from MY_LAT/MY_LON or MY_ADDRESS env vars."""
    lat_raw = os.getenv("MY_LAT")
    lon_raw = os.getenv("MY_LON")
    if lat_raw and lon_raw:
        try:
            return GeoPoint(float(lat_raw), float(lon_raw), label="env")
        except ValueError:
            logger.error("MY_LAT/MY_LON must be valid floats")

    address = os.getenv("MY_ADDRESS")
    if address:
        return geocode_query(address)

    return None


def geo_from_telegram_location(latitude: float, longitude: float) -> GeoPoint:
    return GeoPoint(lat=latitude, lon=longitude, label="Telegram")


def resolve_user_geo(
    stored: Optional[Dict[str, float]] = None,
) -> Optional[GeoPoint]:
    """Pick user location: saved Telegram/context data, then env fallback."""
    if stored and "lat" in stored and "lon" in stored:
        return GeoPoint(
            lat=stored["lat"],
            lon=stored["lon"],
            label=stored.get("label"),
        )
    return geo_from_env()


def track_distances(
    user_geo: GeoPoint,
    track_names: List[str],
    track_coords: Optional[Dict[str, GeoPoint]] = None,
) -> List[Tuple[str, Optional[float]]]:
    """Return (track_name, distance_km) pairs; None if track coords are missing."""
    coords = track_coords or load_track_coords()
    return [
        (name, haversine_km(user_geo, coords[name]) if name in coords else None)
        for name in track_names
    ]


def _route_cache_key(geo: GeoPoint) -> Tuple[float, float]:
    return (round(geo.lat, 4), round(geo.lon, 4))


def fetch_track_routes(
    user_geo: GeoPoint,
    track_names: List[str],
    track_coords: Optional[Dict[str, GeoPoint]] = None,
) -> Dict[str, RouteInfo]:
    """Driving distance and duration from user to each track via OSRM."""
    cache_key = _route_cache_key(user_geo)
    now = datetime.now()
    cached = _route_cache.get(cache_key)
    if cached and (now - cached[0]).total_seconds() < ROUTE_CACHE_TTL:
        return {name: cached[1][name] for name in track_names if name in cached[1]}

    coords = track_coords or load_track_coords()
    resolved = [(name, coords[name]) for name in track_names if name in coords]
    if not resolved:
        return {}

    routes: Dict[str, RouteInfo] = {}
    try:
        points = [user_geo] + [point for _, point in resolved]
        coord_str = ";".join(f"{point.lon},{point.lat}" for point in points)
        resp = GEOCODE_SESSION.get(
            f"{OSRM_TABLE_URL}/{coord_str}",
            params={"sources": "0", "annotations": "duration,distance"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != "Ok":
            raise RuntimeError(data.get("message", "OSRM table request failed"))

        durations = data["durations"][0]
        distances = data["distances"][0]
        for idx, (name, point) in enumerate(resolved, start=1):
            duration_sec = durations[idx]
            distance_m = distances[idx]
            if duration_sec is None or distance_m is None:
                routes[name] = RouteInfo(
                    distance_km=haversine_km(user_geo, point),
                    duration_min=None,
                )
            else:
                routes[name] = RouteInfo(
                    distance_km=distance_m / 1000.0,
                    duration_min=duration_sec / 60.0,
                )
    except Exception as e:
        logger.error(f"Driving routes lookup failed: {e}")
        for name, point in resolved:
            routes[name] = RouteInfo(
                distance_km=haversine_km(user_geo, point),
                duration_min=None,
            )

    _route_cache[cache_key] = (now, routes)
    return routes


def nearest_tracks(
    user_geo: GeoPoint,
    track_names: List[str],
    limit: int = 3,
) -> List[Tuple[str, RouteInfo]]:
    """Tracks sorted by driving time (fallback: straight-line distance)."""
    routes = fetch_track_routes(user_geo, track_names)
    ranked = list(routes.items())
    ranked.sort(
        key=lambda item: (
            item[1].duration_min if item[1].duration_min is not None else float("inf"),
            item[1].distance_km,
        )
    )
    return ranked[:limit]
