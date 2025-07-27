"""
Validation utilities for AetherLink
"""

def validate_frequency(freq: float, min_freq: float, max_freq: float) -> bool:
    """Validate frequency is within range"""
    return min_freq <= freq <= max_freq

def validate_sample_rate(rate: float, min_rate: float, max_rate: float) -> bool:
    """Validate sample rate is within range"""
    return min_rate <= rate <= max_rate

def is_restricted_frequency(freq: float) -> bool:
    """Check if frequency is in a restricted band"""
    restricted_bands = [
        (108e6, 137e6),     # Aviation
        (406e6, 406.1e6),   # Emergency beacons
        (1.215e9, 1.39e9),  # GPS/GNSS
    ]
    
    for low, high in restricted_bands:
        if low <= freq <= high:
            return True
    return False
