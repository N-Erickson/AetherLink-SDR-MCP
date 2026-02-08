"""
Meteor-M LRPT Decoder
Decodes LRPT (Low Rate Picture Transmission) from Russian Meteor-M weather satellites
Uses SatDump for OQPSK demodulation and image decoding
"""

import logging
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger(__name__)

# Meteor-M satellite frequencies
METEOR_SATELLITES = {
    "METEOR-M2":    {"frequency": 137.1e6, "name": "Meteor-M N2",   "status": "inactive"},
    "METEOR-M2-2":  {"frequency": 137.1e6, "name": "Meteor-M N2-2", "status": "degraded"},
    "METEOR-M2-3":  {"frequency": 137.9e6, "name": "Meteor-M N2-3", "status": "active"},
    "METEOR-M2-4":  {"frequency": 137.9e6, "name": "Meteor-M N2-4", "status": "active", "backup": 137.1e6},
}

# Common LRPT frequencies
LRPT_FREQUENCIES = {
    "137.1MHz": 137.1e6,
    "137.9MHz": 137.9e6,
}

@dataclass
class MeteorPass:
    """Meteor satellite pass data"""
    satellite: str
    frequency: float
    start_time: datetime
    duration: int  # seconds
    output_dir: str
    decoded_images: List[str]
    success: bool
    quality: Optional[float] = None
    channels_received: Optional[List[str]] = None
    error_message: Optional[str] = None


class MeteorLRPTDecoder:
    """Meteor-M LRPT decoder using SatDump subprocess"""

    def __init__(self):
        self.passes: List[MeteorPass] = []
        self.current_satellite = "METEOR-M2-4"  # Default to newest satellite
        self.sample_rate = 1.0e6  # 1 MSPS for LRPT

    def get_satellite_info(self, satellite: str) -> Optional[Dict[str, Any]]:
        """Get information about a Meteor satellite"""
        return METEOR_SATELLITES.get(satellite)

    def get_active_satellites(self) -> List[str]:
        """Get list of currently active Meteor satellites"""
        return [
            sat for sat, info in METEOR_SATELLITES.items()
            if info["status"] == "active"
        ]

    def get_frequency(self, satellite: str) -> float:
        """Get primary frequency for a satellite"""
        info = self.get_satellite_info(satellite)
        if info:
            return info["frequency"]
        return 137.9e6  # Default to M2-4 frequency

    def get_satdump_pipeline(self, satellite: str) -> str:
        """Get the appropriate SatDump pipeline name for a satellite

        SatDump uses pipeline names like:
        - meteor_m2_lrpt (for M2 - old QPSK 72k)
        - meteor_m2-x_lrpt (for M2-2, M2-3, M2-4 with OQPSK 72k)

        Note: Actual pipeline ID is "meteor_m2-x_lrpt" (hyphen, not underscore)
        """
        if satellite == "METEOR-M2":
            return "meteor_m2_lrpt"
        else:
            # M2-2, M2-3, M2-4 all use OQPSK
            # Note the hyphen in "m2-x"
            return "meteor_m2-x_lrpt"

    def build_satdump_command(
        self,
        satellite: str,
        frequency: float,
        output_dir: str,
        duration: int,
        gain: float = 40.0,
        bias_tee: bool = False
    ) -> List[str]:
        """Build SatDump command for live LRPT reception

        Args:
            satellite: Satellite name (e.g., "METEOR-M2-4")
            frequency: Center frequency in Hz
            output_dir: Output directory for decoded images
            duration: Recording duration in seconds
            gain: RTL-SDR gain in dB
            bias_tee: Enable bias tee for LNA power

        Returns:
            Command as list of strings
        """
        pipeline = self.get_satdump_pipeline(satellite)

        cmd = [
            "satdump",
            "live",
            pipeline,
            output_dir,
            "--source", "rtlsdr",
            "--samplerate", str(int(self.sample_rate)),
            "--frequency", str(int(frequency)),
            "--gain", str(int(gain)),
            "--timeout", str(duration),
        ]

        if bias_tee:
            cmd.append("--bias")

        return cmd

    def parse_satdump_output(self, output_dir: str) -> Dict[str, Any]:
        """Parse SatDump output directory for decoded images and metadata

        SatDump creates:
        - decoded_*.png (individual channel images)
        - products/*.png (composite images)
        - dataset.json (metadata)
        """
        import os
        import json
        import glob

        result = {
            "success": False,
            "images": [],
            "channels": [],
            "metadata": {}
        }

        # Check for dataset.json
        dataset_path = os.path.join(output_dir, "dataset.json")
        if os.path.exists(dataset_path):
            try:
                with open(dataset_path, 'r') as f:
                    result["metadata"] = json.load(f)
                result["success"] = True
            except Exception as e:
                logger.warning(f"Failed to parse dataset.json: {e}")

        # Find all PNG images
        decoded_images = glob.glob(os.path.join(output_dir, "*.png"))
        product_images = glob.glob(os.path.join(output_dir, "products", "*.png"))

        result["images"] = decoded_images + product_images

        # Parse channel names from filenames
        for img in decoded_images:
            basename = os.path.basename(img)
            if "channel" in basename.lower():
                result["channels"].append(basename)

        return result

    def add_pass(self, meteor_pass: MeteorPass):
        """Add a decoded pass to history"""
        self.passes.append(meteor_pass)
        if len(self.passes) > 50:  # Keep last 50 passes
            self.passes = self.passes[-50:]

    def get_statistics(self) -> Dict[str, Any]:
        """Get decoder statistics"""
        successful_passes = sum(1 for p in self.passes if p.success)

        return {
            "total_passes": len(self.passes),
            "successful_passes": successful_passes,
            "failed_passes": len(self.passes) - successful_passes,
            "satellites_decoded": list(set(p.satellite for p in self.passes)),
            "current_satellite": self.current_satellite,
            "active_satellites": self.get_active_satellites()
        }
