"""
Receiver location without user input.

Resolution order: explicit tool arguments, then the AETHERLINK_LATITUDE /
AETHERLINK_LONGITUDE environment variables, then a cached automatic result,
then IP geolocation (city-level, plenty for pass prediction and single-frame
ADS-B positions), then an estimate from decoded aircraft positions.
"""

import json
import logging
import os
import time
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

ENV_LAT = "AETHERLINK_LATITUDE"
ENV_LON = "AETHERLINK_LONGITUDE"
CACHE_FILE = "receiver_location.json"
CACHE_MAX_AGE_S = 7 * 24 * 3600
IP_TIMEOUT_S = 6


@dataclass
class Location:
    latitude: float
    longitude: float
    source: str                 # argument | environment | ip-geolocation | adsb-estimate
    description: str = ""       # e.g. "City, Region, Country"
    accuracy_km: float = 0.0
    timestamp: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def label(self) -> str:
        where = f"{self.latitude:.4f}, {self.longitude:.4f}"
        if self.description:
            where += f" ({self.description})"
        how = {
            "argument": "given",
            "environment": "from environment",
            "ip-geolocation": "auto-detected from IP address",
            "adsb-estimate": "estimated from aircraft positions",
        }.get(self.source, self.source)
        acc = f", ±{self.accuracy_km:.0f} km" if self.accuracy_km else ""
        return f"{where} [{how}{acc}]"


def _parse_ipapi_co(data: Dict[str, Any]):
    if "latitude" not in data or "longitude" not in data:
        return None
    desc = ", ".join(filter(None, [data.get("city"), data.get("region"), data.get("country_name")]))
    return float(data["latitude"]), float(data["longitude"]), desc


def _parse_ip_api_com(data: Dict[str, Any]):
    if data.get("status") != "success":
        return None
    desc = ", ".join(filter(None, [data.get("city"), data.get("regionName"), data.get("country")]))
    return float(data["lat"]), float(data["lon"]), desc


IP_PROVIDERS = (
    ("ipapi.co", "https://ipapi.co/json/", _parse_ipapi_co),
    ("ip-api.com", "http://ip-api.com/json/?fields=status,lat,lon,city,regionName,country", _parse_ip_api_com),
)


def _valid(lat: float, lon: float) -> bool:
    return -90 <= lat <= 90 and -180 <= lon <= 180


class LocationResolver:
    """Finds and remembers where the receiver is."""

    def __init__(self, cache_dir: Optional[str] = None):
        self.cache_dir = cache_dir
        self._cached: Optional[Location] = None
        self._load_cache()

    # ------------------------------------------------------------ cache

    def _cache_path(self) -> Optional[str]:
        return os.path.join(self.cache_dir, CACHE_FILE) if self.cache_dir else None

    def _load_cache(self):
        path = self._cache_path()
        if path and os.path.exists(path):
            try:
                with open(path) as f:
                    self._cached = Location(**json.load(f))
            except Exception as e:
                logger.debug(f"Could not read location cache: {e}")

    def _save_cache(self, loc: Location):
        self._cached = loc
        path = self._cache_path()
        if not path:
            return
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            with open(path, "w") as f:
                json.dump(loc.to_dict(), f, indent=2)
        except Exception as e:
            logger.debug(f"Could not write location cache: {e}")

    # ------------------------------------------------------------ sources

    @staticmethod
    def from_arguments(arguments: Optional[Dict[str, Any]]) -> Optional[Location]:
        if not arguments:
            return None
        lat = arguments.get("receiver_latitude", arguments.get("latitude"))
        lon = arguments.get("receiver_longitude", arguments.get("longitude"))
        if lat is None or lon is None:
            return None
        lat, lon = float(lat), float(lon)
        if not _valid(lat, lon):
            raise ValueError("Latitude must be between -90 and 90 and longitude between -180 and 180")
        return Location(lat, lon, "argument", timestamp=time.time())

    @staticmethod
    def from_environment() -> Optional[Location]:
        lat, lon = os.environ.get(ENV_LAT), os.environ.get(ENV_LON)
        if not lat or not lon:
            return None
        try:
            lat_f, lon_f = float(lat), float(lon)
        except ValueError:
            raise ValueError(f"{ENV_LAT} / {ENV_LON} must be decimal degrees")
        if not _valid(lat_f, lon_f):
            raise ValueError(f"{ENV_LAT} / {ENV_LON} out of range")
        return Location(lat_f, lon_f, "environment", timestamp=time.time())

    def cached(self, max_age_s: float = CACHE_MAX_AGE_S) -> Optional[Location]:
        loc = self._cached
        if loc and time.time() - loc.timestamp < max_age_s:
            return loc
        return None

    @staticmethod
    def ip_geolocate() -> Optional[Location]:
        """City-level location from the public IP address (blocking; ~1 s)."""
        for name, url, parse in IP_PROVIDERS:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "AetherLink-SDR-MCP"})
                with urllib.request.urlopen(req, timeout=IP_TIMEOUT_S) as resp:
                    data = json.loads(resp.read().decode(errors="replace"))
                parsed = parse(data)
                if parsed and _valid(parsed[0], parsed[1]):
                    lat, lon, desc = parsed
                    logger.info(f"Receiver location from {name}: {lat:.3f}, {lon:.3f} ({desc})")
                    return Location(lat, lon, "ip-geolocation", desc, accuracy_km=25.0, timestamp=time.time())
            except Exception as e:
                logger.debug(f"IP geolocation via {name} failed: {e}")
        return None

    # ------------------------------------------------------------ resolution

    def resolve(self, arguments: Optional[Dict[str, Any]] = None,
                allow_network: bool = True) -> Optional[Location]:
        """Best available location, or None if nothing can be determined."""
        loc = self.from_arguments(arguments) or self.from_environment()
        if loc:
            return loc
        loc = self.cached()
        if loc:
            # A fresh aircraft-derived estimate is physics, a fresh IP result is a guess; both beat a lookup
            return loc
        if allow_network:
            found = self.ip_geolocate()
            if found:
                self._save_cache(found)
                return found
        return self._cached  # stale is better than nothing

    def remember_estimate(self, latitude: float, longitude: float, accuracy_km: float = 50.0):
        """Store an aircraft-derived estimate. It replaces an IP guess (which it has just
        contradicted or refined) but never an explicit argument or environment setting,
        since those are not cached in the first place."""
        self._save_cache(Location(latitude, longitude, "adsb-estimate", accuracy_km=accuracy_km,
                                  timestamp=time.time()))
