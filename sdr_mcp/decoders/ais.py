"""
AIS (Automatic Identification System) vessel tracking decoder.

Live reception is done by an external receiver (AIS-catcher or rtl_ais) that
emits NMEA 0183 ``!AIVDM`` sentences on 161.975 / 162.025 MHz. This module
assembles multi-part sentences, decodes them with pyais and keeps a vessel table.
"""

import logging
import re
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    from pyais import decode as pyais_decode
    AIS_AVAILABLE = True
except ImportError:
    pyais_decode = None
    AIS_AVAILABLE = False
    logger.warning("AIS decoder not available. Install with: pip install pyais")

# AIS constants
AIS_CHANNEL_A = 161.975e6
AIS_CHANNEL_B = 162.025e6

# Matches a VDM/VDO sentence embedded anywhere in a line (receivers add prefixes/suffixes)
NMEA_RE = re.compile(r"(![A-Z]{2}VD[MO],[^\s]*)")

# Sentinel values meaning "not available" in AIS messages
LAT_UNAVAILABLE = 91.0
LON_UNAVAILABLE = 181.0
SPEED_UNAVAILABLE = 102.3
COURSE_UNAVAILABLE = 360.0
HEADING_UNAVAILABLE = 511


@dataclass
class AISVessel:
    """Tracked vessel from AIS"""
    mmsi: str
    name: Optional[str] = None
    callsign: Optional[str] = None
    imo: Optional[int] = None
    ship_type: Optional[str] = None
    nav_status: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    speed: Optional[float] = None    # knots
    course: Optional[float] = None   # degrees
    heading: Optional[float] = None  # degrees
    destination: Optional[str] = None
    last_seen: Optional[datetime] = None
    message_count: int = 0

    @property
    def tracking_url(self) -> str:
        return f"https://www.marinetraffic.com/en/ais/details/ships/mmsi:{self.mmsi}"

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["last_seen"] = self.last_seen.isoformat() if self.last_seen else None
        data["tracking_url"] = self.tracking_url
        return data


def _enum_name(value: Any) -> Optional[str]:
    """pyais returns enums for ship type / nav status; store a readable string."""
    if value is None:
        return None
    name = getattr(value, "name", None)
    if name:
        # UnderWayUsingEngine -> Under Way Using Engine
        return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
    return str(value)


def _clean_text(value: Any) -> Optional[str]:
    if not value:
        return None
    text = str(value).replace("@", " ").strip()
    return text or None


class AISDecoder:
    """AIS NMEA assembler, pyais decoder, and vessel tracker"""

    def __init__(self, max_age_seconds: int = 600):
        self.vessels: Dict[str, AISVessel] = {}
        self.max_age_seconds = max_age_seconds
        self.message_count = 0     # successfully decoded AIS messages
        self.nmea_count = 0        # NMEA sentences seen
        self.decode_errors = 0
        self.recent_nmea: Deque[str] = deque(maxlen=200)
        self._pending: Dict[str, Dict[int, str]] = {}

    # ------------------------------------------------------------ ingest

    def feed_line(self, line: str) -> Optional[Dict[str, Any]]:
        """Feed one line of receiver output. Returns decoded message info when complete."""
        match = NMEA_RE.search(line)
        if not match:
            return None
        sentence = match.group(1)
        self.nmea_count += 1
        self.recent_nmea.append(sentence)

        fields = sentence.split("*")[0].split(",")
        if len(fields) < 6:
            return None
        try:
            total = int(fields[1])
            index = int(fields[2])
        except ValueError:
            return None

        if total <= 1:
            return self._decode([sentence])

        # Multi-sentence message: group by channel + sequential id
        key = f"{fields[4]}:{fields[3]}"
        parts = self._pending.setdefault(key, {})
        parts[index] = sentence
        if len(parts) < total:
            if len(self._pending) > 64:  # drop stale partial messages
                self._pending.pop(next(iter(self._pending)))
            return None
        del self._pending[key]
        if sorted(parts) != list(range(1, total + 1)):
            return None
        return self._decode([parts[i] for i in range(1, total + 1)])

    def _decode(self, sentences: List[str]) -> Optional[Dict[str, Any]]:
        if not AIS_AVAILABLE:
            return None
        try:
            message = pyais_decode(*[s.encode() for s in sentences])
            data = message.asdict()
        except Exception as e:
            self.decode_errors += 1
            logger.debug(f"AIS decode failed: {e} ({sentences[0][:40]}...)")
            return None
        return self._apply(data)

    def _apply(self, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        mmsi = data.get("mmsi")
        if mmsi is None:
            return None
        mmsi = str(mmsi)

        vessel = self.vessels.get(mmsi)
        if vessel is None:
            vessel = AISVessel(mmsi=mmsi)
            self.vessels[mmsi] = vessel
        vessel.last_seen = datetime.now()
        vessel.message_count += 1
        self.message_count += 1

        lat = data.get("lat")
        lon = data.get("lon")
        if lat is not None and lon is not None and abs(lat) <= 90 and abs(lon) <= 180 \
                and lat != LAT_UNAVAILABLE and lon != LON_UNAVAILABLE:
            vessel.latitude = float(lat)
            vessel.longitude = float(lon)

        speed = data.get("speed")
        if speed is not None and speed < SPEED_UNAVAILABLE:
            vessel.speed = float(speed)
        course = data.get("course")
        if course is not None and course < COURSE_UNAVAILABLE:
            vessel.course = float(course)
        heading = data.get("heading")
        if heading is not None and heading != HEADING_UNAVAILABLE:
            vessel.heading = float(heading)

        name = _clean_text(data.get("shipname") or data.get("name"))
        if name:
            vessel.name = name
        callsign = _clean_text(data.get("callsign"))
        if callsign:
            vessel.callsign = callsign
        destination = _clean_text(data.get("destination"))
        if destination:
            vessel.destination = destination
        ship_type = _enum_name(data.get("ship_type"))
        if ship_type:
            vessel.ship_type = ship_type
        nav_status = _enum_name(data.get("status"))
        if nav_status:
            vessel.nav_status = nav_status
        imo = data.get("imo")
        if imo:
            vessel.imo = int(imo)

        return {
            "mmsi": mmsi,
            "message_type": data.get("msg_type"),
            "vessel": vessel.to_dict(),
        }

    # ------------------------------------------------------------ queries

    def get_vessel_list(self, max_age_seconds: Optional[int] = None) -> List[Dict[str, Any]]:
        """Vessels seen within max_age_seconds, most recent first"""
        max_age = max_age_seconds or self.max_age_seconds
        now = datetime.now()
        active = []
        for vessel in self.vessels.values():
            if vessel.last_seen is None:
                continue
            age = (now - vessel.last_seen).total_seconds()
            if age < max_age:
                data = vessel.to_dict()
                data["age_seconds"] = int(age)
                active.append(data)
        active.sort(key=lambda v: v["age_seconds"])
        return active

    def get_statistics(self, max_age_seconds: Optional[int] = None) -> Dict[str, Any]:
        """Get decoder statistics"""
        active = self.get_vessel_list(max_age_seconds)
        return {
            'total_messages': self.message_count,
            'nmea_sentences': self.nmea_count,
            'decode_errors': self.decode_errors,
            'total_vessels': len(self.vessels),
            'active_vessels': len(active),
            'with_position': sum(1 for v in active if v.get("latitude") is not None),
            'with_name': sum(1 for v in active if v.get("name")),
        }

    def clear(self):
        self.vessels.clear()
        self._pending.clear()
