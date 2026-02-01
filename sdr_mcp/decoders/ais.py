"""
AIS (Automatic Identification System) Ship Tracking Decoder
Decodes AIS messages on 161.975 MHz and 162.025 MHz
"""

import numpy as np
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

# AIS constants
AIS_CHANNEL_A = 161.975e6
AIS_CHANNEL_B = 162.025e6

@dataclass
class AISVessel:
    """Tracked vessel from AIS"""
    mmsi: str
    name: Optional[str] = None
    callsign: Optional[str] = None
    imo: Optional[int] = None
    ship_type: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    speed: Optional[float] = None  # knots
    course: Optional[float] = None  # degrees
    heading: Optional[float] = None  # degrees
    destination: Optional[str] = None
    eta: Optional[str] = None
    last_seen: datetime = None
    message_count: int = 0

class AISDecoder:
    """AIS protocol decoder"""

    # AIS ship types
    SHIP_TYPES = {
        30: "Fishing",
        31: "Towing",
        32: "Towing (large)",
        33: "Dredging",
        34: "Diving",
        35: "Military",
        36: "Sailing",
        37: "Pleasure craft",
        40: "High speed craft",
        41: "High speed craft (hazardous)",
        42: "High speed craft (medical)",
        50: "Pilot vessel",
        51: "SAR",
        52: "Tug",
        53: "Port tender",
        54: "Anti-pollution",
        55: "Law enforcement",
        60: "Passenger",
        70: "Cargo",
        80: "Tanker",
        90: "Other"
    }

    def __init__(self):
        self.vessels: Dict[str, AISVessel] = {}
        self.message_count = 0

    def decode_sixbit(self, data: str) -> str:
        """Decode AIS 6-bit ASCII payload"""
        result = []
        for char in data:
            val = ord(char) - 48
            if val > 40:
                val -= 8
            result.append(chr(val + 32) if 32 <= val + 32 <= 95 else '?')
        return ''.join(result).strip('@')

    def decode_position_report(self, mmsi: str, payload: bytes) -> Optional[Dict[str, Any]]:
        """Decode AIS position report (message types 1, 2, 3)"""
        # Extract fields from payload
        # This is a simplified decoder - real AIS needs proper bit extraction

        # Latitude/Longitude (28 bits each, in 1/10000 minute)
        lat_raw = int.from_bytes(payload[8:12], 'big') & 0x7FFFFFF
        lon_raw = int.from_bytes(payload[12:16], 'big') & 0xFFFFFFF

        latitude = (lat_raw / 600000.0) if lat_raw != 0x3412140 else None
        longitude = (lon_raw / 600000.0) if lon_raw != 0x6791AC0 else None

        # Speed over ground (10 bits, in 1/10 knot)
        speed_raw = (int.from_bytes(payload[6:8], 'big') >> 6) & 0x3FF
        speed = speed_raw / 10.0 if speed_raw != 1023 else None

        # Course over ground (12 bits, in 1/10 degree)
        course_raw = int.from_bytes(payload[7:9], 'big') & 0xFFF
        course = course_raw / 10.0 if course_raw != 3600 else None

        # True heading (9 bits)
        heading_raw = (int.from_bytes(payload[9:11], 'big') >> 7) & 0x1FF
        heading = float(heading_raw) if heading_raw != 511 else None

        return {
            'latitude': latitude,
            'longitude': longitude,
            'speed': speed,
            'course': course,
            'heading': heading
        }

    def decode_static_data(self, mmsi: str, payload: bytes) -> Optional[Dict[str, Any]]:
        """Decode AIS static and voyage data (message type 5)"""
        # Extract vessel name (20 chars, 6-bit ASCII)
        name_data = payload[20:140].decode('ascii', errors='ignore')
        name = self.decode_sixbit(name_data) if name_data else None

        # Callsign (7 chars)
        callsign_data = payload[70:112].decode('ascii', errors='ignore')
        callsign = self.decode_sixbit(callsign_data) if callsign_data else None

        # Ship type
        ship_type_code = payload[232:240]
        ship_type = self.SHIP_TYPES.get(ship_type_code, f"Type {ship_type_code}")

        # Destination (20 chars)
        dest_data = payload[302:422].decode('ascii', errors='ignore')
        destination = self.decode_sixbit(dest_data) if dest_data else None

        return {
            'name': name,
            'callsign': callsign,
            'ship_type': ship_type,
            'destination': destination
        }

    def decode_message(self, payload: bytes) -> Optional[Dict[str, Any]]:
        """Decode AIS message"""
        if len(payload) < 2:
            return None

        # Message type (6 bits)
        msg_type = (payload[0] >> 2) & 0x3F

        # MMSI (30 bits)
        mmsi_raw = ((payload[1] & 0x3) << 28) | (payload[2] << 20) | (payload[3] << 12) | (payload[4] << 4) | ((payload[5] >> 4) & 0xF)
        mmsi = str(mmsi_raw)

        # Update or create vessel entry
        if mmsi not in self.vessels:
            self.vessels[mmsi] = AISVessel(mmsi=mmsi, last_seen=datetime.now())

        vessel = self.vessels[mmsi]
        vessel.last_seen = datetime.now()
        vessel.message_count += 1
        self.message_count += 1

        # Decode based on message type
        if msg_type in [1, 2, 3]:  # Position reports
            pos_data = self.decode_position_report(mmsi, payload)
            if pos_data:
                vessel.latitude = pos_data.get('latitude')
                vessel.longitude = pos_data.get('longitude')
                vessel.speed = pos_data.get('speed')
                vessel.course = pos_data.get('course')
                vessel.heading = pos_data.get('heading')

        elif msg_type == 5:  # Static and voyage data
            static_data = self.decode_static_data(mmsi, payload)
            if static_data:
                vessel.name = static_data.get('name')
                vessel.callsign = static_data.get('callsign')
                vessel.ship_type = static_data.get('ship_type')
                vessel.destination = static_data.get('destination')

        return {
            'mmsi': mmsi,
            'message_type': msg_type,
            'vessel': asdict(vessel)
        }

    def get_vessel_list(self) -> List[Dict[str, Any]]:
        """Get list of tracked vessels"""
        now = datetime.now()
        active_vessels = []

        for mmsi, vessel in self.vessels.items():
            # Only include vessels seen in last 10 minutes
            time_diff = (now - vessel.last_seen).total_seconds()
            if time_diff < 600:
                active_vessels.append(asdict(vessel))

        return active_vessels

    def get_statistics(self) -> Dict[str, Any]:
        """Get decoder statistics"""
        return {
            'total_messages': self.message_count,
            'total_vessels': len(self.vessels),
            'active_vessels': len(self.get_vessel_list())
        }
