#!/usr/bin/env python3
"""
SDR MCP Server - Direct control for RTL-SDR and HackRF with protocol decoding
"""

import asyncio
import gc
import json
import logging
import os
import shutil
import traceback
from collections import deque
from datetime import datetime
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

import numpy as np

# MCP imports
from mcp.server import Server
from mcp.types import Tool, TextContent, Resource
import mcp.server.stdio

from . import __version__
from .hardware.base import SDRDevice
from .hardware.rtlsdr import RTLSDRDevice, RTLSDR_AVAILABLE, RTLSDR_IMPORT_ERROR, RTLSDR_INSTALL_HINT
from .hardware.hackrf import HackRFDevice, HACKRF_AVAILABLE, HACKRF_IMPORT_ERROR, HACKRF_INSTALL_HINT
from .analysis.spectrum import SpectrumAnalyzer, SignalRecorder, FrequencyScanner, AudioRecorder
from .utils.validators import sanitize_path_component, find_binary
from .utils.paths import recordings_dir
from .decoders.adsb import ADSBDecoder, ADSB_AVAILABLE
from .decoders.pocsag import POCSAGDecoder, POCSAG_BAUD_RATES
from .decoders.ais import AISDecoder, AIS_AVAILABLE
from .decoders.rtl433 import RTL433Decoder
from .decoders.meteor_lrpt import MeteorLRPTDecoder, MeteorPass
from .decoders.satellite_passes import PassPredictor, NORAD_IDS, SGP4_AVAILABLE

# Logging goes to stderr; stdout is the MCP transport and must stay clean.
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Constants
DEFAULT_SAMPLE_RATE = 2.048e6
DEFAULT_GAIN = 'auto'
ADSB_FREQUENCY = 1090e6
ADSB_RAW_PORT = 30002
RTLSDR_MIN_FREQ = 24e6
RTLSDR_MAX_FREQ = 1.766e9
MAX_METEOR_DURATION = 1800
MAX_TX_DURATION = 60.0
STARTUP_WAIT = 2.0  # seconds to wait for an external decoder to fail fast

# Valid FFT window functions
VALID_WINDOWS = {"hamming", "hann", "blackman", "blackman-harris", "flattop"}

# Install hints for external tools
DUMP1090_HINT = "brew install dump1090-fa (macOS) or sudo apt install dump1090-fa (Debian/Ubuntu)"
RTL_FM_HINT = "brew install librtlsdr (macOS) or sudo apt install rtl-sdr (Debian/Ubuntu)"
MULTIMON_HINT = (
    "sudo apt install multimon-ng (Debian/Ubuntu) or build from "
    "https://github.com/EliasOenal/multimon-ng (install.sh does this for you)"
)
AIS_HINT = (
    "install AIS-catcher (https://github.com/jvde-github/AIS-catcher) or rtl_ais "
    "(sudo apt install rtl-ais, or build https://github.com/dgiardini/rtl-ais)"
)
RTL433_HINT = "brew install rtl_433 (macOS) or sudo apt install rtl-433 (Debian/Ubuntu)"
SATDUMP_HINT = "brew install satdump (macOS) or see https://github.com/SatDump/SatDump"

# External processes that take exclusive control of the radio
HOLDER_LABELS = {
    "adsb": "dump1090 (ADS-B aircraft tracking)",
    "pocsag": "rtl_fm + multimon-ng (POCSAG pager decoding)",
    "ais": "the AIS receiver (vessel tracking)",
    "rtl433": "rtl_433 (ISM band scanning)",
    "meteor": "SatDump (Meteor-M satellite decoding)",
}
STOP_TOOLS = {
    "adsb": "aviation_stop_tracking",
    "pocsag": "pager_stop_decoding",
    "ais": "marine_stop_tracking",
    "rtl433": "ism_stop_scanning",
    "meteor": "satellite_stop_decoding",
}
CAPTURE_TASKS = {"recorder": "an IQ recording (recording_stop)",
                 "audio_recorder": "an audio recording (audio_record_stop)"}

# Paging allocations scanned by pager_find_frequencies (Hz)
PAGER_BANDS = {
    "vhf": [(152.0e6, 153.0e6), (157.4e6, 158.1e6)],
    "uhf": [(454.0e6, 455.0e6), (462.7e6, 463.2e6)],
    "900": [(929.0e6, 932.0e6)],
}
LOCATION_ENV = ("AETHERLINK_LATITUDE", "AETHERLINK_LONGITUDE")


class SDRError(Exception):
    """Error raised by SDR operations. Propagates to MCP clients as isError: true."""


def _text(message: str) -> List[TextContent]:
    return [TextContent(type="text", text=message)]


def _json_safe(obj: Any) -> Any:
    """Make nested data JSON-serializable (datetimes and other objects become strings)."""
    return json.loads(json.dumps(obj, default=str))


def _output_schema(**properties: str) -> Dict[str, Any]:
    """Loose structured-output schema: named top-level fields with JSON types, extras allowed."""
    return {
        "type": "object",
        "properties": {name: {"type": json_type} for name, json_type in properties.items()},
        "additionalProperties": True,
    }


