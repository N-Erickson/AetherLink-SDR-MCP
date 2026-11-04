"""
ADS-B aircraft state tracking and pyModeS decoding.
"""

import json
import logging
import math
import time
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from . import cpr

logger = logging.getLogger(__name__)

try:
    import pyModeS as pms
    ADSB_AVAILABLE = True
except ImportError:
    pms = None
    ADSB_AVAILABLE = False
    logger.warning("ADS-B decoder not available. Install with: pip install pyModeS")


@dataclass
class Aircraft:
    """Tracked aircraft data."""

    icao: str
    callsign: Optional[str] = None
    altitude: Optional[int] = None
    speed: Optional[float] = None
    heading: Optional[float] = None
    vertical_rate: Optional[float] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    registration: Optional[str] = None
    aircraft_type: Optional[str] = None
    operator: Optional[str] = None
    icao_type: Optional[str] = None
    position_time: Optional[datetime] = None
    distance_km: Optional[float] = None   # from the receiver, when its location is known
    bearing_deg: Optional[float] = None
    on_ground: Optional[bool] = None
    last_seen: Optional[datetime] = None
    message_count: int = 0


CPR_PAIR_MAX_AGE_S = 10.0  # even/odd frames must be this close to solve a global position


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def initial_bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlmb = math.radians(lon2 - lon1)
    x = math.sin(dlmb) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlmb)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


