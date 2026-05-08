"""
ADS-B aircraft state tracking and pyModeS decoding.
"""

import json
import logging
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

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
    last_seen: Optional[datetime] = None
    message_count: int = 0


class ADSBDecoder:
    """ADS-B protocol decoder and aircraft state tracker."""

    def __init__(self):
        self.aircraft: Dict[str, Aircraft] = {}
        self.registration_cache: Dict[str, Dict[str, Any]] = {}
        self.raw_message_count = 0
        self.message_count = 0

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

            elif isinstance(tc, int) and 9 <= tc <= 18:
                alt = decoded.get("altitude")
                if alt is not None:
                    aircraft.altitude = alt
                if decoded.get("latitude") is not None:
                    aircraft.latitude = decoded["latitude"]
                if decoded.get("longitude") is not None:
                    aircraft.longitude = decoded["longitude"]

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
            "climbing": sum(1 for ac in active if (ac.get("vertical_rate") or 0) > 200),
            "descending": sum(1 for ac in active if (ac.get("vertical_rate") or 0) < -200),
        }