class SDRMCPServer:
    """MCP Server for SDR control"""

    def __init__(self):
        self.server = Server(
            "sdr-mcp",
            version=__version__,
            instructions=(
                "AetherLink SDR MCP Server. Controls RTL-SDR and HackRF software-defined "
                "radio hardware for spectrum analysis, signal decoding (ADS-B aircraft, AIS "
                "ships, POCSAG pagers, Meteor-M satellites, ISM band devices), recording, "
                "and transmission (HackRF only). All frequencies are in Hz. "
                "Spectrum, recording and audio tools need sdr_connect first. The decoder "
                "tools (aviation_*, pager_*, marine_*, ism_*, satellite_*) run external "
                "programs that take exclusive control of the radio; Python-side control is "
                "paused while one runs and restored when it is stopped. A receiver location "
                "(latitude/longitude arguments, or the AETHERLINK_LATITUDE and "
                "AETHERLINK_LONGITUDE environment variables) enables aircraft distances and "
                "satellite pass prediction."
            ),
        )
        self.sdr: Optional[SDRDevice] = None
        self.sdr_lease: Optional[str] = None       # external decoder currently holding the radio
        self._sdr_snapshot: Optional[Dict[str, Any]] = None

        self.adsb_decoder = ADSBDecoder()
        self.pocsag_decoder = POCSAGDecoder()
        self.ais_decoder = AISDecoder()
        self.rtl433_decoder = RTL433Decoder()
        self.meteor_decoder = MeteorLRPTDecoder()
        self.meteor_state: Dict[str, Any] = {"status": "idle"}
        self.pass_predictor = PassPredictor(cache_dir=recordings_dir())
        self.active_decoders: Dict[str, asyncio.Task] = {}

        # Analysis modules
        self.spectrum_analyzer = SpectrumAnalyzer()
        self.signal_recorder = SignalRecorder()
        self.audio_recorder = AudioRecorder()
        self.frequency_scanner = FrequencyScanner(self.spectrum_analyzer)

        self.setup_handlers()

    # ====================================================================== helpers

    # ---- background task bookkeeping

    def _task_running(self, name: str) -> bool:
        task = self.active_decoders.get(name)
        if task is None:
            return False
        if task.done():
            self.active_decoders.pop(name, None)
            return False
        return True

    def _running_tasks(self) -> List[str]:
        return [name for name in list(self.active_decoders) if self._task_running(name)]

    async def _stop_task(self, name: str) -> bool:
        """Cancel a background task and wait for its cleanup. Returns True if it was running."""
        task = self.active_decoders.pop(name, None)
        if task is None or task.done():
            return False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"{name} task raised during stop: {e}")
        return True

    async def _start_task(self, name: str, coro, failure_prefix: str,
                          startup_wait: float = STARTUP_WAIT) -> None:
        """Start a background task and surface an immediate failure as an SDRError."""
        task = asyncio.create_task(coro)
        self.active_decoders[name] = task
        await asyncio.sleep(startup_wait)
        if task.done():
            self.active_decoders.pop(name, None)
            try:
                task.result()
            except asyncio.CancelledError:
                raise SDRError(f"{failure_prefix}: stopped before it started")
            except Exception as e:
                raise SDRError(f"{failure_prefix}: {e}")

    # ---- device access rules

    def _lease_error(self) -> SDRError:
        holder = self.sdr_lease
        return SDRError(
            f"The SDR is currently in use by {HOLDER_LABELS.get(holder, holder)}. "
            f"Stop it with {STOP_TOOLS.get(holder, 'the matching stop tool')} first."
        )

    def _require_sdr(self) -> SDRDevice:
        """Python-side control of a connected radio."""
        if self.sdr_lease:
            raise self._lease_error()
        if not self.sdr:
            raise SDRError("No SDR connected. Use sdr_connect first.")
        return self.sdr

    def _require_capture_idle(self):
        """Only one Python-side capture (sync read or stream) can run at a time."""
        for name, label in CAPTURE_TASKS.items():
            if self._task_running(name):
                raise SDRError(f"The SDR is busy with {label}. Stop it first.")

    def _require_device_free(self):
        """An external decoder is about to take the radio."""
        if self.sdr_lease:
            raise self._lease_error()
        self._require_capture_idle()

    def _device_index(self) -> int:
        if self.sdr:
            return self.sdr.device_index
        if self._sdr_snapshot:
            return self._sdr_snapshot["index"]
        return 0

    async def _release_sdr(self, holder: str):
        """Hand the radio to an external process, remembering settings for _restore_sdr."""
        self.sdr_lease = holder
        if self.sdr:
            self._sdr_snapshot = {
                "type": "hackrf" if isinstance(self.sdr, HackRFDevice) else "rtlsdr",
                "index": self.sdr.device_index,
                "frequency": self.sdr.frequency,
                "sample_rate": self.sdr.sample_rate,
                "gain": self.sdr.gain,
                "ppm": getattr(self.sdr, "ppm", 0),
                "bias_tee": getattr(self.sdr, "bias_tee", False),
            }
            logger.info(f"Releasing SDR device for {holder}")
            await self.sdr.disconnect()
            self.sdr = None
            gc.collect()
            await asyncio.sleep(0.5)  # give the OS time to release the USB device
        else:
            self._sdr_snapshot = None

    async def _restore_sdr(self):
        """Reconnect the radio with the settings it had before an external decoder ran."""
        snapshot, self._sdr_snapshot = self._sdr_snapshot, None
        self.sdr_lease = None
        if not snapshot:
            return
        logger.info("Reconnecting Python SDR control")
        device: SDRDevice = (
            HackRFDevice(snapshot["index"]) if snapshot["type"] == "hackrf"
            else RTLSDRDevice(snapshot["index"])
        )
        if snapshot.get("ppm") and hasattr(device, "ppm"):
            device.ppm = snapshot["ppm"]          # RTL-SDR re-applies this on connect
        try:
            if not await device.connect():
                logger.error("Could not reconnect the SDR after the external decoder finished")
                return
            if snapshot.get("bias_tee"):
                try:
                    await device.set_bias_tee(True)
                except Exception as e:
                    logger.warning(f"Could not restore bias tee: {e}")
            for setter, key in (
                (device.set_sample_rate, "sample_rate"),
                (device.set_frequency, "frequency"),
                (device.set_gain, "gain"),
            ):
                try:
                    await setter(snapshot[key])
                except Exception as e:
                    logger.warning(f"Could not restore {key}={snapshot[key]!r}: {e}")
            self.sdr = device
        except Exception as e:
            logger.error(f"Error reconnecting SDR: {e}")

    @staticmethod
    def _receiver_location(arguments: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
        """Receiver lat/lon from tool arguments, falling back to the environment."""
        lat = arguments.get("receiver_latitude", arguments.get("latitude"))
        lon = arguments.get("receiver_longitude", arguments.get("longitude"))
        if lat is None or lon is None:
            env_lat, env_lon = (os.environ.get(k) for k in LOCATION_ENV)
            if env_lat and env_lon:
                try:
                    lat, lon = float(env_lat), float(env_lon)
                except ValueError:
                    raise SDRError(f"{LOCATION_ENV[0]} / {LOCATION_ENV[1]} must be decimal degrees")
        if lat is None or lon is None:
            return None, None
        lat, lon = float(lat), float(lon)
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise SDRError("Latitude must be between -90 and 90 and longitude between -180 and 180")
        return lat, lon

    def _status_dict(self) -> Dict[str, Any]:
        active = self._running_tasks()
        if self.sdr_lease and not self.sdr:
            holder = self.sdr_lease
            return {
                "connected": False,
                "device_in_use_by": HOLDER_LABELS.get(holder, holder),
                "message": (
                    f"The radio is held by {HOLDER_LABELS.get(holder, holder)}. "
                    f"Python SDR control resumes after {STOP_TOOLS.get(holder, 'stop')}."
                ),
                "active_decoders": active,
            }
        if not self.sdr:
            return {"connected": False, "message": "No SDR connected", "active_decoders": active}
        return {
            "connected": True,
            "device_name": self.sdr.device_name,
            "device_index": self.sdr.device_index,
            "frequency": self.sdr.frequency,
            "sample_rate": self.sdr.sample_rate,
            "gain": self.sdr.gain,
            "is_capturing": self.sdr.is_capturing,
            "active_decoders": active,
        }

    # ---- subprocess helpers

    @staticmethod
    async def _terminate(process, name: str):
        if process is None or process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=3.0)
        except (asyncio.TimeoutError, Exception):
            try:
                process.kill()
                await process.wait()
            except Exception:
                pass
        logger.debug(f"{name} terminated")

    @staticmethod
    async def _pump_lines(stream, name: str, tail: Deque[str],
                          on_line: Optional[Callable[[str], bool]] = None):
        """Read a subprocess stream line by line, keeping a tail for diagnostics."""
        error_markers = ("error", "failed", "no supported devices", "usb_claim", "cannot open")
        try:
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode(errors="replace").rstrip()
                if not text:
                    continue
                tail.append(text)
                if on_line and on_line(text):
                    continue
                lowered = text.lower()
                level = logging.ERROR if any(m in lowered for m in error_markers) else logging.DEBUG
                logger.log(level, f"{name}: {text}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"{name} stream closed: {e}")

    @staticmethod
    def _tail_text(tail: Deque[str], n: int = 5) -> str:
        return " | ".join(list(tail)[-n:]) if tail else "(no output)"

    # ====================================================================== MCP handlers

    def setup_handlers(self):
        """Setup MCP server handlers"""

        @self.server.list_tools()
        async def list_tools() -> List[Tool]:
            """List available SDR tools"""
            return [
                Tool(
                    name="sdr_connect",
                    description=(
                        "Connect to SDR hardware (RTL-SDR or HackRF) for Python-side control: "
                        "spectrum analysis, scanning, IQ and audio recording, HackRF transmit."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "device_type": {
                                "type": "string",
                                "enum": ["rtlsdr", "hackrf"],
                                "default": "rtlsdr"
                            },
                            "device_index": {
                                "type": "integer",
                                "description": "Device index if multiple devices connected",
                                "default": 0,
                                "minimum": 0,
                            }
                        }
                    }
                ),
                Tool(
                    name="sdr_disconnect",
                    description="Stop all activity and disconnect from SDR hardware",
                    inputSchema={"type": "object", "properties": {}}
                ),
                Tool(
                    name="sdr_set_frequency",
                    description="Set SDR center frequency in Hz",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "frequency": {
                                "type": "number",
                                "description": "Frequency in Hz (e.g., 1090000000 for 1090 MHz)"
                            }
                        },
                        "required": ["frequency"]
                    }
                ),
                Tool(
                    name="sdr_set_gain",
                    description="Set SDR gain in dB or 'auto'",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "gain": {
                                "oneOf": [
                                    {"type": "number", "description": "Gain in dB"},
                                    {"type": "string", "enum": ["auto"]}
                                ]
                            }
                        },
                        "required": ["gain"]
                    }
                ),
                Tool(
                    name="sdr_get_status",
                    description=(
                        "Get current SDR status: connection, tuning, which decoders are running, "
                        "and whether an external decoder currently holds the radio"
                    ),
                    inputSchema={"type": "object", "properties": {}},
                    outputSchema=_output_schema(connected="boolean", active_decoders="array"),
                ),
                Tool(
                    name="sdr_list_devices",
                    description="List attached RTL-SDR and HackRF devices (index, name, serial) without opening them",
                    inputSchema={"type": "object", "properties": {}},
                    outputSchema=_output_schema(devices="array"),
                ),
                Tool(
                    name="sdr_set_sample_rate",
                    description="Set the SDR sample rate in Hz (RTL-SDR 225 kHz-3.2 MHz, HackRF 2-20 MHz)",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "sample_rate": {
                                "type": "number",
                                "description": "Sample rate in Hz (e.g., 2048000)",
                                "exclusiveMinimum": 0,
                            }
                        },
                        "required": ["sample_rate"]
                    }
                ),
                Tool(
                    name="sdr_set_ppm",
                    description="Set the RTL-SDR frequency correction in parts per million (from rtl_test -p or a known reference)",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "ppm": {
                                "type": "integer",
                                "description": "Correction in PPM",
                                "minimum": -1000,
                                "maximum": 1000,
                            }
                        },
                        "required": ["ppm"]
                    }
                ),
                Tool(
                    name="sdr_set_bias_tee",
                    description="Enable or disable antenna port power (bias tee) for powering an LNA (RTL-SDR Blog V3/V4, HackRF)",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "enabled": {"type": "boolean", "default": True}
                        },
                        "required": ["enabled"]
                    }
                ),
                Tool(
                    name="aviation_track_aircraft",
                    description=(
                        "Start tracking aircraft via ADS-B on 1090 MHz using dump1090. Takes exclusive "
                        "control of the RTL-SDR until aviation_stop_tracking or the duration elapses."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "gain": {
                                "type": "number",
                                "description": "RTL-SDR gain in dB for dump1090",
                                "default": 40,
                            },
                            "duration": {
                                "type": "integer",
                                "description": "Tracking duration in seconds; 0 runs until stopped",
                                "default": 120,
                                "minimum": 0,
                            },
                            "aggressive": {
                                "type": "boolean",
                                "description": "Enable dump1090 aggressive mode",
                                "default": True,
                            },
                            "fix_crc": {
                                "type": "boolean",
                                "description": "Enable dump1090 single-bit CRC correction",
                                "default": True,
                            },
                            "receiver_latitude": {
                                "type": "number",
                                "description": (
                                    "Receiver latitude in decimal degrees. With a location, positions decode from "
                                    "single frames and the aircraft list shows distance. Falls back to "
                                    "AETHERLINK_LATITUDE."
                                ),
                            },
                            "receiver_longitude": {
                                "type": "number",
                                "description": "Receiver longitude in decimal degrees. Falls back to AETHERLINK_LONGITUDE.",
                            },
                        },
                    }
                ),
                Tool(
                    name="aviation_stop_tracking",
                    description="Stop tracking aircraft and return the radio to Python control",
                    inputSchema={"type": "object", "properties": {}}
                ),
                Tool(
                    name="aviation_get_aircraft",
                    description="Get list of currently tracked aircraft",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "max_age": {
                                "type": "integer",
                                "description": "Maximum aircraft age in seconds",
                                "default": 120,
                            },
                            "include_inactive": {
                                "type": "boolean",
                                "description": "Include all aircraft seen since tracking started",
                                "default": False,
                            },
                            "lookup_registrations": {
                                "type": "boolean",
                                "description": "Resolve registration, type, and operator via hexdb.io (network lookup)",
                                "default": True,
                            },
                        },
                    },
                    outputSchema=_output_schema(active="boolean", statistics="object", aircraft="array"),
                ),
                Tool(
                    name="pager_start_decoding",
                    description=(
                        "Start decoding POCSAG pager messages using rtl_fm piped into multimon-ng. "
                        "Takes exclusive control of the RTL-SDR until pager_stop_decoding. Common "
                        "paging frequencies: 152-153 MHz, 454 MHz, 929-931 MHz (check RadioReference "
                        "for your area)."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "frequency": {
                                "type": "number",
                                "description": (
                                    "Pager frequency in Hz (e.g., 152240000). Defaults to the "
                                    "connected SDR's current frequency."
                                ),
                            },
                            "baud_rate": {
                                "type": "integer",
                                "enum": [512, 1200, 2400],
                                "description": "Decode only this POCSAG baud rate; omit to decode all three",
                            },
                            "gain": {
                                "type": "number",
                                "description": "RTL-SDR gain in dB",
                                "default": 40,
                            },
                            "duration": {
                                "type": "integer",
                                "description": "Decoding duration in seconds; 0 runs until stopped",
                                "default": 0,
                                "minimum": 0,
                            },
                        }
                    }
                ),
                Tool(
                    name="pager_stop_decoding",
                    description="Stop decoding POCSAG pager messages and return the radio to Python control",
                    inputSchema={"type": "object", "properties": {}}
                ),
                Tool(
                    name="pager_get_messages",
                    description="Get decoded pager messages (newest last)",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "max_age": {
                                "type": "integer",
                                "description": "Only messages from the last N seconds; 0 for all",
                                "default": 0,
                                "minimum": 0,
                            },
                            "limit": {
                                "type": "integer",
                                "description": "Maximum number of messages to return",
                                "default": 20,
                                "minimum": 1,
                            },
                        }
                    },
                    outputSchema=_output_schema(active="boolean", statistics="object", messages="array"),
                ),
                Tool(
                    name="pager_find_frequencies",
                    description=(
                        "Scan the common paging bands (VHF 152-153/157-158 MHz, UHF 454-455/462-463 MHz, "
                        "929-932 MHz) for narrowband carriers that are likely pager channels, strongest first. "
                        "Needs sdr_connect. Follow up with pager_start_decoding on a candidate."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "bands": {
                                "type": "array",
                                "items": {"type": "string", "enum": ["vhf", "uhf", "900"]},
                                "description": "Bands to scan; default all three",
                            },
                            "min_snr": {
                                "type": "number",
                                "description": "Minimum SNR in dB for a candidate",
                                "default": 10,
                            },
                            "max_results": {
                                "type": "integer",
                                "description": "Maximum candidates to return",
                                "default": 15,
                                "minimum": 1,
                            },
                        }
                    },
                    outputSchema=_output_schema(bands_scanned="array", candidates="array"),
                ),
                Tool(
                    name="marine_track_vessels",
                    description=(
                        "Start tracking ships via AIS (161.975 and 162.025 MHz, both channels) using "
                        "AIS-catcher or rtl_ais plus pyais. Takes exclusive control of the RTL-SDR "
                        "until marine_stop_tracking. Needs a coastal or waterway location."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "gain": {
                                "type": "number",
                                "description": "RTL-SDR tuner gain in dB",
                                "default": 40,
                            },
                            "duration": {
                                "type": "integer",
                                "description": "Tracking duration in seconds; 0 runs until stopped",
                                "default": 0,
                                "minimum": 0,
                            },
                        }
                    }
                ),
                Tool(
                    name="marine_stop_tracking",
                    description="Stop tracking ships and return the radio to Python control",
                    inputSchema={"type": "object", "properties": {}}
                ),
                Tool(
                    name="marine_get_vessels",
                    description="Get list of tracked vessels",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "max_age": {
                                "type": "integer",
                                "description": "Maximum vessel age in seconds",
                                "default": 600,
                                "minimum": 1,
                            },
                        }
                    },
                    outputSchema=_output_schema(active="boolean", statistics="object", vessels="array"),
                ),
                Tool(
                    name="satellite_decode_meteor",
                    description=(
                        "Start decoding a Meteor-M weather satellite LRPT pass with SatDump in the "
                        "background. Takes exclusive control of the RTL-SDR for the duration. Check "
                        "progress with satellite_get_status; images land in the recordings directory."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "satellite": {
                                "type": "string",
                                "enum": ["METEOR-M2-3", "METEOR-M2-4"],
                                "description": "Meteor satellite identifier",
                                "default": "METEOR-M2-4"
                            },
                            "duration": {
                                "type": "integer",
                                "description": "Recording duration in seconds (typically 600-900 for a full pass)",
                                "default": 600,
                                "minimum": 30,
                                "maximum": MAX_METEOR_DURATION,
                            },
                            "gain": {
                                "type": "number",
                                "description": "RTL-SDR gain in dB",
                                "default": 40
                            }
                        }
                    }
                ),
                Tool(
                    name="satellite_get_status",
                    description="Get the status and results of the current or last Meteor-M decode",
                    inputSchema={"type": "object", "properties": {}},
                    outputSchema=_output_schema(status="string", running="boolean", images="array"),
                ),
                Tool(
                    name="satellite_stop_decoding",
                    description="Stop the running Meteor-M decode early and return the radio to Python control",
                    inputSchema={"type": "object", "properties": {}}
                ),
                Tool(
                    name="satellite_predict_passes",
                    description=(
                        "Predict upcoming Meteor-M passes over a location (rise/set times, peak elevation, "
                        "azimuths) using current orbital elements from Celestrak. Needs network access. Use it "
                        "to decide when to run satellite_decode_meteor."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "satellite": {
                                "type": "string",
                                "enum": ["METEOR-M2-3", "METEOR-M2-4"],
                                "default": "METEOR-M2-4"
                            },
                            "latitude": {
                                "type": "number",
                                "description": "Observer latitude in decimal degrees (falls back to AETHERLINK_LATITUDE)",
                            },
                            "longitude": {
                                "type": "number",
                                "description": "Observer longitude in decimal degrees (falls back to AETHERLINK_LONGITUDE)",
                            },
                            "altitude_m": {
                                "type": "number",
                                "description": "Observer altitude in metres",
                                "default": 0,
                            },
                            "hours": {
                                "type": "number",
                                "description": "Look-ahead window in hours (max 168)",
                                "default": 24,
                                "exclusiveMinimum": 0,
                                "maximum": 168,
                            },
                            "min_elevation": {
                                "type": "number",
                                "description": "Only report passes whose peak elevation reaches this many degrees",
                                "default": 20,
                                "minimum": 0,
                                "maximum": 90,
                            },
                        }
                    },
                    outputSchema=_output_schema(satellite="string", passes="array", elements="object"),
                ),
                Tool(
                    name="spectrum_analyze",
                    description="Perform spectrum analysis (FFT, signal detection) at the current frequency",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "fft_size": {
                                "type": "integer",
                                "description": "FFT size (power of 2)",
                                "default": 2048
                            },
                            "window": {
                                "type": "string",
                                "description": "Window function",
                                "enum": ["hamming", "hann", "blackman", "blackman-harris", "flattop"],
                                "default": "blackman-harris"
                            },
                            "averaging": {
                                "type": "boolean",
                                "description": "Enable spectrum averaging",
                                "default": True
                            }
                        }
                    },
                    outputSchema=_output_schema(center_freq_hz="number", peak_power_db="number",
                                                noise_floor_db="number", signals="array"),
                ),
                Tool(
                    name="spectrum_scan",
                    description="Scan a frequency range for signals (at most 2000 steps)",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "start_freq": {
                                "type": "number",
                                "description": "Start frequency in Hz"
                            },
                            "stop_freq": {
                                "type": "number",
                                "description": "Stop frequency in Hz"
                            },
                            "step": {
                                "type": "number",
                                "description": "Step size in Hz (must be > 0)",
                                "default": 1000000,
                                "exclusiveMinimum": 0,
                            },
                            "dwell_time": {
                                "type": "number",
                                "description": "Dwell time per frequency in seconds (0.01-5)",
                                "default": 0.1,
                                "minimum": 0.01,
                                "maximum": 5,
                            }
                        },
                        "required": ["start_freq", "stop_freq"]
                    },
                    outputSchema=_output_schema(summary="object", results="array"),
                ),
                Tool(
                    name="recording_start",
                    description="Start recording raw IQ samples (complex float32) to a file",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "description": {
                                "type": "string",
                                "description": "Recording description",
                                "default": ""
                            }
                        }
                    }
                ),
                Tool(
                    name="recording_stop",
                    description="Stop the current IQ recording",
                    inputSchema={"type": "object", "properties": {}}
                ),
                Tool(
                    name="audio_record_start",
                    description="Start recording demodulated audio (FM/AM) at the current frequency to a WAV file",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "modulation": {
                                "type": "string",
                                "description": "Modulation type: FM or AM",
                                "enum": ["FM", "AM"],
                                "default": "FM"
                            },
                            "description": {
                                "type": "string",
                                "description": "Recording description",
                                "default": ""
                            }
                        }
                    }
                ),
                Tool(
                    name="audio_record_stop",
                    description="Stop the current audio recording",
                    inputSchema={"type": "object", "properties": {}}
                ),
                Tool(
                    name="hackrf_set_tx_gain",
                    description="Set HackRF transmit gain (0-47 dB)",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "gain": {
                                "type": "integer",
                                "description": "TX VGA gain in dB (0-47)",
                                "minimum": 0,
                                "maximum": 47
                            }
                        },
                        "required": ["gain"]
                    }
                ),
                Tool(
                    name="signal_generator",
                    description=(
                        "Generate and transmit a test signal (HackRF only, max 60 s). Transmission on "
                        "aviation, public safety, cellular, GNSS and other protected bands is refused."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "frequency": {
                                "type": "number",
                                "description": "Transmit frequency in Hz"
                            },
                            "signal_type": {
                                "type": "string",
                                "enum": ["cw", "tone", "noise", "sweep"],
                                "description": "Type of signal to generate"
                            },
                            "duration": {
                                "type": "number",
                                "description": "Duration in seconds (max 60)",
                                "default": 1.0,
                                "exclusiveMinimum": 0,
                                "maximum": MAX_TX_DURATION,
                            },
                            "tone_freq": {
                                "type": "number",
                                "description": "Tone frequency for 'tone' type (Hz)",
                                "default": 1000
                            }
                        },
                        "required": ["frequency", "signal_type"]
                    }
                ),
                Tool(
                    name="ism_start_scanning",
                    description=(
                        "Start scanning ISM bands for devices (433/315/868/915 MHz) using rtl_433. "
                        "Takes exclusive control of the RTL-SDR until ism_stop_scanning."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "frequencies": {
                                "type": "array",
                                "description": "List of frequencies to scan in Hz (e.g., [433920000, 315000000])",
                                "items": {"type": "number"},
                                "default": [433920000, 315000000]
                            },
                            "hop_interval": {
                                "type": "integer",
                                "description": "Hop between frequencies every N seconds",
                                "default": 30,
                                "minimum": 1,
                                "maximum": 3600,
                            }
                        }
                    }
                ),
                Tool(
                    name="ism_stop_scanning",
                    description="Stop ISM band scanning and return the radio to Python control",
                    inputSchema={"type": "object", "properties": {}}
                ),
                Tool(
                    name="ism_get_devices",
                    description="Get list of detected ISM band devices (weather stations, sensors, etc.)",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "max_age": {
                                "type": "integer",
                                "description": "Maximum age of devices in seconds",
                                "default": 300
                            }
                        }
                    },
                    outputSchema=_output_schema(scanning="boolean", statistics="object", devices="array"),
                )
            ]

        @self.server.call_tool()
        async def call_tool(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
            """Handle tool calls"""
            arguments = arguments or {}

            # ---------------------------------------------------------- core control
            if name == "sdr_connect":
                device_type = arguments.get("device_type", "rtlsdr")
                device_index = int(arguments.get("device_index", 0) or 0)

                if self.sdr_lease:
                    raise self._lease_error()
                if self.sdr:
                    if self._running_tasks():
                        raise SDRError(
                            "An SDR is already connected and busy. Stop the active tasks or "
                            "use sdr_disconnect first."
                        )
                    await self.sdr.disconnect()
                    self.sdr = None

                if device_type == "rtlsdr":
                    if not RTLSDR_AVAILABLE:
                        raise SDRError(f"RTL-SDR support is unavailable: {RTLSDR_IMPORT_ERROR}. {RTLSDR_INSTALL_HINT}")
                    device: SDRDevice = RTLSDRDevice(device_index)
                elif device_type == "hackrf":
                    if not HACKRF_AVAILABLE:
                        raise SDRError(f"HackRF support is unavailable: {HACKRF_IMPORT_ERROR}. {HACKRF_INSTALL_HINT}")
                    device = HackRFDevice(device_index)
                else:
                    raise SDRError(f"Unsupported device type: {device_type}")

                if not await device.connect():
                    raise SDRError(
                        f"Failed to connect to {device.device_name} (device index {device_index}). "
                        "Check the USB connection and make sure no other program (GQRX, SDR#, "
                        "dump1090, rtl_433) is using the device."
                    )
                self.sdr = device
                info = await device.get_info()
                details = []
                if info.get("tuner_type"):
                    details.append(f"Tuner: {info['tuner_type']}")
                if info.get("serial"):
                    details.append(f"Serial: {info['serial']}")
                if info.get("firmware"):
                    details.append(f"Firmware: {info['firmware']}")
                return _text(
                    f"Successfully connected to {device.device_name} (index {device_index})\n"
                    + (" | ".join(details) + "\n" if details else "")
                    + f"Frequency: {device.frequency/1e6:.3f} MHz | Sample rate: "
                    f"{device.sample_rate/1e6:.3f} Msps | Gain: {device.gain}"
                )

            elif name == "sdr_disconnect":
                stopped = []
                for task_name in self._running_tasks():
                    if await self._stop_task(task_name):
                        stopped.append(task_name)
                # Finalize any recording files the tasks left open
                if self.signal_recorder.is_recording:
                    await self.signal_recorder.stop_recording()
                if self.audio_recorder.is_recording:
                    await self.audio_recorder.stop_recording()
                if not self.sdr:
                    if stopped:
                        return _text(f"Stopped {', '.join(stopped)}; no Python SDR connection to close")
                    raise SDRError("No SDR connected")
                await self.sdr.disconnect()
                self.sdr = None
                return _text("Disconnected from SDR" + (f" (stopped {', '.join(stopped)})" if stopped else ""))

            elif name == "sdr_set_frequency":
                sdr = self._require_sdr()
                freq = float(arguments["frequency"])
                try:
                    await sdr.set_frequency(freq)
                except ValueError as e:
                    raise SDRError(str(e))
                return _text(f"Set frequency to {freq/1e6:.3f} MHz")

            elif name == "sdr_set_gain":
                sdr = self._require_sdr()
                gain = arguments["gain"]
                try:
                    await sdr.set_gain(gain)
                except ValueError as e:
                    raise SDRError(str(e))
                if isinstance(sdr, HackRFDevice):
                    return _text(
                        f"Set gain to {gain} (LNA {sdr.lna_gain} dB, VGA {sdr.vga_gain} dB"
                        f"{', amp on' if sdr.amp_enable else ''})"
                    )
                return _text(f"Set gain to {gain}")

            elif name == "sdr_get_status":
                status = self._status_dict()
                return _text(json.dumps(status, indent=2, default=str)), _json_safe(status)

            elif name == "sdr_list_devices":
                devices = await asyncio.to_thread(
                    lambda: RTLSDRDevice.list_devices() + HackRFDevice.list_devices()
                )
                connected = None
                if self.sdr:
                    connected = {
                        "type": "hackrf" if isinstance(self.sdr, HackRFDevice) else "rtlsdr",
                        "index": self.sdr.device_index,
                    }
                result = f"Attached SDR devices: {len(devices)}\n"
                if self.sdr_lease:
                    result += f"(the radio is currently held by {HOLDER_LABELS.get(self.sdr_lease, self.sdr_lease)})\n"
                for dev in devices:
                    marker = ""
                    if connected and connected["type"] == dev["type"] and connected["index"] == dev["index"]:
                        marker = "  <- connected"
                    serial = f" serial {dev['serial']}" if dev.get("serial") else ""
                    result += f"  [{dev['type']} #{dev['index']}] {dev['name']}{serial}{marker}\n"
                if not devices:
                    result += "No RTL-SDR or HackRF found. Check the USB connection"
                    if not RTLSDR_AVAILABLE:
                        result += f"; RTL-SDR support unavailable: {RTLSDR_IMPORT_ERROR}"
                    if not HACKRF_AVAILABLE:
                        result += "; HackRF library not installed"
                    result += ".\n"
                result += "\nUse sdr_connect with device_type and device_index to open one."
                return _text(result), {
                    "devices": devices,
                    "connected": connected,
                    "rtlsdr_support": RTLSDR_AVAILABLE,
                    "hackrf_support": HACKRF_AVAILABLE,
                }

            elif name == "sdr_set_sample_rate":
                sdr = self._require_sdr()
                self._require_capture_idle()
                rate = float(arguments["sample_rate"])
                try:
                    await sdr.set_sample_rate(rate)
                except (ValueError, RuntimeError) as e:
                    raise SDRError(str(e))
                return _text(f"Set sample rate to {rate/1e6:.3f} Msps")

            elif name == "sdr_set_ppm":
                sdr = self._require_sdr()
                ppm = int(arguments["ppm"])
                try:
                    await sdr.set_freq_correction(ppm)
                except (NotImplementedError, ValueError, RuntimeError) as e:
                    raise SDRError(str(e))
                return _text(f"Set frequency correction to {ppm} ppm")

            elif name == "sdr_set_bias_tee":
                sdr = self._require_sdr()
                enabled = bool(arguments.get("enabled", True))
                try:
                    await sdr.set_bias_tee(enabled)
                except (NotImplementedError, ValueError, RuntimeError) as e:
                    raise SDRError(str(e))
                return _text(f"Bias tee (antenna port power) {'enabled' if enabled else 'disabled'}")

            # ---------------------------------------------------------- ADS-B
            elif name == "aviation_track_aircraft":
                if self._task_running("adsb"):
                    return _text("ADS-B tracking already active")
                if not ADSB_AVAILABLE:
                    raise SDRError(
                        "pyModeS is not installed. Install with: pip install pyModeS "
                        "(or pip install 'aetherlink[decoders]')"
                    )
                self._require_device_free()

                gain = arguments.get("gain", 40)
                duration_arg = arguments.get("duration", 120)
                duration = 120 if duration_arg is None else int(duration_arg)
                aggressive = bool(arguments.get("aggressive", True))
                fix_crc = bool(arguments.get("fix_crc", True))
                rx_lat, rx_lon = self._receiver_location(arguments)
                try:
                    self.adsb_decoder.set_receiver_location(rx_lat, rx_lon)
                except ValueError as e:
                    raise SDRError(str(e))

                await self._start_task(
                    "adsb",
                    self._adsb_decoder_task(gain=str(gain), duration=duration,
                                            aggressive=aggressive, fix_crc=fix_crc),
                    "Failed to start ADS-B tracking",
                )
                duration_msg = "until stopped" if duration <= 0 else f"for {duration} seconds"
                if rx_lat is not None:
                    location_msg = f"Receiver location: {rx_lat:.4f}, {rx_lon:.4f} (distances and single-frame positions enabled)\n"
                else:
                    location_msg = (
                        "Tip: pass receiver_latitude/receiver_longitude (or set AETHERLINK_LATITUDE and "
                        "AETHERLINK_LONGITUDE) to get distances and faster position fixes.\n"
                    )
                return _text(
                    "Started ADS-B aircraft tracking on 1090 MHz with dump1090\n"
                    f"Gain: {gain} dB | Duration: {duration_msg}\n"
                    + location_msg +
                    "\nNOTE: Python SDR control is paused while tracking.\n"
                    "Use aviation_stop_tracking to regain SDR control."
                )

            elif name == "aviation_stop_tracking":
                if await self._stop_task("adsb"):
                    return _text("Stopped ADS-B tracking")
                return _text("ADS-B tracking not active")

            elif name == "aviation_get_aircraft":
                max_age = int(arguments.get("max_age", 120) or 120)
                include_inactive = bool(arguments.get("include_inactive", False))
                lookup_registrations = bool(arguments.get("lookup_registrations", True))

                aircraft_list = self.adsb_decoder.get_aircraft_list(
                    max_age_seconds=max_age,
                    include_inactive=include_inactive,
                )
                if lookup_registrations and aircraft_list:
                    await asyncio.gather(*[
                        asyncio.to_thread(self.adsb_decoder.lookup_aircraft, ac["icao"])
                        for ac in aircraft_list[:25]
                    ])
                    aircraft_list = self.adsb_decoder.get_aircraft_list(
                        max_age_seconds=max_age,
                        include_inactive=include_inactive,
                    )

                stats = self.adsb_decoder.get_statistics(max_age_seconds=max_age)
                decoder_active = self._task_running("adsb")
                have_receiver = self.adsb_decoder.receiver_lat is not None
                structured = {
                    "active": decoder_active,
                    "statistics": stats,
                    "aircraft": _json_safe(aircraft_list),
                }

                summary = f"ADS-B Aircraft Tracker ({'active' if decoder_active else 'stopped'})\n"
                summary += (
                    f"Raw messages: {stats['raw_messages']} | Decoded: {stats['decoded_messages']} | "
                    f"Position fixes: {stats['positions_decoded']}\n"
                )
                summary += (
                    f"Aircraft seen: {stats['total_aircraft_seen']} | Active: {len(aircraft_list)} | "
                    f"With position: {stats['with_position']}\n"
                )
                summary += (
                    f"{stats['identified_callsigns']} callsigns | "
                    f"{stats['with_altitude']} with altitude | "
                    f"{stats['climbing']} climbing | {stats['descending']} descending\n"
                )
                if have_receiver:
                    summary += (
                        f"Receiver: {self.adsb_decoder.receiver_lat:.4f}, "
                        f"{self.adsb_decoder.receiver_lon:.4f} (nearest first)\n"
                    )
                summary += "\n"

                if not aircraft_list and stats["total_aircraft_seen"] > 0:
                    summary += (
                        "Aircraft were detected, but none match the current age filter.\n"
                        "Try include_inactive=true or a larger max_age value.\n"
                    )
                    return _text(summary), structured
                if not aircraft_list:
                    summary += "No aircraft detected yet. Try a longer run or check antenna placement.\n"
                    return _text(summary), structured

                dist_hdr = f" {'Dist':>6}" if have_receiver else ""
                dist_sep = f" {'-'*6}" if have_receiver else ""
                summary += (
                    f"{'ICAO':<8} {'Reg':<10} {'Call':<9} {'Operator':<18} {'Type':<6} {'Alt':>8} "
                    f"{'Spd':>6} {'Hdg':>5} {'V/S':>7} {'Lat':>8} {'Lon':>9}{dist_hdr} {'Msgs':>5}\n"
                )
                summary += (
                    f"{'-'*8} {'-'*10} {'-'*9} {'-'*18} {'-'*6} {'-'*8} {'-'*6} {'-'*5} {'-'*7} "
                    f"{'-'*8} {'-'*9}{dist_sep} {'-'*5}\n"
                )

                for aircraft in aircraft_list[:25]:
                    reg = aircraft.get("registration") or ""
                    callsign = aircraft.get("callsign") or ""
                    operator = aircraft.get("operator") or ""
                    if len(operator) > 17:
                        operator = operator[:16] + "."
                    icao_type = aircraft.get("icao_type") or ""
                    alt = f"{aircraft['altitude']:>8,}" if aircraft.get("altitude") is not None else f"{'--':>8}"
                    speed = f"{aircraft['speed']:>6.0f}" if aircraft.get("speed") is not None else f"{'--':>6}"
                    heading = f"{aircraft['heading']:>5.0f}" if aircraft.get("heading") is not None else f"{'--':>5}"
                    vertical_rate = (
                        f"{aircraft['vertical_rate']:>+7.0f}"
                        if aircraft.get("vertical_rate") is not None else f"{'--':>7}"
                    )
                    lat = f"{aircraft['latitude']:>8.3f}" if aircraft.get("latitude") is not None else f"{'--':>8}"
                    lon = f"{aircraft['longitude']:>9.3f}" if aircraft.get("longitude") is not None else f"{'--':>9}"
                    dist = ""
                    if have_receiver:
                        dist = (
                            f" {aircraft['distance_km']:>6.1f}"
                            if aircraft.get("distance_km") is not None else f" {'--':>6}"
                        )
                    summary += (
                        f"{aircraft['icao']:<8} {reg:<10} {callsign:<9} {operator:<18} {icao_type:<6} "
                        f"{alt} {speed} {heading} {vertical_rate} {lat} {lon}{dist} "
                        f"{aircraft['message_count']:>5}\n"
                    )

                if not have_receiver:
                    summary += (
                        "\nPositions need an even+odd frame pair within 10 s. Give aviation_track_aircraft a "
                        "receiver_latitude/longitude (or set AETHERLINK_LATITUDE/LONGITUDE) to decode from single "
                        "frames and show distance.\n"
                    )
                summary += "\nLive tracking links:\n"
                for aircraft in aircraft_list[:15]:
                    label = " ".join(filter(None, [
                        aircraft.get("registration") or aircraft["icao"],
                        aircraft.get("operator") or "",
                        aircraft.get("callsign") or "",
                    ]))
                    summary += f"  {label}: {aircraft['tracking_url']}\n"

                return _text(summary), structured

            # ---------------------------------------------------------- POCSAG
            elif name == "pager_start_decoding":
                if self._task_running("pocsag"):
                    return _text("POCSAG decoding already active")
                self._require_device_free()

                frequency = arguments.get("frequency")
                if frequency is None:
                    if self.sdr:
                        frequency = self.sdr.frequency
                    else:
                        raise SDRError(
                            "Provide a pager frequency in Hz (e.g. 152240000), or connect the SDR "
                            "and tune it first."
                        )
                frequency = float(frequency)
                if not (RTLSDR_MIN_FREQ <= frequency <= RTLSDR_MAX_FREQ):
                    raise SDRError(f"Frequency {frequency/1e6:.3f} MHz is outside the RTL-SDR range (24-1766 MHz)")

                baud_rate = arguments.get("baud_rate")
                if baud_rate is not None:
                    baud_rate = int(baud_rate)
                    if baud_rate not in POCSAG_BAUD_RATES:
                        raise SDRError(f"baud_rate must be one of {POCSAG_BAUD_RATES}")
                gain = arguments.get("gain", 40)
                duration = int(arguments.get("duration", 0) or 0)

                self.pocsag_decoder.baud_rate = baud_rate
                await self._start_task(
                    "pocsag",
                    self._pocsag_decoder_task(frequency, gain, duration, baud_rate),
                    "Failed to start POCSAG decoding",
                )
                bauds = str(baud_rate) if baud_rate else "512, 1200 and 2400 (auto)"
                duration_msg = "until stopped" if duration <= 0 else f"for {duration} seconds"
                return _text(
                    "Started POCSAG pager decoding with rtl_fm + multimon-ng\n"
                    f"Frequency: {frequency/1e6:.4f} MHz | Baud: {bauds} | Gain: {gain} dB | "
                    f"Duration: {duration_msg}\n\n"
                    "Pager traffic is bursty; wait a few minutes, then use pager_get_messages.\n"
                    "NOTE: Python SDR control is paused while decoding. Use pager_stop_decoding to regain it."
                )

            elif name == "pager_stop_decoding":
                if await self._stop_task("pocsag"):
                    return _text("Stopped POCSAG decoding")
                return _text("POCSAG decoding not active")

            elif name == "pager_get_messages":
                max_age = int(arguments.get("max_age", 0) or 0)
                limit = int(arguments.get("limit", 20) or 20)
                stats = self.pocsag_decoder.get_statistics()
                messages = self.pocsag_decoder.get_messages(max_age_seconds=max_age or None, limit=limit)
                active = self._task_running("pocsag")

                result = f"POCSAG Pager Decoder ({'active' if active else 'stopped'})\n"
                if stats["frequency_MHz"]:
                    result += f"Frequency: {stats['frequency_MHz']:.4f} MHz\n"
                result += f"Messages decoded: {stats['total_messages']} "
                result += f"({stats['alpha_messages']} alpha, {stats['numeric_messages']} numeric, "
                result += f"{stats['tone_only_pages']} tone-only)\n"
                result += f"Addresses seen: {stats['addresses_seen']}\n\n"

                if not messages:
                    result += "No messages decoded yet.\n"
                    if active:
                        result += (
                            "Pagers transmit intermittently; give it a few minutes. If nothing arrives, "
                            "verify the frequency is active in your area (pager_find_frequencies can help) "
                            "and try a different gain."
                        )
                else:
                    result += f"Showing {len(messages)} message(s):\n\n"
                    for msg in messages:
                        stamp = msg["timestamp"].replace("T", " ")[:19]
                        result += f"[{stamp}] {msg['bitrate']} baud | Address {msg['address']} | Function {msg['function']}\n"
                        if msg["message_type"] == "tone":
                            result += "  (tone-only page, no text)\n\n"
                        else:
                            result += f"  {msg['message_type'].capitalize()}: {msg['message']}\n\n"

                return _text(result), {"active": active, "statistics": stats, "messages": messages}

            elif name == "pager_find_frequencies":
                sdr = self._require_sdr()
                self._require_capture_idle()

                bands = arguments.get("bands") or list(PAGER_BANDS)
                unknown = [b for b in bands if b not in PAGER_BANDS]
                if unknown:
                    raise SDRError(f"Unknown band(s) {unknown}. Choose from {list(PAGER_BANDS)}")
                min_snr = float(arguments.get("min_snr", 10) or 10)
                max_results = int(arguments.get("max_results", 15) or 15)

                original_freq = sdr.frequency
                span = 0.9 * sdr.sample_rate
                candidates: Dict[int, Dict[str, Any]] = {}
                scanned: List[Dict[str, Any]] = []
                try:
                    for band in bands:
                        for start, stop in PAGER_BANDS[band]:
                            if hasattr(sdr, "max_frequency") and stop > sdr.max_frequency:
                                continue
                            scanned.append({"band": band, "start_hz": start, "stop_hz": stop})
                            try:
                                points = await self.frequency_scanner.scan_range(
                                    sdr, start + span / 2, stop + span / 2, span, 0.1
                                )
                            except ValueError as e:
                                raise SDRError(str(e))
                            for point in points:
                                for sig in point["signals"]:
                                    f_hz = sig["frequency"]
                                    if not (start <= f_hz <= stop):
                                        continue
                                    if sig["snr"] < min_snr or sig["bandwidth"] > 60e3:
                                        continue
                                    key = int(round(f_hz / 2500.0))  # 2.5 kHz grid
                                    f_round = key * 2500.0
                                    entry = candidates.get(key)
                                    if entry is None or sig["power"] > entry["power_db"]:
                                        candidates[key] = {
                                            "frequency_hz": f_round,
                                            "frequency_mhz": round(f_round / 1e6, 4),
                                            "power_db": round(sig["power"], 1),
                                            "snr_db": round(sig["snr"], 1),
                                            "bandwidth_hz": int(round(sig["bandwidth"])),
                                            "band": band,
                                        }
                finally:
                    try:
                        await sdr.set_frequency(original_freq)
                    except Exception:
                        pass

                ranked = sorted(candidates.values(), key=lambda c: c["power_db"], reverse=True)[:max_results]
                scanned_str = ", ".join(f"{b['start_hz']/1e6:.1f}-{b['stop_hz']/1e6:.1f} MHz" for b in scanned)
                result = f"Pager frequency finder: scanned {scanned_str}\n"
                result += f"Narrowband carriers (<60 kHz) with SNR >= {min_snr:g} dB, strongest first\n\n"
                if not ranked:
                    result += (
                        "No candidate carriers found. Pagers are bursty, so repeat the scan a few times, "
                        "raise the gain, or lower min_snr. Many areas have moved paging to 929-931 MHz."
                    )
                else:
                    result += f"{'Frequency':>12} {'Power':>8} {'SNR':>7} {'BW':>8}  Band\n"
                    for c in ranked:
                        result += (
                            f"{c['frequency_mhz']:>10.4f} MHz {c['power_db']:>6.1f}dB {c['snr_db']:>5.1f}dB "
                            f"{c['bandwidth_hz']/1e3:>6.1f}kHz  {c['band']}\n"
                        )
                    best = ranked[0]
                    result += (
                        f"\nThese are carriers, not confirmed pagers. Try the strongest first:\n"
                        f"  pager_start_decoding frequency={int(best['frequency_hz'])}\n"
                        "A busy channel shows messages within a few minutes."
                    )
                return _text(result), {"bands_scanned": scanned, "min_snr_db": min_snr, "candidates": ranked}

            # ---------------------------------------------------------- AIS
            elif name == "marine_track_vessels":
                if self._task_running("ais"):
                    return _text("AIS tracking already active")
                if not AIS_AVAILABLE:
                    raise SDRError(
                        "pyais is not installed. Install with: pip install pyais "
                        "(or pip install 'aetherlink[decoders]')"
                    )
                self._require_device_free()

                gain = arguments.get("gain", 40)
                duration = int(arguments.get("duration", 0) or 0)
                receiver, _ = self._build_ais_command(gain)  # raises if no receiver installed
                await self._start_task(
                    "ais",
                    self._ais_decoder_task(gain, duration),
                    "Failed to start AIS tracking",
                )
                duration_msg = "until stopped" if duration <= 0 else f"for {duration} seconds"
                return _text(
                    f"Started AIS vessel tracking with {receiver} on 161.975 / 162.025 MHz\n"
                    f"Gain: {gain} dB | Duration: {duration_msg}\n\n"
                    "Use marine_get_vessels to see tracked ships.\n"
                    "NOTE: Python SDR control is paused while tracking. Use marine_stop_tracking to regain it."
                )

            elif name == "marine_stop_tracking":
                if await self._stop_task("ais"):
                    return _text("Stopped AIS tracking")
                return _text("AIS tracking not active")

            elif name == "marine_get_vessels":
                max_age = int(arguments.get("max_age", 600) or 600)
                vessels = self.ais_decoder.get_vessel_list(max_age_seconds=max_age)
                stats = self.ais_decoder.get_statistics(max_age_seconds=max_age)
                active = self._task_running("ais")

                result = f"AIS Vessel Tracker ({'active' if active else 'stopped'})\n"
                result += f"NMEA sentences: {stats['nmea_sentences']} | Decoded messages: {stats['total_messages']}"
                if stats["decode_errors"]:
                    result += f" | Decode errors: {stats['decode_errors']}"
                result += "\n"
                result += f"Vessels seen: {stats['total_vessels']} | Active (last {max_age}s): {stats['active_vessels']}\n\n"

                if not vessels:
                    result += "No vessels tracked yet.\n"
                    if active:
                        result += "AIS range is line-of-sight; you need ships within roughly 20-40 km."
                else:
                    for vessel in vessels:
                        header = f"MMSI {vessel['mmsi']}"
                        if vessel.get("name"):
                            header += f" - {vessel['name']}"
                        if vessel.get("callsign"):
                            header += f" ({vessel['callsign']})"
                        result += header + "\n"
                        details = []
                        if vessel.get("ship_type"):
                            details.append(f"Type: {vessel['ship_type']}")
                        if vessel.get("nav_status"):
                            details.append(f"Status: {vessel['nav_status']}")
                        if vessel.get("destination"):
                            details.append(f"Dest: {vessel['destination']}")
                        if details:
                            result += "  " + " | ".join(details) + "\n"
                        motion = []
                        if vessel.get("latitude") is not None:
                            motion.append(f"Position: {vessel['latitude']:.4f}, {vessel['longitude']:.4f}")
                        if vessel.get("speed") is not None:
                            motion.append(f"Speed: {vessel['speed']:.1f} kts")
                        if vessel.get("course") is not None:
                            motion.append(f"Course: {vessel['course']:.0f}°")
                        if vessel.get("heading") is not None:
                            motion.append(f"Heading: {vessel['heading']:.0f}°")
                        if motion:
                            result += "  " + " | ".join(motion) + "\n"
                        result += f"  Messages: {vessel['message_count']} | Last seen: {vessel['age_seconds']}s ago\n"
                        result += f"  {vessel['tracking_url']}\n\n"

                return _text(result), {"active": active, "statistics": stats, "vessels": vessels}

            # ---------------------------------------------------------- Meteor-M
            elif name == "satellite_decode_meteor":
                if self._task_running("meteor"):
                    return _text(
                        "A Meteor-M decode is already running. Use satellite_get_status to follow it "
                        "or satellite_stop_decoding to end it early."
                    )
                satellite = arguments.get("satellite", "METEOR-M2-4")
                duration = int(arguments.get("duration", 600) or 600)
                gain = arguments.get("gain", 40)

                try:
                    sanitize_path_component(satellite)  # prevents path traversal
                except ValueError as e:
                    raise SDRError(str(e))
                sat_info = self.meteor_decoder.get_satellite_info(satellite)
                if not sat_info:
                    raise SDRError(
                        f"Unknown satellite: {satellite}. Active: "
                        f"{', '.join(self.meteor_decoder.get_active_satellites())}"
                    )
                if not (30 <= duration <= MAX_METEOR_DURATION):
                    raise SDRError(f"Duration must be between 30 and {MAX_METEOR_DURATION} seconds")
                find_binary("satdump", SATDUMP_HINT)  # fail fast with an install hint
                self._require_device_free()

                freq = sat_info["frequency"]
                output_dir = os.path.join(
                    recordings_dir(), f"meteor_{satellite}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                )
                os.makedirs(output_dir, exist_ok=True)
                self.meteor_state = {
                    "status": "starting",
                    "satellite": satellite,
                    "frequency": freq,
                    "duration": duration,
                    "gain": gain,
                    "output_dir": output_dir,
                    "images": [],
                }
                await self._start_task(
                    "meteor",
                    self._meteor_decoder_task(satellite, freq, duration, gain, output_dir),
                    "Failed to start SatDump",
                )

                result = f"Started Meteor-M LRPT decode of {satellite} with SatDump\n"
                result += f"Frequency: {freq/1e6:.3f} MHz | Duration: {duration} s | Gain: {gain} dB\n"
                result += f"Output directory: {output_dir}/\n\n"
                if sat_info["status"] != "active":
                    result += f"Warning: {satellite} status is '{sat_info['status']}'; decoding may fail.\n\n"
                result += "SatDump is now tuning the RTL-SDR, demodulating OQPSK, decoding LRPT frames and\n"
                result += "writing channel images. This runs in the background for the full duration.\n"
                result += "Use satellite_get_status to check progress and results.\n"
                result += "NOTE: Python SDR control is paused until the decode finishes or satellite_stop_decoding is called."
                return _text(result)

            elif name == "satellite_get_status":
                state = dict(self.meteor_state)
                running = self._task_running("meteor")
                state["running"] = running
                if running and state.get("started"):
                    try:
                        elapsed = (datetime.now() - datetime.fromisoformat(state["started"])).total_seconds()
                        state["elapsed_seconds"] = int(elapsed)
                        state["remaining_seconds"] = max(0, int(state.get("duration", 0) - elapsed))
                    except Exception:
                        pass
                result = f"Meteor-M decode status: {state.get('status', 'idle')}\n"
                if state.get("satellite"):
                    result += f"Satellite: {state['satellite']} @ {state['frequency']/1e6:.3f} MHz\n"
                if "elapsed_seconds" in state:
                    result += f"Elapsed: {state['elapsed_seconds']} s | Remaining: ~{state['remaining_seconds']} s\n"
                if state.get("output_dir"):
                    result += f"Output directory: {state['output_dir']}/\n"
                if state.get("images"):
                    result += f"\nImages ({len(state['images'])}):\n"
                    for img in state["images"]:
                        result += f"  - {os.path.basename(img)}\n"
                elif state.get("status") in ("completed", "failed", "stopped"):
                    result += "\nNo images were produced. This usually means the satellite was below the horizon,\n"
                    result += "the signal was too weak (check antenna and gain), or the frequency was wrong.\n"
                    result += "Use satellite_predict_passes to find the next good pass.\n"
                if state.get("error"):
                    result += f"\nError: {state['error']}\n"
                if state.get("log_tail"):
                    result += "\nSatDump output (tail):\n  " + "\n  ".join(state["log_tail"]) + "\n"
                stats = self.meteor_decoder.get_statistics()
                result += f"\nPasses this session: {stats['total_passes']} ({stats['successful_passes']} successful)"
                state["session"] = stats
                return _text(result), _json_safe(state)

            elif name == "satellite_stop_decoding":
                if await self._stop_task("meteor"):
                    return _text(
                        "Stopped Meteor-M decode. Any images produced so far are listed by satellite_get_status."
                    )
                return _text("No Meteor-M decode is running")

            elif name == "satellite_predict_passes":
                satellite = arguments.get("satellite", "METEOR-M2-4")
                if satellite not in NORAD_IDS:
                    raise SDRError(f"Unknown satellite {satellite}. Known: {', '.join(NORAD_IDS)}")
                lat, lon = self._receiver_location(arguments)
                if lat is None:
                    raise SDRError(
                        "Provide latitude and longitude in decimal degrees, or set the "
                        f"{LOCATION_ENV[0]} and {LOCATION_ENV[1]} environment variables."
                    )
                altitude_m = float(arguments.get("altitude_m", 0) or 0)
                hours = float(arguments.get("hours", 24) or 24)
                min_elevation = float(arguments.get("min_elevation", 20) or 0)
                if not SGP4_AVAILABLE:
                    raise SDRError("The sgp4 package is not installed. Install with: pip install sgp4")

                try:
                    passes, info = await asyncio.to_thread(
                        self.pass_predictor.predict, satellite, lat, lon, altitude_m, hours, min_elevation
                    )
                except (ValueError, RuntimeError) as e:
                    raise SDRError(str(e))

                sat_info = self.meteor_decoder.get_satellite_info(satellite) or {}
                result = (
                    f"{satellite} passes over {lat:.4f}, {lon:.4f} in the next {hours:g} h "
                    f"(peak elevation >= {min_elevation:g}°)\n"
                )
                result += f"Orbital elements: {info['tle_name']} via {info['tle_source']}, {info['tle_age_hours']} h old\n"
                if sat_info.get("frequency"):
                    result += f"Downlink: {sat_info['frequency']/1e6:.3f} MHz LRPT\n"
                result += "\n"
                pass_dicts = [p.to_dict() for p in passes]
                if not passes:
                    result += (
                        f"No pass reaches {min_elevation:g}° in this window ({info['all_passes']} lower passes). "
                        "Try a longer window or a lower min_elevation.\n"
                    )
                else:
                    for i, d in enumerate(pass_dicts, 1):
                        los_time = d["los_local"].split(" ")[1]
                        peak_time = d["max_elevation_local"].split(" ")[1]
                        result += (
                            f"{i}. {d['aos_local']}  ->  {los_time}   "
                            f"{d['duration_s']//60} min {d['duration_s']%60:02d} s\n"
                            f"   peak {d['max_elevation_deg']:.0f}° at {peak_time} (az {d['max_azimuth_deg']}°), "
                            f"{d['direction']}, rises az {d['aos_azimuth_deg']}°, sets az {d['los_azimuth_deg']}°\n"
                        )
                    best = max(passes, key=lambda p: p.max_elevation_deg)
                    best_d = best.to_dict()
                    result += (
                        f"\nBest pass: {best_d['aos_local']} peaking at {best.max_elevation_deg:.0f}°. "
                        f"Start satellite_decode_meteor about a minute before it rises with "
                        f"duration={int(best.duration_s) + 120}.\n"
                        "Passes above 30° usually give good images; a 137 MHz V-dipole works well."
                    )
                return _text(result), {
                    "satellite": satellite,
                    "observer": {"latitude": lat, "longitude": lon, "altitude_m": altitude_m},
                    "elements": info,
                    "passes": pass_dicts,
                }

            # ---------------------------------------------------------- analysis
            elif name == "spectrum_analyze":
                sdr = self._require_sdr()
                self._require_capture_idle()

                fft_size = int(arguments.get("fft_size", 2048))
                window = arguments.get("window", "blackman-harris")
                averaging = bool(arguments.get("averaging", True))

                if fft_size <= 0 or (fft_size & (fft_size - 1)) != 0:
                    raise SDRError(f"FFT size must be a power of 2, got {fft_size}")
                if fft_size > 65536:
                    raise SDRError("FFT size too large (max 65536)")
                if window not in VALID_WINDOWS:
                    raise SDRError(f"Unknown window function: {window}. Valid: {', '.join(sorted(VALID_WINDOWS))}")

                self.spectrum_analyzer.fft_size = fft_size
                self.spectrum_analyzer.window_type = window
                self.spectrum_analyzer.window = self.spectrum_analyzer._get_window(window, fft_size)
                if not averaging:
                    self.spectrum_analyzer.reset_averaging()

                samples = await sdr.read_samples(fft_size * 2)
                frame = await self.spectrum_analyzer.analyze_spectrum(
                    samples[:fft_size], sdr.sample_rate, sdr.frequency
                )

                result = f"Spectrum Analysis at {frame.center_freq/1e6:.3f} MHz\n"
                result += f"Span: {sdr.sample_rate/1e6:.3f} MHz | FFT: {fft_size} | Window: {window}\n"
                result += f"Peak power: {frame.peak_power:.1f} dB\n"
                result += f"Noise floor: {frame.noise_floor:.1f} dB\n"
                result += f"Dynamic range: {frame.peak_power - frame.noise_floor:.1f} dB\n"

                signals = []
                if frame.detected_signals:
                    result += f"\nDetected {len(frame.detected_signals)} signals:\n"
                    for sig in frame.detected_signals:
                        result += f"  {sig.frequency/1e6:.3f} MHz: "
                        result += f"{sig.power:.1f} dB, "
                        result += f"BW: {sig.bandwidth/1e3:.1f} kHz, "
                        result += f"SNR: {sig.snr:.1f} dB"
                        if sig.modulation_hint:
                            result += f" [{sig.modulation_hint}]"
                        result += f" (confidence: {sig.confidence*100:.0f}%)\n"
                        signals.append({
                            "frequency_hz": float(sig.frequency),
                            "power_db": round(float(sig.power), 1),
                            "bandwidth_hz": round(float(sig.bandwidth)),
                            "snr_db": round(float(sig.snr), 1),
                            "hint": sig.modulation_hint,
                            "confidence": round(float(sig.confidence), 2),
                        })
                else:
                    result += "\nNo signals detected above threshold"

                return _text(result), {
                    "center_freq_hz": float(frame.center_freq),
                    "sample_rate_hz": float(sdr.sample_rate),
                    "fft_size": fft_size,
                    "window": window,
                    "peak_power_db": round(float(frame.peak_power), 1),
                    "noise_floor_db": round(float(frame.noise_floor), 1),
                    "dynamic_range_db": round(float(frame.peak_power - frame.noise_floor), 1),
                    "signals": signals,
                }

            elif name == "spectrum_scan":
                sdr = self._require_sdr()
                self._require_capture_idle()

                start_freq = float(arguments["start_freq"])
                stop_freq = float(arguments["stop_freq"])
                step_arg = arguments.get("step")
                step = 1e6 if step_arg is None else float(step_arg)
                dwell_arg = arguments.get("dwell_time")
                dwell_time = 0.1 if dwell_arg is None else float(dwell_arg)

                try:
                    await self.frequency_scanner.scan_range(sdr, start_freq, stop_freq, step, dwell_time)
                except ValueError as e:
                    raise SDRError(str(e))

                summary = self.frequency_scanner.get_activity_summary()
                result = f"Scanned {start_freq/1e6:.3f} - {stop_freq/1e6:.3f} MHz in {step/1e6:.3f} MHz steps\n"
                result += f"- Frequencies with signals: {summary.get('scan_points', 0)}\n"
                result += f"- Signals found: {summary.get('total_signals', 0)}\n"

                if summary.get('signal_types'):
                    result += "\nSignal types detected:\n"
                    for sig_type, count in summary['signal_types'].items():
                        result += f"  - {sig_type}: {count}\n"

                if summary.get('strongest_signal'):
                    sig = summary['strongest_signal']
                    result += "\nStrongest signal:\n"
                    result += f"  {sig['frequency']/1e6:.3f} MHz @ {sig['power']:.1f} dB"
                    if sig.get('type'):
                        result += f" [{sig['type']}]"

                return _text(result), {
                    "start_hz": start_freq,
                    "stop_hz": stop_freq,
                    "step_hz": step,
                    "dwell_s": dwell_time,
                    "summary": _json_safe(summary),
                    "results": _json_safe(self.frequency_scanner.scan_results),
                }

            # ---------------------------------------------------------- recording
            elif name == "recording_start":
                sdr = self._require_sdr()
                if self._task_running("recorder") or self.signal_recorder.is_recording:
                    raise SDRError(
                        f"An IQ recording is already in progress ({self.signal_recorder.recording_metadata.get('id')}). "
                        "Stop it with recording_stop first."
                    )
                self._require_capture_idle()

                description = arguments.get("description", "")
                recording_id = await self.signal_recorder.start_recording(
                    sdr.frequency, sdr.sample_rate, sdr.gain, description
                )
                self.active_decoders["recorder"] = asyncio.create_task(self._recording_task())
                return _text(
                    f"Started IQ recording: {recording_id}\n"
                    f"File: {self.signal_recorder.recording_metadata['file']}\n"
                    f"Format: complex float32 interleaved I/Q at {sdr.sample_rate/1e6:.3f} Msps"
                )

            elif name == "recording_stop":
                was_running = await self._stop_task("recorder")
                if not was_running and not self.signal_recorder.is_recording:
                    return _text("No recording in progress")
                metadata = await self.signal_recorder.stop_recording()
                result = "Recording stopped:\n"
                result += f"- ID: {metadata.get('id', 'N/A')}\n"
                result += f"- Duration: {metadata.get('duration', 0):.1f} seconds\n"
                result += f"- Samples: {metadata.get('samples_recorded', 0):,}\n"
                result += f"- File: {metadata.get('file', 'N/A')}"
                if not was_running:
                    result += "\n(The capture task had already ended; the file was finalized.)"
                return _text(result)

            elif name == "audio_record_start":
                sdr = self._require_sdr()
                if self._task_running("audio_recorder") or self.audio_recorder.is_recording:
                    raise SDRError(
                        f"An audio recording is already in progress ({self.audio_recorder.recording_metadata.get('id')}). "
                        "Stop it with audio_record_stop first."
                    )
                self._require_capture_idle()

                modulation = str(arguments.get("modulation", "FM")).upper()
                if modulation not in ("FM", "AM"):
                    raise SDRError("modulation must be FM or AM")
                description = arguments.get("description", "")

                recording_id = await self.audio_recorder.start_recording(
                    sdr.frequency, sdr.sample_rate, modulation, description
                )
                self.active_decoders["audio_recorder"] = asyncio.create_task(
                    self._audio_recording_task(modulation)
                )
                return _text(
                    f"Started audio recording ({modulation}): {recording_id}\n"
                    f"File: {self.audio_recorder.recording_metadata['file']}\n"
                    f"Audio rate: {self.audio_recorder.output_rate} Hz mono 16-bit"
                )

            elif name == "audio_record_stop":
                was_running = await self._stop_task("audio_recorder")
                if not was_running and not self.audio_recorder.is_recording:
                    return _text("No audio recording in progress")
                metadata = await self.audio_recorder.stop_recording()
                result = "Audio recording stopped:\n"
                result += f"- ID: {metadata.get('id', 'N/A')}\n"
                result += f"- Duration: {metadata.get('duration', 0):.1f} seconds\n"
                result += f"- Audio captured: {metadata.get('audio_seconds', 0):.1f} seconds\n"
                result += f"- Modulation: {metadata.get('modulation', 'N/A')}\n"
                result += f"- File: {metadata.get('file', 'N/A')}"
                if not was_running:
                    result += "\n(The capture task had already ended; the file was finalized.)"
                return _text(result)

            # ---------------------------------------------------------- HackRF TX
            elif name == "hackrf_set_tx_gain":
                sdr = self._require_sdr()
                if not isinstance(sdr, HackRFDevice):
                    raise SDRError("This command requires a HackRF device")
                gain = int(arguments["gain"])
                try:
                    await sdr.set_tx_gain(gain)
                except ValueError as e:
                    raise SDRError(str(e))
                return _text(f"Set HackRF TX gain to {gain} dB")

            elif name == "signal_generator":
                sdr = self._require_sdr()
                if not isinstance(sdr, HackRFDevice):
                    raise SDRError("Signal generation requires a HackRF device")
                self._require_capture_idle()

                frequency = float(arguments["frequency"])
                signal_type = arguments["signal_type"]
                duration = float(arguments.get("duration", 1.0) or 1.0)
                tone_freq = float(arguments.get("tone_freq", 1000) or 1000)

                if not (0 < duration <= MAX_TX_DURATION):
                    raise SDRError(f"Duration must be between 0 and {MAX_TX_DURATION:.0f} seconds")
                if not sdr.validate_tx_safety(frequency):
                    raise SDRError(
                        f"Cannot transmit on {frequency/1e6:.3f} MHz: it is below 10 MHz or inside a "
                        "protected band (aviation, public safety, cellular, GNSS, satellite)."
                    )
                if abs(tone_freq) >= sdr.sample_rate / 2:
                    raise SDRError("tone_freq must be below half the sample rate")

                num_samples = int(sdr.sample_rate * duration)
                t = np.arange(num_samples) / sdr.sample_rate

                if signal_type == "cw":
                    waveform = np.ones(num_samples, dtype=complex)
                elif signal_type == "tone":
                    waveform = np.exp(2j * np.pi * tone_freq * t)
                elif signal_type == "noise":
                    waveform = (np.random.randn(num_samples) + 1j * np.random.randn(num_samples)) / np.sqrt(2)
                elif signal_type == "sweep":
                    sweep_rate = sdr.sample_rate / 4 / duration
                    waveform = np.exp(1j * np.pi * sweep_rate * t ** 2)
                else:
                    raise SDRError(f"Unknown signal type: {signal_type}")

                try:
                    await sdr.set_frequency(frequency)
                    await sdr.transmit(waveform * 0.8)  # scale for headroom; returns when done
                except (ValueError, RuntimeError) as e:
                    raise SDRError(f"Transmit failed: {e}")

                return _text(
                    f"Transmitted {signal_type} signal at {frequency/1e6:.3f} MHz for {duration} seconds "
                    f"(TX gain {sdr.tx_vga_gain} dB)"
                )

            # ---------------------------------------------------------- rtl_433
            elif name == "ism_start_scanning":
                if self._task_running("rtl433"):
                    return _text("ISM scanning already active")
                self._require_device_free()

                freq_list = arguments.get("frequencies", [433.92e6, 315e6])
                hop_arg = arguments.get("hop_interval")
                hop_interval = 30 if hop_arg is None else int(hop_arg)

                if not freq_list:
                    raise SDRError("Provide at least one frequency")
                if len(freq_list) > 10:
                    raise SDRError("Maximum 10 frequencies allowed")
                if not (1 <= hop_interval <= 3600):
                    raise SDRError("Hop interval must be 1-3600 seconds")

                frequencies = []
                for f in freq_list:
                    f_hz = float(f)
                    if f_hz < 10e3:  # tolerate MHz values from older clients
                        f_hz *= 1e6
                    if not (RTLSDR_MIN_FREQ <= f_hz <= RTLSDR_MAX_FREQ):
                        raise SDRError(f"Frequency {f_hz/1e6:.3f} MHz out of RTL-SDR range (24-1766 MHz)")
                    frequencies.append(f_hz)

                self.rtl433_decoder.set_frequencies(frequencies)
                self.rtl433_decoder.set_hop_interval(hop_interval)
                find_binary("rtl_433", RTL433_HINT)  # fail fast with an install hint

                await self._start_task("rtl433", self._rtl433_decoder_task(), "Failed to start rtl_433")

                freq_str = ", ".join(f"{f/1e6:.3f} MHz" for f in frequencies)
                return _text(
                    "Started ISM band scanning with rtl_433\n\n"
                    f"Frequencies: {freq_str}\nHop interval: {hop_interval} seconds\n\n"
                    "NOTE: Python SDR control is paused while scanning.\n"
                    "Use ism_stop_scanning to regain SDR control."
                )

            elif name == "ism_stop_scanning":
                if await self._stop_task("rtl433"):
                    return _text("Stopped ISM scanning")
                return _text("ISM scanning not active")

            elif name == "ism_get_devices":
                max_age = int(arguments.get("max_age", 300) or 300)
                devices = self.rtl433_decoder.get_device_list(max_age_seconds=max_age)
                stats = self.rtl433_decoder.get_statistics()

                result = f"ISM Band Devices ({'scanning' if self._task_running('rtl433') else 'stopped'})\n"
                result += "=" * 50 + "\n\n"
                result += f"Total messages: {stats['total_messages']}\n"
                result += f"Unique devices: {stats['total_devices_seen']}\n"
                result += f"Active devices: {stats['active_devices']}\n"
                result += f"Scanning: {', '.join(f'{f:.3f} MHz' for f in stats['frequencies_MHz'])}\n"
                result += f"Hop interval: {stats['hop_interval_seconds']}s\n\n"

                if stats['device_types']:
                    result += "Device types seen:\n"
                    for device_type, count in stats['device_types'].items():
                        result += f"  • {device_type}: {count}\n"
                    result += "\n"

                if not devices:
                    result += f"No devices detected in the last {max_age} seconds.\n"
                    result += "\nTips:\n"
                    result += "- Make sure devices are transmitting\n"
                    result += "- Weather stations typically transmit every 30-60 seconds\n"
                    result += "- Try waiting longer for more results\n"
                else:
                    result += f"Recently Active Devices ({len(devices)}):\n"
                    result += "-" * 50 + "\n\n"
                    for device in devices:
                        result += self.rtl433_decoder.get_device_summary(device) + "\n"
                        result += f"  Last seen: {device['age_seconds']}s ago\n\n"

                return _text(result), {
                    "scanning": self._task_running("rtl433"),
                    "statistics": _json_safe(stats),
                    "devices": _json_safe(devices),
                }

            else:
                raise SDRError(f"Unknown tool: {name}")

        # Exposed for in-process testing without an MCP transport
        self.call_tool = call_tool

        @self.server.list_resources()
        async def list_resources() -> List[Resource]:
            """List available resources"""
            return [
                Resource(
                    uri="sdr://status",
                    name="SDR Status",
                    mimeType="application/json",
                    description="Current SDR hardware status"
                ),
                Resource(
                    uri="aviation://aircraft",
                    name="Tracked Aircraft",
                    mimeType="application/json",
                    description="Currently tracked aircraft from ADS-B"
                ),
                Resource(
                    uri="spectrum://waterfall",
                    name="Waterfall Data",
                    mimeType="application/json",
                    description="Recent waterfall display data"
                ),
                Resource(
                    uri="scan://results",
                    name="Scan Results",
                    mimeType="application/json",
                    description="Latest frequency scan results"
                )
            ]

        @self.server.read_resource()
        async def read_resource(uri: str) -> str:
            """Read resource content"""
            uri = str(uri)
            if uri == "sdr://status":
                return json.dumps(self._status_dict(), indent=2, default=str)

            elif uri == "aviation://aircraft":
                aircraft_data = {
                    "aircraft": self.adsb_decoder.get_aircraft_list(),
                    "statistics": self.adsb_decoder.get_statistics(),
                    "total_messages": self.adsb_decoder.message_count,
                    "raw_messages": self.adsb_decoder.raw_message_count,
                    "decoder_active": self._task_running("adsb"),
                }
                return json.dumps(aircraft_data, indent=2, default=str)

            elif uri == "spectrum://waterfall":
                waterfall_data = self.spectrum_analyzer.get_waterfall_data(50)
                data = {
                    "lines": waterfall_data.tolist() if len(waterfall_data) > 0 else [],
                    "fft_size": self.spectrum_analyzer.fft_size,
                    "center_freq": self.sdr.frequency if self.sdr else 0,
                    "sample_rate": self.sdr.sample_rate if self.sdr else 0
                }
                return json.dumps(data, indent=2)

            elif uri == "scan://results":
                scan_data = {
                    "results": self.frequency_scanner.scan_results,
                    "summary": self.frequency_scanner.get_activity_summary()
                }
                return json.dumps(scan_data, indent=2, default=str)

            return f"Unknown resource: {uri}"

    # ====================================================================== background tasks

    async def _adsb_decoder_task(
        self,
        gain: str = "40",
        duration: int = 120,
        aggressive: bool = True,
        fix_crc: bool = True,
    ):
        """Background task for ADS-B decoding using dump1090 raw TCP output.

        Releases the Python SDR connection so dump1090 can open the device, and
        reconnects it with the previous settings when done.
        """
        logger.info("Starting ADS-B decoder with dump1090 subprocess")
        await self._release_sdr("adsb")

        process = None
        writer = None
        pump = None
        stderr_tail: Deque[str] = deque(maxlen=30)
        try:
            # Homebrew packages FlightAware dump1090 as dump1090-fa; the binary is dump1090.
            dump1090_path = find_binary("dump1090", DUMP1090_HINT)
            cmd = [dump1090_path, "--net", "--gain", str(gain), "--quiet"]
            device_index = self._device_index()
            if device_index:
                cmd.extend(["--device-index", str(device_index)])
            if fix_crc:
                cmd.append("--fix")
            if aggressive:
                cmd.append("--aggressive")
            logger.info(f"Starting dump1090: {' '.join(cmd)}")

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            pump = asyncio.create_task(self._pump_lines(process.stderr, "dump1090", stderr_tail))
            logger.info(f"dump1090 started (PID: {process.pid})")

            # dump1090 needs a moment before the raw TCP port is ready.
            reader = None
            for _ in range(20):
                if process.returncode is not None:
                    raise RuntimeError(
                        f"dump1090 exited with code {process.returncode}: {self._tail_text(stderr_tail)}"
                    )
                try:
                    reader, writer = await asyncio.open_connection("127.0.0.1", ADSB_RAW_PORT)
                    break
                except OSError:
                    await asyncio.sleep(0.5)
            if reader is None:
                raise RuntimeError(
                    f"Could not connect to dump1090 raw output on 127.0.0.1:{ADSB_RAW_PORT}. "
                    f"{self._tail_text(stderr_tail)}"
                )

            loop = asyncio.get_running_loop()
            msg_count = 0
            decode_count = 0
            last_log_time = loop.time()
            deadline = loop.time() + duration if duration and duration > 0 else None

            while True:
                if deadline and loop.time() >= deadline:
                    logger.info("ADS-B tracking duration reached")
                    break
                if process.returncode is not None:
                    logger.error(f"dump1090 exited with code {process.returncode}")
                    break

                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=2.0)
                except asyncio.TimeoutError:
                    continue
                if not line:
                    await asyncio.sleep(0.2)
                    continue

                msg_line = line.decode(errors="replace").strip()
                if msg_line.startswith('*') and msg_line.endswith(';'):
                    msg_count += 1
                    msg_hex = msg_line[1:-1]
                    if len(msg_hex) == 28 and self.adsb_decoder.decode_message(msg_hex):
                        decode_count += 1

                if loop.time() - last_log_time > 10:
                    logger.info(
                        f"ADS-B: {msg_count} msgs, {decode_count} decoded, "
                        f"{len(self.adsb_decoder.aircraft)} aircraft"
                    )
                    last_log_time = loop.time()

        except asyncio.CancelledError:
            logger.info("ADS-B tracking stopped by user")
            raise
        except Exception as e:
            logger.error(f"ADS-B decoder error: {e}")
            logger.debug(traceback.format_exc())
            raise
        finally:
            if pump:
                pump.cancel()
            if writer:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
            await self._terminate(process, "dump1090")
            await self._restore_sdr()

    async def _pocsag_decoder_task(self, frequency: float, gain: Any, duration: int,
                                   baud_rate: Optional[int]):
        """Background task for POCSAG decoding: rtl_fm | multimon-ng.

        Releases the Python SDR connection so rtl_fm can open the device, and
        reconnects it with the previous settings when done.
        """
        logger.info("Starting POCSAG decoder (rtl_fm | multimon-ng)")
        await self._release_sdr("pocsag")

        rtl_fm_proc = None
        multimon_proc = None
        read_fd = write_fd = None
        pumps: List[asyncio.Task] = []
        rtl_tail: Deque[str] = deque(maxlen=30)
        mm_tail: Deque[str] = deque(maxlen=30)
        try:
            rtl_fm_path = find_binary("rtl_fm", RTL_FM_HINT)
            multimon_path = find_binary("multimon-ng", MULTIMON_HINT)

            bauds = [baud_rate] if baud_rate else list(POCSAG_BAUD_RATES)
            rtl_cmd = [
                rtl_fm_path,
                "-f", str(int(frequency)),
                "-M", "fm",
                "-s", "22050",
                "-g", str(gain),
                "-d", str(self._device_index()),
                "-",
            ]
            mm_cmd = [multimon_path, "-t", "raw"]
            for baud in bauds:
                mm_cmd.extend(["-a", f"POCSAG{baud}"])
            mm_cmd.append("-")
            logger.info(f"Starting: {' '.join(rtl_cmd)} | {' '.join(mm_cmd)}")

            read_fd, write_fd = os.pipe()
            rtl_fm_proc = await asyncio.create_subprocess_exec(
                *rtl_cmd, stdout=write_fd, stderr=asyncio.subprocess.PIPE
            )
            os.close(write_fd)
            write_fd = None
            multimon_proc = await asyncio.create_subprocess_exec(
                *mm_cmd, stdin=read_fd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            os.close(read_fd)
            read_fd = None

            pumps = [
                asyncio.create_task(self._pump_lines(rtl_fm_proc.stderr, "rtl_fm", rtl_tail)),
                asyncio.create_task(self._pump_lines(multimon_proc.stderr, "multimon-ng", mm_tail)),
            ]

            # Give rtl_fm a moment to claim the device and fail fast if it cannot.
            await asyncio.sleep(1.5)
            if rtl_fm_proc.returncode is not None:
                raise RuntimeError(
                    f"rtl_fm exited with code {rtl_fm_proc.returncode}: {self._tail_text(rtl_tail)}"
                )
            if multimon_proc.returncode is not None:
                raise RuntimeError(
                    f"multimon-ng exited with code {multimon_proc.returncode}: {self._tail_text(mm_tail)}"
                )
            self.pocsag_decoder.frequency = frequency
            logger.info(f"POCSAG pipeline running (rtl_fm PID {rtl_fm_proc.pid}, multimon-ng PID {multimon_proc.pid})")

            loop = asyncio.get_running_loop()
            deadline = loop.time() + duration if duration and duration > 0 else None
            last_log_time = loop.time()
            start_count = self.pocsag_decoder.message_count

            while True:
                if deadline and loop.time() >= deadline:
                    logger.info("POCSAG decoding duration reached")
                    break
                if rtl_fm_proc.returncode is not None:
                    logger.error(f"rtl_fm exited with code {rtl_fm_proc.returncode}: {self._tail_text(rtl_tail)}")
                    break
                if multimon_proc.returncode is not None:
                    logger.error(f"multimon-ng exited with code {multimon_proc.returncode}")
                    break

                try:
                    line = await asyncio.wait_for(multimon_proc.stdout.readline(), timeout=2.0)
                except asyncio.TimeoutError:
                    continue
                if not line:
                    break

                text = line.decode(errors="replace").strip()
                if text:
                    message = self.pocsag_decoder.parse_multimon_line(text)
                    if message:
                        logger.info(
                            f"POCSAG{message.bitrate} addr {message.address} fn {message.function}: "
                            f"{message.message[:80]}"
                        )

                if loop.time() - last_log_time > 30:
                    logger.info(f"POCSAG: {self.pocsag_decoder.message_count - start_count} messages this run")
                    last_log_time = loop.time()

        except asyncio.CancelledError:
            logger.info("POCSAG decoding stopped by user")
            raise
        except Exception as e:
            logger.error(f"POCSAG decoder error: {e}")
            logger.debug(traceback.format_exc())
            raise
        finally:
            for pump in pumps:
                pump.cancel()
            for fd in (read_fd, write_fd):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            await self._terminate(multimon_proc, "multimon-ng")
            await self._terminate(rtl_fm_proc, "rtl_fm")
            await self._restore_sdr()

    def _build_ais_command(self, gain: Any):
        """Pick an installed AIS receiver and build its command line."""
        device_index = self._device_index()
        ais_catcher = shutil.which("AIS-catcher")
        if ais_catcher:
            cmd = [ais_catcher, f"-d:{device_index}", "-o", "1"]
            if gain is not None and str(gain).lower() != "auto":
                cmd.extend(["-gr", "TUNER", str(gain), "RTLAGC", "off"])
            return "AIS-catcher", cmd
        rtl_ais = shutil.which("rtl_ais")
        if rtl_ais:
            cmd = [rtl_ais, "-n", "-d", str(device_index)]
            if gain is not None and str(gain).lower() != "auto":
                cmd.extend(["-g", str(gain)])
            return "rtl_ais", cmd
        raise SDRError(f"No AIS receiver found on PATH. {AIS_HINT}")

    async def _ais_decoder_task(self, gain: Any, duration: int):
        """Background task for AIS vessel tracking via AIS-catcher or rtl_ais.

        Releases the Python SDR connection so the receiver can open the device,
        and reconnects it with the previous settings when done.
        """
        logger.info("Starting AIS decoder")
        await self._release_sdr("ais")

        process = None
        pumps: List[asyncio.Task] = []
        tail: Deque[str] = deque(maxlen=40)
        try:
            receiver, cmd = self._build_ais_command(gain)
            logger.info(f"Starting {receiver}: {' '.join(cmd)}")
            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )

            def handle(text: str) -> bool:
                if "!AIVD" in text:
                    self.ais_decoder.feed_line(text)
                    return True
                return False

            # rtl_ais logs NMEA to stderr with -n; AIS-catcher uses stdout. Watch both.
            pumps = [
                asyncio.create_task(self._pump_lines(process.stdout, receiver, tail, handle)),
                asyncio.create_task(self._pump_lines(process.stderr, receiver, tail, handle)),
            ]

            await asyncio.sleep(1.5)
            if process.returncode is not None:
                raise RuntimeError(f"{receiver} exited with code {process.returncode}: {self._tail_text(tail)}")
            logger.info(f"{receiver} running (PID {process.pid})")

            loop = asyncio.get_running_loop()
            deadline = loop.time() + duration if duration and duration > 0 else None
            last_log_time = loop.time()
            while True:
                if deadline and loop.time() >= deadline:
                    logger.info("AIS tracking duration reached")
                    break
                if process.returncode is not None:
                    logger.error(f"{receiver} exited with code {process.returncode}: {self._tail_text(tail)}")
                    break
                await asyncio.sleep(1.0)
                if loop.time() - last_log_time > 30:
                    stats = self.ais_decoder.get_statistics()
                    logger.info(
                        f"AIS: {stats['nmea_sentences']} sentences, {stats['total_messages']} decoded, "
                        f"{stats['total_vessels']} vessels"
                    )
                    last_log_time = loop.time()

        except asyncio.CancelledError:
            logger.info("AIS tracking stopped by user")
            raise
        except Exception as e:
            logger.error(f"AIS decoder error: {e}")
            logger.debug(traceback.format_exc())
            raise
        finally:
            for pump in pumps:
                pump.cancel()
            await self._terminate(process, "AIS receiver")
            await self._restore_sdr()

    async def _meteor_decoder_task(self, satellite: str, frequency: float, duration: int,
                                   gain: Any, output_dir: str):
        """Background task running SatDump for a Meteor-M LRPT pass.

        Releases the Python SDR connection so SatDump can open the device, and
        reconnects it with the previous settings when done.
        """
        logger.info(f"Starting SatDump for {satellite}")
        await self._release_sdr("meteor")

        state = self.meteor_state
        process = None
        pump = None
        tail: Deque[str] = deque(maxlen=60)
        try:
            satdump_path = find_binary("satdump", SATDUMP_HINT)
            cmd = self.meteor_decoder.build_satdump_command(
                satellite, frequency, output_dir, duration, gain,
                satdump_path=satdump_path, device_index=self._device_index(),
            )
            state.update(command=" ".join(cmd), status="running", started=datetime.now().isoformat())
            logger.info(f"Command: {' '.join(cmd)}")

            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
            pump = asyncio.create_task(self._pump_lines(process.stdout, "satdump", tail))

            await asyncio.sleep(1.5)
            if process.returncode is not None and process.returncode != 0:
                raise RuntimeError(f"satdump exited with code {process.returncode}: {self._tail_text(tail)}")

            await process.wait()  # SatDump stops itself when --timeout elapses
            state["return_code"] = process.returncode
            parsed = self.meteor_decoder.parse_satdump_output(output_dir)
            state["images"] = parsed["images"]
            state["channels"] = parsed["channels"]
            success = process.returncode == 0 and bool(parsed["images"])
            state["status"] = "completed" if success else "failed"
            if process.returncode != 0:
                state["error"] = f"SatDump exited with code {process.returncode}"
            elif not parsed["images"]:
                state["error"] = "SatDump finished but produced no images"
            self.meteor_decoder.add_pass(MeteorPass(
                satellite=satellite,
                frequency=frequency,
                start_time=datetime.fromisoformat(state["started"]),
                duration=duration,
                output_dir=output_dir,
                decoded_images=parsed["images"],
                success=success,
                channels_received=parsed["channels"],
                error_message=state.get("error"),
            ))
            logger.info(f"SatDump finished: {state['status']} ({len(parsed['images'])} images)")

        except asyncio.CancelledError:
            logger.info("Meteor-M decode stopped by user")
            state["status"] = "stopped"
            try:
                parsed = self.meteor_decoder.parse_satdump_output(output_dir)
                state["images"] = parsed["images"]
            except Exception:
                pass
            raise
        except Exception as e:
            logger.error(f"Meteor decoder error: {e}")
            logger.debug(traceback.format_exc())
            state["status"] = "failed"
            state["error"] = str(e)
            raise
        finally:
            state["finished"] = datetime.now().isoformat()
            state["log_tail"] = list(tail)[-15:]
            if pump:
                pump.cancel()
            await self._terminate(process, "satdump")
            await self._restore_sdr()

    async def _recording_task(self):
        """Background task for recording IQ samples (gap-free device streaming)"""
        logger.info("Starting recording task")
        sdr = self.sdr
        try:
            chunk_size = int(sdr.sample_rate * 0.1)  # 100 ms chunks
            async for samples in sdr.stream(chunk_size):
                await self.signal_recorder.add_samples(samples)
        except asyncio.CancelledError:
            logger.info("Recording task cancelled")
            raise
        except Exception as e:
            logger.error(f"Recording task error: {e}")
            raise

    async def _audio_recording_task(self, modulation: str = "FM"):
        """Background task for recording demodulated audio (gap-free device streaming)"""
        logger.info(f"Starting audio recording task ({modulation})")
        sdr = self.sdr
        try:
            chunk_size = int(sdr.sample_rate * 0.1)  # 100 ms chunks
            async for samples in sdr.stream(chunk_size):
                await self.audio_recorder.add_samples(samples, sdr.sample_rate, modulation)
        except asyncio.CancelledError:
            logger.info("Audio recording task cancelled")
            raise
        except Exception as e:
            logger.error(f"Audio recording task error: {e}")
            raise

    async def _rtl433_decoder_task(self):
        """Background task for ISM band decoding using an rtl_433 subprocess.

        Releases the Python SDR connection so rtl_433 can open the device, and
        reconnects it with the previous settings when done.
        """
        logger.info("Starting rtl_433 decoder with subprocess")
        await self._release_sdr("rtl433")

        process = None
        pump = None
        stderr_tail: Deque[str] = deque(maxlen=30)
        try:
            rtl_433_path = find_binary("rtl_433", RTL433_HINT)
            cmd = [rtl_433_path, "-F", "json", "-d", str(self._device_index())]
            for freq in self.rtl433_decoder.frequencies:
                cmd.extend(["-f", f"{int(freq)}"])
            if len(self.rtl433_decoder.frequencies) > 1:
                cmd.extend(["-H", str(self.rtl433_decoder.hop_interval)])
            logger.info(f"Starting rtl_433: {' '.join(cmd)}")

            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            pump = asyncio.create_task(self._pump_lines(process.stderr, "rtl_433", stderr_tail))
            logger.info(f"rtl_433 started (PID: {process.pid})")

            await asyncio.sleep(1.5)
            if process.returncode is not None:
                raise RuntimeError(f"rtl_433 exited with code {process.returncode}: {self._tail_text(stderr_tail)}")

            loop = asyncio.get_running_loop()
            msg_count = 0
            last_log_time = loop.time()

            while True:
                line = await process.stdout.readline()
                if not line:
                    if process.returncode is not None:
                        logger.error(f"rtl_433 exited with code {process.returncode}: {self._tail_text(stderr_tail)}")
                    else:
                        logger.error("rtl_433 output ended")
                    break

                json_line = line.decode(errors="replace").strip()
                if json_line:
                    device = self.rtl433_decoder.parse_message(json_line)
                    if device:
                        msg_count += 1
                        logger.debug(f"Decoded {device.model}: {self.rtl433_decoder.get_device_summary(device)}")

                if loop.time() - last_log_time > 30:
                    stats = self.rtl433_decoder.get_statistics()
                    logger.info(
                        f"RTL_433: {msg_count} msgs, {stats['total_devices_seen']} devices, "
                        f"{stats['active_devices']} active"
                    )
                    last_log_time = loop.time()

        except asyncio.CancelledError:
            logger.info("RTL_433 scanning stopped by user")
            raise
        except Exception as e:
            logger.error(f"RTL_433 decoder error: {e}")
            logger.debug(traceback.format_exc())
            raise
        finally:
            if pump:
                pump.cancel()
            await self._terminate(process, "rtl_433")
            await self._restore_sdr()

    # ====================================================================== transport

    async def run(self):
        """Run the MCP server"""
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await self.server.run(
                read_stream,
                write_stream,
                self.server.create_initialization_options()
            )


async def main():
    """Async entry point"""
    server = SDRMCPServer()
    await server.run()


def setup_claude_desktop():
    """Configure Claude Desktop to use AetherLink MCP server."""
    import sys
    import platform

    system = platform.system()
    home = os.path.expanduser("~")

    if system == "Darwin":
        config_dir = os.path.join(home, "Library", "Application Support", "Claude")
    elif system == "Windows":
        config_dir = os.path.join(os.environ.get("APPDATA", home), "Claude")
    else:
        config_dir = os.path.join(home, ".config", "Claude")

    config_file = os.path.join(config_dir, "claude_desktop_config.json")

    # Prefer the entry point of the install that is running this command, so Claude
    # Desktop uses the same version. Fall back to uvx, then to python -m.
    aetherlink_path = shutil.which("aetherlink")
    uvx_path = shutil.which("uvx")
    if aetherlink_path:
        server_entry = {"command": aetherlink_path, "args": []}
    elif uvx_path:
        server_entry = {"command": uvx_path, "args": ["aetherlink"]}
    else:
        server_entry = {"command": sys.executable, "args": ["-m", "sdr_mcp"]}

    mcp_config = {
        "command": server_entry["command"],
        "args": server_entry["args"],
    }

    # Load existing config -- preserve everything, only add/update aetherlink
    config = {}
    if os.path.exists(config_file):
        with open(config_file, "r") as f:
            try:
                config = json.load(f)
            except json.JSONDecodeError:
                print(f"Warning: {config_file} has invalid JSON, creating backup...")
                backup = config_file + ".bak"
                shutil.copy2(config_file, backup)
                print(f"  Backup saved to {backup}")

    if "mcpServers" not in config:
        config["mcpServers"] = {}

    if "aetherlink" in config["mcpServers"]:
        print(f"AetherLink already configured in {config_file}")
        print(f"  command: {config['mcpServers']['aetherlink'].get('command')}")
        print()
        try:
            answer = input("Update AetherLink entry? (other servers untouched) [y/N] ").strip().lower()
        except EOFError:
            answer = "n"
        if answer != "y":
            print("No changes made.")
            return

    other_servers = [k for k in config["mcpServers"] if k != "aetherlink"]
    if other_servers:
        print(f"Existing MCP servers (untouched): {', '.join(other_servers)}")

    config["mcpServers"]["aetherlink"] = mcp_config

    os.makedirs(config_dir, exist_ok=True)
    with open(config_file, "w") as f:
        json.dump(config, f, indent=2)

    print(f"Added AetherLink to {config_file}")
    print()
    print(f"  command: {mcp_config['command']}")
    if mcp_config["args"]:
        print(f"  args:    {mcp_config['args']}")
    print()
    print("Restart Claude Desktop to activate AetherLink.")

    # Check system deps
    print()
    print("System dependencies:")
    checks = [
        (["rtl_test"], "RTL-SDR drivers", "brew install librtlsdr / sudo apt install rtl-sdr"),
        (["dump1090"], "ADS-B decoder (dump1090)", "brew install dump1090-fa"),
        (["rtl_fm", "multimon-ng"], "POCSAG pager decoder (rtl_fm + multimon-ng)", "see README; install.sh builds multimon-ng"),
        (["AIS-catcher", "rtl_ais"], "AIS receiver (AIS-catcher or rtl_ais)", "see README"),
        (["rtl_433"], "ISM band decoder (rtl_433)", "brew install rtl_433"),
        (["satdump"], "Satellite decoder (SatDump)", "brew install satdump"),
    ]
    for names, desc, hint in checks:
        if len(names) == 1 or desc.startswith("POCSAG"):
            ok = all(shutil.which(n) for n in names)
        else:
            ok = any(shutil.which(n) for n in names)
        if ok:
            print(f"  + {desc}")
        else:
            print(f"  - {desc} -- install with: {hint}")
    if HACKRF_AVAILABLE:
        print("  + HackRF library (libhackrf)")
    else:
        print(f"  - HackRF library (libhackrf), optional -- {HACKRF_INSTALL_HINT}")


def run():
    """Synchronous entry point for console_scripts (delegates to the lightweight CLI)."""
    from .cli import run as cli_run
    cli_run()


if __name__ == "__main__":
    run()