class ADSBDecoder:
    """ADS-B protocol decoder and aircraft state tracker."""

    def __init__(self):
        self.aircraft: Dict[str, Aircraft] = {}
        self.registration_cache: Dict[str, Dict[str, Any]] = {}
        self.raw_message_count = 0
        self.message_count = 0
        self.position_count = 0
        self.receiver_lat: Optional[float] = None
        self.receiver_lon: Optional[float] = None
        # Latest even/odd CPR frames per aircraft: icao -> {"even": (hex, t), "odd": (hex, t)}
        self._cpr: Dict[str, Dict[str, Any]] = {}

    def set_receiver_location(self, latitude: Optional[float], longitude: Optional[float]):
        """Receiver position enables single-frame (local CPR) and surface position decoding."""
        if latitude is None or longitude is None:
            self.receiver_lat = self.receiver_lon = None
            return
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise ValueError("Receiver latitude/longitude out of range")
        self.receiver_lat = float(latitude)
        self.receiver_lon = float(longitude)

    def _update_position(self, aircraft: Aircraft, msg_hex: str, tc: int):
        """Decode an airborne or surface position from CPR-encoded frames.

        Uses the in-repo CPR implementation so it behaves the same with pyModeS 2 and 3.
        """
        try:
            cpr_format, cpr_lat, cpr_lon = cpr.cpr_fields(msg_hex)
        except (ValueError, IndexError):
            return
        now = time.time()
        frames = self._cpr.setdefault(aircraft.icao, {})
        frames["odd" if cpr_format else "even"] = (cpr_lat, cpr_lon, now)

        position = None
        airborne = 9 <= tc <= 18 or 20 <= tc <= 22
        try:
            if airborne:
                even, odd = frames.get("even"), frames.get("odd")
                if even and odd and abs(even[2] - odd[2]) <= CPR_PAIR_MAX_AGE_S:
                    position = cpr.airborne_global(even[0], even[1], odd[0], odd[1],
                                                   even_is_newer=even[2] >= odd[2])
                if position is None and self.receiver_lat is not None:
                    position = cpr.airborne_local(cpr_lat, cpr_lon, cpr_format,
                                                  self.receiver_lat, self.receiver_lon)
            elif 5 <= tc <= 8 and self.receiver_lat is not None:
                position = cpr.surface_local(cpr_lat, cpr_lon, cpr_format,
                                             self.receiver_lat, self.receiver_lon)
        except Exception as e:
            logger.debug(f"CPR decode failed for {aircraft.icao}: {e}")
            position = None

        if not position:
            return
        lat, lon = position
        aircraft.latitude = lat
        aircraft.longitude = lon
        aircraft.position_time = datetime.now()
        aircraft.on_ground = not airborne
        self.position_count += 1
        if self.receiver_lat is not None:
            aircraft.distance_km = round(haversine_km(self.receiver_lat, self.receiver_lon, lat, lon), 1)
            aircraft.bearing_deg = round(initial_bearing_deg(self.receiver_lat, self.receiver_lon, lat, lon))

    def decode_message(self, msg_hex: str) -> Optional[Dict[str, Any]]:
        """Decode a single ADS-B message from a hex string."""
        self.raw_message_count += 1
        if not ADSB_AVAILABLE:
            return None

        try:
            # pyModeS expects hex strings, not bytes.
            msg_hex = msg_hex.strip().upper()
            if len(msg_hex) != 28:  # 14 bytes * 2 hex chars
                return None
            int(msg_hex, 16)

            decoded = self._decode_adsb_fields(msg_hex)
            if not decoded:
                return None

            # Only process ADS-B extended squitter frames with valid CRC.
            if decoded.get("df") not in (17, 18) or not decoded.get("crc_valid"):
                return None

            icao = decoded.get("icao")
            if not icao:
                return None
            icao = str(icao).upper()

            if icao not in self.aircraft:
                self.aircraft[icao] = Aircraft(icao=icao, last_seen=datetime.now())

            aircraft = self.aircraft[icao]
            aircraft.last_seen = datetime.now()
            aircraft.message_count += 1
            self.message_count += 1

            tc = decoded.get("typecode")

            if isinstance(tc, int) and 1 <= tc <= 4:
                callsign = decoded.get("callsign")
                if callsign:
                    aircraft.callsign = str(callsign).strip().replace("_", "")

            elif isinstance(tc, int) and 5 <= tc <= 8:
                # Surface position (needs the receiver location as CPR reference)
                self._update_position(aircraft, msg_hex, tc)

            elif isinstance(tc, int) and (9 <= tc <= 18 or 20 <= tc <= 22):
                alt = decoded.get("altitude")
                if alt is not None:
                    aircraft.altitude = alt
                self._update_position(aircraft, msg_hex, tc)

            elif tc == 19:
                speed = decoded.get("groundspeed")
                if speed is None:
                    speed = decoded.get("airspeed")
                heading = decoded.get("track")
                if heading is None:
                    heading = decoded.get("heading")
                vertical_rate = decoded.get("vertical_rate")
                if speed is not None:
                    aircraft.speed = speed
                if heading is not None:
                    aircraft.heading = heading
                if vertical_rate is not None:
                    aircraft.vertical_rate = vertical_rate

            return {
                "icao": icao,
                "aircraft": asdict(aircraft),
                "message_type": tc,
                "raw": msg_hex,
            }

        except Exception as e:
            logger.debug(f"Failed to decode ADS-B message: {e}")
            return None

    def _decode_adsb_fields(self, msg_hex: str) -> Optional[Dict[str, Any]]:
        """Decode Mode-S fields with pyModeS v3, falling back to the v2 API."""
        if pms is None:
            return None

        decode_fn = getattr(pms, "decode", None)
        if callable(decode_fn):
            try:
                decoded = decode_fn(msg_hex)
            except TypeError:
                decoded = None
            except Exception as e:
                logger.debug(f"pyModeS v3 decode failed: {e}")
                return None
            if isinstance(decoded, dict) and "df" in decoded:
                return dict(decoded)

        return self._decode_adsb_fields_v2(msg_hex)

    def _decode_adsb_fields_v2(self, msg_hex: str) -> Optional[Dict[str, Any]]:
        """Decode ADS-B fields with the pyModeS 2.x function-per-field API."""
        if pms is None:
            return None

        try:
            df = pms.df(msg_hex)
            if df not in (17, 18) or pms.crc(msg_hex) != 0:
                return None

            adsb = getattr(pms, "adsb")
            icao_fn = getattr(adsb, "icao", None)
            icao = icao_fn(msg_hex) if callable(icao_fn) else None
            if not icao and hasattr(pms, "icao"):
                icao = pms.icao(msg_hex)

            tc = adsb.typecode(msg_hex)
            decoded: Dict[str, Any] = {
                "df": df,
                "icao": icao,
                "crc_valid": True,
                "typecode": tc,
            }

            if 1 <= tc <= 4:
                decoded["callsign"] = adsb.callsign(msg_hex)
            elif 9 <= tc <= 18:
                decoded["altitude"] = adsb.altitude(msg_hex)
            elif tc == 19:
                velocity = adsb.velocity(msg_hex)
                if velocity:
                    decoded["groundspeed"] = velocity[0] if len(velocity) > 0 else None
                    decoded["track"] = velocity[1] if len(velocity) > 1 else None
                    decoded["vertical_rate"] = velocity[2] if len(velocity) > 2 else None

            return decoded
        except Exception as e:
            logger.debug(f"pyModeS v2 decode failed: {e}")
            return None

    def get_aircraft_list(
        self,
        max_age_seconds: int = 120,
        include_inactive: bool = False,
    ) -> List[Dict[str, Any]]:
        """Get a list of tracked aircraft."""
        now = datetime.now()
        active_aircraft = []

        for icao, aircraft in self.aircraft.items():
            time_diff = (now - aircraft.last_seen).total_seconds() if aircraft.last_seen else None
            if include_inactive or (time_diff is not None and time_diff < max_age_seconds):
                data = asdict(aircraft)
                data["age_seconds"] = time_diff
                data["tracking_url"] = self.get_tracking_url(icao)
                active_aircraft.append(data)

        if self.receiver_lat is not None:
            # Nearest first when we know where we are; unknown positions last
            active_aircraft.sort(key=lambda ac: (ac.get("distance_km") is None, ac.get("distance_km") or 0))
        else:
            active_aircraft.sort(key=lambda ac: ac.get("message_count", 0), reverse=True)
        return active_aircraft

    def get_tracking_url(self, icao: str) -> str:
        """Build a public ADS-B tracking URL for an ICAO address."""
        return f"https://globe.theairtraffic.com/?icao={icao.lower()}"

    def lookup_aircraft(self, icao: str) -> Dict[str, Any]:
        """Look up registration/type/operator metadata from hexdb.io."""
        icao = icao.upper()
        if icao in self.registration_cache:
            return self.registration_cache[icao]

        try:
            url = f"https://hexdb.io/api/v1/aircraft/{icao}"
            req = urllib.request.Request(url, headers={"User-Agent": "AetherLink-SDR/1.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                info = json.loads(resp.read())
        except Exception as e:
            logger.debug(f"Aircraft lookup failed for {icao}: {e}")
            info = {}

        self.registration_cache[icao] = info

        aircraft = self.aircraft.get(icao)
        if aircraft and info:
            aircraft.registration = info.get("Registration") or aircraft.registration
            aircraft.aircraft_type = info.get("Type") or aircraft.aircraft_type
            aircraft.operator = info.get("RegisteredOwners") or aircraft.operator
            aircraft.icao_type = info.get("ICAOTypeCode") or aircraft.icao_type

        return info

    def get_statistics(self, max_age_seconds: int = 120) -> Dict[str, Any]:
        """Return summary counters for tracked aircraft."""
        active = self.get_aircraft_list(max_age_seconds=max_age_seconds)
        return {
            "raw_messages": self.raw_message_count,
            "decoded_messages": self.message_count,
            "total_aircraft_seen": len(self.aircraft),
            "active_aircraft": len(active),
            "identified_callsigns": sum(1 for ac in active if ac.get("callsign")),
            "with_altitude": sum(1 for ac in active if ac.get("altitude")),
            "with_position": sum(1 for ac in active if ac.get("latitude") is not None),
            "positions_decoded": self.position_count,
            "receiver_location": (
                [self.receiver_lat, self.receiver_lon] if self.receiver_lat is not None else None
            ),
            "climbing": sum(1 for ac in active if (ac.get("vertical_rate") or 0) > 200),
            "descending": sum(1 for ac in active if (ac.get("vertical_rate") or 0) < -200),
        }
