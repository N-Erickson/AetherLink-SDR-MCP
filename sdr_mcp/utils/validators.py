"""
Validation utilities for AetherLink
"""

import logging
import os
import re
import shutil
import sys

logger = logging.getLogger(__name__)


def validate_frequency(freq: float, min_freq: float, max_freq: float) -> bool:
    """Validate frequency is within range"""
    return min_freq <= freq <= max_freq


def validate_sample_rate(rate: float, min_rate: float, max_rate: float) -> bool:
    """Validate sample rate is within range"""
    return min_rate <= rate <= max_rate


# Comprehensive restricted TX bands
RESTRICTED_TX_BANDS = [
    (108e6, 137e6),       # Aviation (VOR, ILS, ATC)
    (150e6, 174e6),       # Public safety / government (partial)
    (156.7e6, 156.9e6),   # VHF Marine distress (Channel 16)
    (225e6, 400e6),       # Military aviation (UHF)
    (406e6, 406.1e6),     # Emergency beacons (EPIRB/PLB)
    (420e6, 433e6),       # Government / military radiolocation (below ISM)
    (435e6, 450e6),       # Government / military radiolocation (above ISM)
    (698e6, 894e6),       # Cellular (700/800 MHz bands)
    (960e6, 1215e6),      # Aeronautical radionavigation
    (1164e6, 1610e6),     # GPS/GNSS (L1, L2, L5) + Galileo + GLONASS
    (1675e6, 1695e6),     # GOES weather satellite downlink
    (1710e6, 2155e6),     # Cellular (AWS, PCS bands)
    (2310e6, 2390e6),     # WCS / satellite radio
]


def is_restricted_frequency(freq: float) -> bool:
    """Check if frequency is in a restricted TX band"""
    for low, high in RESTRICTED_TX_BANDS:
        if low <= freq <= high:
            return True
    return False


def sanitize_path_component(name: str) -> str:
    """Validate and sanitize a string for safe use in file paths.

    Raises ValueError if the name contains path traversal characters.
    Returns the sanitized name.
    """
    if not name or not isinstance(name, str):
        raise ValueError("Path component must be a non-empty string")

    # Reject path traversal and dangerous characters
    if "\0" in name:
        raise ValueError("Path component must not contain null bytes")
    if "/" in name or "\\" in name:
        raise ValueError(f"Path component must not contain slashes: {name!r}")
    if ".." in name:
        raise ValueError(f"Path component must not contain '..': {name!r}")

    # Only allow alphanumeric, hyphens, underscores
    if not re.match(r'^[A-Za-z0-9_-]+$', name):
        raise ValueError(f"Path component contains invalid characters: {name!r}")

    # Limit length
    if len(name) > 64:
        raise ValueError(f"Path component too long ({len(name)} > 64)")

    return name


def find_binary(name: str, install_hint: str = "") -> str:
    """Find an external binary on PATH or common locations.

    Returns the full path to the binary.
    Raises FileNotFoundError with install instructions if not found.
    """
    # Try PATH first
    path = shutil.which(name)
    if path:
        return path

    # Try common platform-specific locations
    common_paths = [
        f"/opt/homebrew/bin/{name}",    # macOS ARM (Homebrew)
        f"/usr/local/bin/{name}",       # macOS Intel / Linux manual installs
        f"/usr/bin/{name}",             # Linux system packages
    ]

    for p in common_paths:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p

    hint = f" Install with: {install_hint}" if install_hint else ""
    raise FileNotFoundError(f"{name} not found on PATH or common locations.{hint}")
