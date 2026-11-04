"""
Satellite pass prediction for Meteor-M LRPT reception.

Uses SGP4 (via the ``sgp4`` package) with TLEs fetched from Celestrak and a
plain topocentric look-angle computation, so no heavy astronomy stack is
required. Accuracy is a few seconds in pass timing, which is plenty for
deciding when to start a decode.
"""

import json
import logging
import math
import os
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    from sgp4.api import Satrec, jday
    SGP4_AVAILABLE = True
except ImportError:  # pragma: no cover
    Satrec = None
    jday = None
    SGP4_AVAILABLE = False

# NORAD catalogue numbers
NORAD_IDS = {
    "METEOR-M2-3": 57166,
    "METEOR-M2-4": 59051,
}
CELESTRAK_URL = "https://celestrak.org/NORAD/elements/gp.php?CATNR={catnr}&FORMAT=TLE"
TLE_MAX_AGE_S = 12 * 3600       # refresh TLEs twice a day
WGS84_A_KM = 6378.137
WGS84_F = 1.0 / 298.257223563
WGS84_E2 = WGS84_F * (2.0 - WGS84_F)


@dataclass
class SatellitePass:
    satellite: str
    aos: datetime                  # acquisition of signal (rises above horizon), UTC
    los: datetime                  # loss of signal, UTC
    max_elevation_time: datetime
    max_elevation_deg: float
    aos_azimuth_deg: float
    max_azimuth_deg: float
    los_azimuth_deg: float
    direction: str                 # northbound / southbound

    @property
    def duration_s(self) -> float:
        return (self.los - self.aos).total_seconds()

    def to_dict(self) -> Dict[str, Any]:
        def local(dt: datetime) -> str:
            return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        return {
            "satellite": self.satellite,
            "aos_utc": self.aos.isoformat(timespec="seconds"),
            "los_utc": self.los.isoformat(timespec="seconds"),
            "aos_local": local(self.aos),
            "los_local": local(self.los),
            "max_elevation_utc": self.max_elevation_time.isoformat(timespec="seconds"),
            "max_elevation_local": local(self.max_elevation_time),
            "max_elevation_deg": round(self.max_elevation_deg, 1),
            "aos_azimuth_deg": round(self.aos_azimuth_deg),
            "max_azimuth_deg": round(self.max_azimuth_deg),
            "los_azimuth_deg": round(self.los_azimuth_deg),
            "duration_s": int(self.duration_s),
            "direction": self.direction,
        }


def _gmst_rad(jd_ut1: float) -> float:
    """Greenwich mean sidereal time (IAU 1982) in radians."""
    t = (jd_ut1 - 2451545.0) / 36525.0
    seconds = (67310.54841 + (876600.0 * 3600.0 + 8640184.812866) * t
               + 0.093104 * t * t - 6.2e-6 * t * t * t)
    return math.radians((seconds % 86400.0) / 240.0)


def _geodetic_to_ecef(lat_deg: float, lon_deg: float, alt_km: float) -> Tuple[float, float, float]:
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    n = WGS84_A_KM / math.sqrt(1.0 - WGS84_E2 * math.sin(lat) ** 2)
    x = (n + alt_km) * math.cos(lat) * math.cos(lon)
    y = (n + alt_km) * math.cos(lat) * math.sin(lon)
    z = (n * (1.0 - WGS84_E2) + alt_km) * math.sin(lat)
    return x, y, z


