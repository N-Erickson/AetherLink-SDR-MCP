"""
Compact Position Reporting (CPR) decoding for ADS-B (DO-260B / ICAO Annex 10).

Pure Python so it works with every pyModeS release (v2 exposed CPR helpers,
v3 removed them). Fields are read directly from the 112-bit frame.
"""

import math
from typing import Optional, Tuple

CPR_SCALE = 131072.0  # 2^17


def cpr_fields(msg_hex: str) -> Tuple[int, int, int]:
    """Return (cpr_format, cpr_lat, cpr_lon) from an airborne/surface position frame.

    Frame bit numbering (1-based): F flag is bit 54, latitude bits 55-71,
    longitude bits 72-88. Works for typecodes 5-8 (surface) and 9-18, 20-22 (airborne).
    """
    bits = bin(int(msg_hex, 16))[2:].zfill(len(msg_hex) * 4)
    return int(bits[53]), int(bits[54:71], 2), int(bits[71:88], 2)


def nl(lat: float) -> int:
    """Number of longitude zones at a latitude."""
    if lat == 0:
        return 59
    a = abs(lat)
    if a == 87:
        return 2
    if a > 87:
        return 1
    try:
        return int(math.floor(2 * math.pi / math.acos(
            1 - (1 - math.cos(math.pi / 30.0)) / math.cos(math.radians(lat)) ** 2
        )))
    except ValueError:
        return 1


def airborne_global(cpr_lat_even: int, cpr_lon_even: int, cpr_lat_odd: int, cpr_lon_odd: int,
                    even_is_newer: bool) -> Optional[Tuple[float, float]]:
    """Decode a global airborne position from an even and an odd frame."""
    lat_e = cpr_lat_even / CPR_SCALE
    lon_e = cpr_lon_even / CPR_SCALE
    lat_o = cpr_lat_odd / CPR_SCALE
    lon_o = cpr_lon_odd / CPR_SCALE

    dlat_e = 360.0 / 60
    dlat_o = 360.0 / 59
    j = math.floor(59 * lat_e - 60 * lat_o + 0.5)
    lat_even = dlat_e * ((j % 60) + lat_e)
    lat_odd = dlat_o * ((j % 59) + lat_o)
    if lat_even >= 270:
        lat_even -= 360
    if lat_odd >= 270:
        lat_odd -= 360

    if nl(lat_even) != nl(lat_odd):
        return None  # frames straddle a zone boundary; wait for the next pair

    if even_is_newer:
        lat = lat_even
        zones = nl(lat)
        ni = max(zones, 1)
        m = math.floor(lon_e * (zones - 1) - lon_o * zones + 0.5)
        lon = (360.0 / ni) * ((m % ni) + lon_e)
    else:
        lat = lat_odd
        zones = nl(lat)
        ni = max(zones - 1, 1)
        m = math.floor(lon_e * (zones - 1) - lon_o * zones + 0.5)
        lon = (360.0 / ni) * ((m % ni) + lon_o)

    if lon > 180:
        lon -= 360
    if not (-90 <= lat <= 90):
        return None
    return round(lat, 5), round(lon, 5)


def _local(cpr_lat: int, cpr_lon: int, cpr_format: int, lat_ref: float, lon_ref: float,
           lat_span: float, lon_span: float) -> Tuple[float, float]:
    lat_cpr = cpr_lat / CPR_SCALE
    lon_cpr = cpr_lon / CPR_SCALE
    i = cpr_format
    dlat = lat_span / (60 - i)
    j = math.floor(lat_ref / dlat) + math.floor(0.5 + (lat_ref % dlat) / dlat - lat_cpr)
    lat = dlat * (j + lat_cpr)
    zones = nl(lat)
    ni = max(zones - i, 1)
    dlon = lon_span / ni
    m = math.floor(lon_ref / dlon) + math.floor(0.5 + (lon_ref % dlon) / dlon - lon_cpr)
    lon = dlon * (m + lon_cpr)
    return lat, lon


def airborne_local(cpr_lat: int, cpr_lon: int, cpr_format: int,
                   lat_ref: float, lon_ref: float) -> Optional[Tuple[float, float]]:
    """Decode an airborne position from one frame using a reference within ~180 NM."""
    lat, lon = _local(cpr_lat, cpr_lon, cpr_format, lat_ref, lon_ref, 360.0, 360.0)
    if lon > 180:
        lon -= 360
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return round(lat, 5), round(lon, 5)


def surface_local(cpr_lat: int, cpr_lon: int, cpr_format: int,
                  lat_ref: float, lon_ref: float) -> Optional[Tuple[float, float]]:
    """Decode a surface position from one frame using a reference within ~45 NM.

    Surface CPR spans 90 degrees, so the nearest of the four ambiguous solutions
    to the reference is chosen.
    """
    lat, lon = _local(cpr_lat, cpr_lon, cpr_format, lat_ref, lon_ref, 90.0, 90.0)
    lat = min((lat + k * 90.0 for k in (-1, 0, 1)), key=lambda c: abs(c - lat_ref))
    lon = min((lon + k * 90.0 for k in (-2, -1, 0, 1, 2)), key=lambda c: abs(c - lon_ref))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return round(lat, 5), round(lon, 5)