class PassPredictor:
    """Predict passes of the Meteor-M satellites over an observer."""

    def __init__(self, cache_dir: Optional[str] = None):
        self.cache_dir = cache_dir
        self._tle_cache: Dict[str, Dict[str, Any]] = {}
        self._load_cache()

    # ------------------------------------------------------------ TLE handling

    def _cache_file(self) -> Optional[str]:
        return os.path.join(self.cache_dir, "tle_cache.json") if self.cache_dir else None

    def _load_cache(self):
        path = self._cache_file()
        if path and os.path.exists(path):
            try:
                with open(path) as f:
                    self._tle_cache = json.load(f)
            except Exception as e:
                logger.debug(f"Could not read TLE cache: {e}")

    def _save_cache(self):
        path = self._cache_file()
        if not path:
            return
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            with open(path, "w") as f:
                json.dump(self._tle_cache, f, indent=2)
        except Exception as e:
            logger.debug(f"Could not write TLE cache: {e}")

    def get_tle(self, satellite: str, force_refresh: bool = False) -> Dict[str, Any]:
        """Return {'name', 'line1', 'line2', 'fetched', 'age_hours', 'source'} for a satellite."""
        catnr = NORAD_IDS.get(satellite)
        if catnr is None:
            raise ValueError(f"Unknown satellite {satellite}. Known: {', '.join(NORAD_IDS)}")

        cached = self._tle_cache.get(satellite)
        if cached and not force_refresh and time.time() - cached["fetched"] < TLE_MAX_AGE_S:
            return self._describe(cached, "cache")

        url = CELESTRAK_URL.format(catnr=catnr)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "AetherLink-SDR-MCP"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                text = resp.read().decode(errors="replace").strip()
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            if len(lines) < 2 or not lines[-2].startswith("1 ") or not lines[-1].startswith("2 "):
                raise ValueError(f"Unexpected TLE response: {text[:80]!r}")
            entry = {
                "name": lines[0] if len(lines) >= 3 else satellite,
                "line1": lines[-2],
                "line2": lines[-1],
                "fetched": time.time(),
            }
            self._tle_cache[satellite] = entry
            self._save_cache()
            return self._describe(entry, "celestrak")
        except Exception as e:
            if cached:
                logger.warning(f"TLE refresh failed ({e}); using cached elements")
                return self._describe(cached, "stale-cache")
            raise RuntimeError(
                f"Could not download orbital elements for {satellite} from Celestrak ({e}). "
                "Check the network connection."
            )

    @staticmethod
    def _describe(entry: Dict[str, Any], source: str) -> Dict[str, Any]:
        return {
            "name": entry["name"],
            "line1": entry["line1"],
            "line2": entry["line2"],
            "fetched": entry["fetched"],
            "age_hours": round((time.time() - entry["fetched"]) / 3600.0, 1),
            "source": source,
        }

    # ------------------------------------------------------------ geometry

    @staticmethod
    def look_angles(sat: "Satrec", when: datetime, obs_ecef: Tuple[float, float, float],
                    lat_deg: float, lon_deg: float) -> Tuple[float, float, float, float]:
        """Return (elevation_deg, azimuth_deg, range_km, sub-satellite latitude_deg)."""
        jd, fr = jday(when.year, when.month, when.day, when.hour, when.minute,
                      when.second + when.microsecond / 1e6)
        err, r_teme, _ = sat.sgp4(jd, fr)
        if err != 0:
            raise RuntimeError(f"SGP4 propagation error {err}")

        # TEME -> ECEF (rotate by GMST about the pole; polar motion is negligible here)
        theta = _gmst_rad(jd + fr)
        c, s_ = math.cos(theta), math.sin(theta)
        x = c * r_teme[0] + s_ * r_teme[1]
        y = -s_ * r_teme[0] + c * r_teme[1]
        z = r_teme[2]

        rx, ry, rz = x - obs_ecef[0], y - obs_ecef[1], z - obs_ecef[2]
        lat = math.radians(lat_deg)
        lon = math.radians(lon_deg)
        east = -math.sin(lon) * rx + math.cos(lon) * ry
        north = (-math.sin(lat) * math.cos(lon) * rx - math.sin(lat) * math.sin(lon) * ry
                 + math.cos(lat) * rz)
        up = (math.cos(lat) * math.cos(lon) * rx + math.cos(lat) * math.sin(lon) * ry
              + math.sin(lat) * rz)
        rng = math.sqrt(rx * rx + ry * ry + rz * rz)
        elevation = math.degrees(math.atan2(up, math.hypot(east, north)))
        azimuth = (math.degrees(math.atan2(east, north)) + 360.0) % 360.0
        sub_lat = math.degrees(math.atan2(z, math.hypot(x, y)))
        return elevation, azimuth, rng, sub_lat

    # ------------------------------------------------------------ prediction

    def predict(self, satellite: str, latitude: float, longitude: float, altitude_m: float = 0.0,
                hours: float = 24.0, min_elevation: float = 20.0, step_s: float = 30.0,
                start: Optional[datetime] = None) -> Tuple[List[SatellitePass], Dict[str, Any]]:
        """Find passes with peak elevation >= min_elevation within the next `hours`."""
        if not SGP4_AVAILABLE:
            raise RuntimeError("The sgp4 package is not installed. Install with: pip install sgp4")
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise ValueError("Latitude/longitude out of range")
        if not (0 < hours <= 168):
            raise ValueError("hours must be between 0 and 168")

        tle = self.get_tle(satellite)
        sat = Satrec.twoline2rv(tle["line1"], tle["line2"])
        obs = _geodetic_to_ecef(latitude, longitude, altitude_m / 1000.0)
        start = (start or datetime.now(timezone.utc)).astimezone(timezone.utc)
        end = start + timedelta(hours=hours)

        def elev_at(t: datetime) -> Tuple[float, float, float]:
            el, az, _, sub_lat = self.look_angles(sat, t, obs, latitude, longitude)
            return el, az, sub_lat

        def find_crossing(t_below: datetime, t_above: datetime) -> datetime:
            """Bisect the horizon crossing between a below-horizon and an above-horizon time."""
            for _ in range(20):
                mid = t_below + (t_above - t_below) / 2
                if elev_at(mid)[0] > 0:
                    t_above = mid
                else:
                    t_below = mid
                if (t_above - t_below).total_seconds() < 0.5:
                    break
            return t_above

        passes: List[SatellitePass] = []
        step = timedelta(seconds=step_s)
        t = start
        prev_t, prev_el = None, None
        in_pass = False
        aos = None
        while t <= end:
            el, _, _ = elev_at(t)
            if not in_pass and el > 0:
                aos = find_crossing(prev_t, t) if prev_t is not None and prev_el is not None and prev_el <= 0 else t
                in_pass = True
            elif in_pass and el <= 0:
                los = find_crossing(t, prev_t)
                passes.append(self._summarize(satellite, aos, los, elev_at))
                in_pass = False
            prev_t, prev_el = t, el
            t += step
        if in_pass and aos is not None:
            passes.append(self._summarize(satellite, aos, min(prev_t, end), elev_at))

        selected = [p for p in passes if p.max_elevation_deg >= min_elevation]
        info = {
            "tle_name": tle["name"],
            "tle_source": tle["source"],
            "tle_age_hours": tle["age_hours"],
            "window_start_utc": start.isoformat(timespec="seconds"),
            "window_hours": hours,
            "all_passes": len(passes),
            "min_elevation": min_elevation,
        }
        return selected, info

    def _summarize(self, satellite: str, aos: datetime, los: datetime, elev_at) -> SatellitePass:
        """Scan a pass at 5 s resolution for its peak and azimuths."""
        best_el, best_t, best_az = -90.0, aos, 0.0
        t = aos
        fine = timedelta(seconds=5)
        while t <= los:
            el, az, _ = elev_at(t)
            if el > best_el:
                best_el, best_t, best_az = el, t, az
            t += fine
        _, aos_az, aos_lat = elev_at(aos + timedelta(seconds=1))
        _, los_az, los_lat = elev_at(los - timedelta(seconds=1))
        direction = "northbound" if los_lat > aos_lat else "southbound"
        return SatellitePass(
            satellite=satellite, aos=aos, los=los, max_elevation_time=best_t,
            max_elevation_deg=best_el, aos_azimuth_deg=aos_az, max_azimuth_deg=best_az,
            los_azimuth_deg=los_az, direction=direction,
        )
