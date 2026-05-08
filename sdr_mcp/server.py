#!/usr/bin/env python3
"""
SDR MCP Server - Direct control for RTL-SDR with protocol decoding
"""

import asyncio
import json
import logging
import os
import shutil
import urllib.request
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict
from datetime import datetime
import numpy as np

# MCP imports
from mcp.server import Server
from mcp.types import Tool, TextContent, Resource
import mcp.server.stdio

# Import hardware drivers
from .hardware.rtlsdr import RTLSDRDevice, RTLSDR_AVAILABLE
from .hardware.hackrf import HackRFDevice, HACKRF_AVAILABLE, HackRFMode

# Import analysis modules
from .analysis.spectrum import SpectrumAnalyzer, SignalRecorder, FrequencyScanner, AudioRecorder

# Import validators
from .utils.validators import sanitize_path_component, is_restricted_frequency, find_binary

# Protocol decoder imports
try:
    import pyModeS as pms
    ADSB_AVAILABLE = True
except ImportError:
    ADSB_AVAILABLE = False
    logging.warning("ADS-B decoder not available. Install with: pip install pyModeS")

from .decoders.pocsag import POCSAGDecoder
from .decoders.ais import AISDecoder
from .decoders.rtl433 import RTL433Decoder
from .decoders.meteor_lrpt import MeteorLRPTDecoder

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Constants
DEFAULT_SAMPLE_RATE = 2.048e6
DEFAULT_GAIN = 'auto'
ADSB_FREQUENCY = 1090e6
ADSB_SAMPLE_RATE = 2e6

# Valid FFT window functions
VALID_WINDOWS = {"hamming", "hann", "blackman", "blackman-harris", "flattop"}


class SDRError(Exception):
    """Error raised by SDR operations. Propagates to MCP clients as isError: true."""
    pass

@dataclass
class SDRStatus:
    """Current SDR hardware status"""
    connected: bool
    device_name: str
    frequency: float
    sample_rate: float
    gain: float
    is_capturing: bool
    active_decoders: List[str]

@dataclass
class Aircraft:
    """Tracked aircraft data"""
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
    last_seen: datetime = None
    message_count: int = 0

class ADSBDecoder:
    """ADS-B protocol decoder"""
    def __init__(self):
        self.aircraft: Dict[str, Aircraft] = {}
        self.registration_cache: Dict[str, Dict[str, Any]] = {}
        self.raw_message_count = 0
        self.message_count = 0
        
    def decode_message(self, msg_hex: str) -> Optional[Dict[str, Any]]:
        """Decode a single ADS-B message from hex string"""
        self.raw_message_count += 1
        if not ADSB_AVAILABLE:
            return None

        try:
            # pyModeS expects hex strings, not bytes
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

            # Get ICAO address
            icao = decoded.get("icao")
            if not icao:
                return None
            icao = str(icao).upper()

            # Update or create aircraft entry
            if icao not in self.aircraft:
                self.aircraft[icao] = Aircraft(icao=icao, last_seen=datetime.now())

            aircraft = self.aircraft[icao]
            aircraft.last_seen = datetime.now()
            aircraft.message_count += 1
            self.message_count += 1

            # Decode message type
            tc = decoded.get("typecode")

            # Aircraft identification (callsign)
            if isinstance(tc, int) and 1 <= tc <= 4:
                callsign = decoded.get("callsign")
                if callsign:
                    aircraft.callsign = str(callsign).strip().replace("_", "")

            # Airborne position
            elif isinstance(tc, int) and 9 <= tc <= 18:
                alt = decoded.get("altitude")
                if alt is not None:
                    aircraft.altitude = alt
                if decoded.get("latitude") is not None:
                    aircraft.latitude = decoded["latitude"]
                if decoded.get("longitude") is not None:
                    aircraft.longitude = decoded["longitude"]

            # Airborne velocity
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
                "raw": msg_hex
            }

        except Exception as e:
            logger.debug(f"Failed to decode ADS-B message: {e}")
            return None

    def _decode_adsb_fields(self, msg_hex: str) -> Optional[Dict[str, Any]]:
        """Decode Mode-S fields with pyModeS v3, falling back to the v2 API."""
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
        """Get list of all tracked aircraft"""
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

class SDRMCPServer:
    """MCP Server for SDR control"""

    def __init__(self):
        self.server = Server(
            "sdr-mcp",
            version="0.1.0",
            instructions=(
                "AetherLink SDR MCP Server. Controls RTL-SDR and HackRF software-defined "
                "radio hardware for spectrum analysis, signal decoding (ADS-B aircraft, AIS "
                "ships, POCSAG pagers, Meteor-M satellites, ISM band devices), recording, "
                "and transmission (HackRF only). Connect an SDR device first with sdr_connect "
                "before using analysis or decoding tools."
            ),
        )
        self.sdr: Optional[SDRDevice] = None
        self.adsb_decoder = ADSBDecoder()
        self.pocsag_decoder = POCSAGDecoder()
        self.ais_decoder = AISDecoder()
        self.rtl433_decoder = RTL433Decoder()
        self.meteor_decoder = MeteorLRPTDecoder()
        self.active_decoders: Dict[str, asyncio.Task] = {}

        # Analysis modules
        self.spectrum_analyzer = SpectrumAnalyzer()
        self.signal_recorder = SignalRecorder()
        self.audio_recorder = AudioRecorder()
        self.frequency_scanner = FrequencyScanner(self.spectrum_analyzer)

        self.setup_handlers()
        
    def setup_handlers(self):
        """Setup MCP server handlers"""
        
        @self.server.list_tools()
        async def list_tools() -> List[Tool]:
            """List available SDR tools"""
            tools = [
                Tool(
                    name="sdr_connect",
                    description="Connect to SDR hardware (RTL-SDR or HackRF)",
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
                                "default": 0
                            }
                        }
                    }
                ),
                Tool(
                    name="sdr_disconnect",
                    description="Disconnect from SDR hardware",
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
                    description="Get current SDR status and configuration",
                    inputSchema={"type": "object", "properties": {}}
                ),
                Tool(
                    name="aviation_track_aircraft",
                    description="Start tracking aircraft via ADS-B on 1090 MHz using dump1090",
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
                                "description": "Optional tracking duration in seconds; 0 runs until stopped",
                                "default": 0,
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
                        },
                    }
                ),
                Tool(
                    name="aviation_stop_tracking",
                    description="Stop tracking aircraft",
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
                                "description": "Resolve registration, type, and operator via hexdb.io",
                                "default": True,
                            },
                        },
                    }
                ),
                Tool(
                    name="pager_start_decoding",
                    description="Start decoding POCSAG pager messages on current frequency",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "baud_rate": {
                                "type": "integer",
                                "enum": [512, 1200, 2400],
                                "description": "POCSAG baud rate",
                                "default": 1200
                            }
                        }
                    }
                ),
                Tool(
                    name="pager_stop_decoding",
                    description="Stop decoding POCSAG pager messages",
                    inputSchema={"type": "object", "properties": {}}
                ),
                Tool(
                    name="pager_get_messages",
                    description="Get decoded pager messages",
                    inputSchema={"type": "object", "properties": {}}
                ),
                Tool(
                    name="marine_track_vessels",
                    description="Start tracking ships via AIS on 161.975 MHz or 162.025 MHz",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "channel": {
                                "type": "string",
                                "enum": ["A", "B"],
                                "description": "AIS channel (A=161.975 MHz, B=162.025 MHz)",
                                "default": "A"
                            }
                        }
                    }
                ),
                Tool(
                    name="marine_stop_tracking",
                    description="Stop tracking ships",
                    inputSchema={"type": "object", "properties": {}}
                ),
                Tool(
                    name="marine_get_vessels",
                    description="Get list of tracked vessels",
                    inputSchema={"type": "object", "properties": {}}
                ),
                Tool(
                    name="satellite_decode_meteor",
                    description="Decode Meteor-M weather satellite LRPT transmission using SatDump",
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
                                "type": "number",
                                "description": "Recording duration in seconds (typically 600-900 for full pass)",
                                "default": 600
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
                    name="spectrum_analyze",
                    description="Perform advanced spectrum analysis at current frequency",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "bandwidth": {
                                "type": "number",
                                "description": "Analysis bandwidth in Hz",
                                "default": 2048000
                            },
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
                    }
                ),
                Tool(
                    name="spectrum_scan",
                    description="Scan a frequency range for signals",
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
                                "description": "Step size in Hz",
                                "default": 1000000
                            },
                            "dwell_time": {
                                "type": "number",
                                "description": "Dwell time per frequency in seconds",
                                "default": 0.1
                            }
                        },
                        "required": ["start_freq", "stop_freq"]
                    }
                ),
                Tool(
                    name="recording_start",
                    description="Start recording IQ samples to file",
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
                    description="Stop current recording",
                    inputSchema={"type": "object", "properties": {}}
                ),
                Tool(
                    name="audio_record_start",
                    description="Start recording demodulated audio (FM/AM) to WAV file",
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
                    description="Stop current audio recording",
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
                    description="Generate and transmit a signal (HackRF only)",
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
                                "description": "Duration in seconds",
                                "default": 1.0
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
                    description="Start scanning ISM bands for devices (433MHz, 315MHz, 868MHz, 915MHz) using rtl_433",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "frequencies": {
                                "type": "array",
                                "description": "List of frequencies to scan in MHz (e.g., [433.92, 315])",
                                "items": {"type": "number"},
                                "default": [433.92, 315]
                            },
                            "hop_interval": {
                                "type": "integer",
                                "description": "Hop between frequencies every N seconds",
                                "default": 30
                            }
                        }
                    }
                ),
                Tool(
                    name="ism_stop_scanning",
                    description="Stop ISM band scanning",
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
                    }
                )
            ]
            return tools
            
        @self.server.call_tool()
        async def call_tool(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
            """Handle tool calls"""
            if name == "sdr_connect":
                device_type = arguments.get("device_type", "rtlsdr")
                device_index = arguments.get("device_index", 0)

                if device_type == "rtlsdr":
                    self.sdr = RTLSDRDevice()
                    success = await self.sdr.connect()
                    if success:
                        return [TextContent(type="text", text="Successfully connected to RTL-SDR")]
                    else:
                        raise SDRError("Failed to connect to RTL-SDR. Check device connection.")
                elif device_type == "hackrf":
                    self.sdr = HackRFDevice(device_index)
                    success = await self.sdr.connect()
                    if success:
                        return [TextContent(type="text", text="Successfully connected to HackRF")]
                    else:
                        raise SDRError("Failed to connect to HackRF. Check device connection.")
                else:
                    raise SDRError(f"Unsupported device type: {device_type}")
                        
            elif name == "sdr_disconnect":
                if self.sdr:
                    # Stop all active decoders and await cleanup
                    for decoder_name, task in list(self.active_decoders.items()):
                        task.cancel()
                        try:
                            await task
                        except (asyncio.CancelledError, Exception):
                            pass
                    self.active_decoders.clear()

                    await self.sdr.disconnect()
                    self.sdr = None
                    return [TextContent(type="text", text="Disconnected from SDR")]
                else:
                    raise SDRError("No SDR connected")

            elif name == "sdr_set_frequency":
                if not self.sdr:
                    raise SDRError("No SDR connected. Use sdr_connect first.")
                freq = arguments["frequency"]
                await self.sdr.set_frequency(freq)
                return [TextContent(type="text", text=f"Set frequency to {freq/1e6:.3f} MHz")]

            elif name == "sdr_set_gain":
                if not self.sdr:
                    raise SDRError("No SDR connected. Use sdr_connect first.")
                gain = arguments["gain"]
                await self.sdr.set_gain(gain)
                
                # Format gain display based on device type
                if isinstance(self.sdr, HackRFDevice) and isinstance(gain, dict):
                    gain_str = f"LNA: {gain.get('lna_gain', 'N/A')} dB, VGA: {gain.get('vga_gain', 'N/A')} dB"
                    if 'amp_enable' in gain:
                        gain_str += f", Amp: {'ON' if gain['amp_enable'] else 'OFF'}"
                    return [TextContent(type="text", text=f"Set gain to {gain_str}")]
                else:
                    return [TextContent(type="text", text=f"Set gain to {gain}")]
                
            elif name == "sdr_get_status":
                if not self.sdr:
                    status = SDRStatus(
                        connected=False,
                        device_name="None",
                        frequency=0,
                        sample_rate=0,
                        gain=0,
                        is_capturing=False,
                        active_decoders=[]
                    )
                else:
                    status = SDRStatus(
                        connected=True,
                        device_name=self.sdr.device_name,
                        frequency=self.sdr.frequency,
                        sample_rate=self.sdr.sample_rate,
                        gain=self.sdr.gain,
                        is_capturing=self.sdr.is_capturing,
                        active_decoders=list(self.active_decoders.keys())
                    )
                return [TextContent(type="text", text=json.dumps(asdict(status), indent=2))]
                
            elif name == "aviation_track_aircraft":
                existing_task = self.active_decoders.get("adsb")
                if existing_task:
                    if existing_task.done():
                        self.active_decoders.pop("adsb", None)
                    else:
                        return [TextContent(type="text", text="ADS-B tracking already active")]

                if not ADSB_AVAILABLE:
                    return [TextContent(
                        type="text",
                        text="Failed to start ADS-B: pyModeS is not installed. Install with: pip install pyModeS",
                    )]

                gain = arguments.get("gain", 40)
                duration = int(arguments.get("duration", 0) or 0)
                aggressive = bool(arguments.get("aggressive", True))
                fix_crc = bool(arguments.get("fix_crc", True))

                # Start ADS-B decoder task (will handle SDR access)
                try:
                    task = asyncio.create_task(
                        self._adsb_decoder_task(
                            gain=str(gain),
                            duration=duration,
                            aggressive=aggressive,
                            fix_crc=fix_crc,
                        )
                    )
                    self.active_decoders["adsb"] = task
                    await asyncio.sleep(2.0)  # Give it time to disconnect SDR and start dump1090

                    # Check if it failed immediately
                    if task.done():
                        self.active_decoders.pop("adsb", None)
                        try:
                            await task
                        except Exception as e:
                            return [TextContent(type="text", text=f"Failed to start ADS-B: {str(e)}\n\nMake sure the RTL-SDR is connected.")]

                    duration_msg = (
                        "until stopped" if duration <= 0 else f"for {duration} seconds"
                    )
                    return [TextContent(
                        type="text",
                        text=(
                            "Started ADS-B aircraft tracking on 1090 MHz with dump1090\n"
                            f"Gain: {gain} dB | Duration: {duration_msg}\n\n"
                            "NOTE: Python SDR control is paused while tracking.\n"
                            "Use aviation_stop_tracking to regain SDR control."
                        ),
                    )]
                except Exception as e:
                    return [TextContent(type="text", text=f"Failed to start ADS-B tracking: {str(e)}")]
                
            elif name == "aviation_stop_tracking":
                task = self.active_decoders.pop("adsb", None)
                if task and not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    return [TextContent(type="text", text="Stopped ADS-B tracking")]
                else:
                    return [TextContent(type="text", text="ADS-B tracking not active")]
                    
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
                active_task = self.active_decoders.get("adsb")
                decoder_active = bool(active_task and not active_task.done())

                logger.info(f"Total aircraft ever seen: {stats['total_aircraft_seen']}")
                logger.info(f"Active aircraft: {len(aircraft_list)}")
                logger.info(f"Total ADS-B messages decoded: {stats['decoded_messages']}")

                summary = f"ADS-B Aircraft Tracker ({'active' if decoder_active else 'stopped'})\n"
                summary += f"Raw messages: {stats['raw_messages']}\n"
                summary += f"Decoded messages: {stats['decoded_messages']}\n"
                summary += f"Aircraft seen: {stats['total_aircraft_seen']}\n"
                summary += f"Active aircraft: {len(aircraft_list)}\n"
                summary += (
                    f"{stats['identified_callsigns']} callsigns | "
                    f"{stats['with_altitude']} with altitude | "
                    f"{stats['climbing']} climbing | {stats['descending']} descending\n\n"
                )

                if not aircraft_list and stats["total_aircraft_seen"] > 0:
                    summary += (
                        "Aircraft were detected, but none match the current age filter.\n"
                        "Try include_inactive=true or a larger max_age value.\n\n"
                    )
                elif not aircraft_list:
                    summary += "No aircraft detected yet. Try a longer run or check antenna placement.\n"
                    return [TextContent(type="text", text=summary)]

                if aircraft_list:
                    summary += (
                        f"{'ICAO':<8} {'Reg':<10} {'Call':<9} {'Operator':<18} "
                        f"{'Type':<6} {'Alt':>8} {'Spd':>6} {'Hdg':>5} {'V/S':>7} {'Msgs':>5}\n"
                    )
                    summary += (
                        f"{'-'*8} {'-'*10} {'-'*9} {'-'*18} {'-'*6} "
                        f"{'-'*8} {'-'*6} {'-'*5} {'-'*7} {'-'*5}\n"
                    )

                    for aircraft in aircraft_list[:25]:
                        reg = aircraft.get("registration") or ""
                        callsign = aircraft.get("callsign") or ""
                        operator = aircraft.get("operator") or ""
                        if len(operator) > 17:
                            operator = operator[:16] + "."
                        icao_type = aircraft.get("icao_type") or ""
                        alt = (
                            f"{aircraft['altitude']:>7,}"
                            if aircraft.get("altitude") is not None else "     --"
                        )
                        speed = (
                            f"{aircraft['speed']:>5.0f}"
                            if aircraft.get("speed") is not None else "   --"
                        )
                        heading = (
                            f"{aircraft['heading']:>4.0f}"
                            if aircraft.get("heading") is not None else "  --"
                        )
                        vertical_rate = (
                            f"{aircraft['vertical_rate']:>+6.0f}"
                            if aircraft.get("vertical_rate") is not None else "    --"
                        )
                        summary += (
                            f"{aircraft['icao']:<8} {reg:<10} {callsign:<9} "
                            f"{operator:<18} {icao_type:<6} {alt} {speed} "
                            f"{heading:>4} {vertical_rate:>7} {aircraft['message_count']:>5}\n"
                        )

                    summary += "\nLive tracking links:\n"
                    for aircraft in aircraft_list[:15]:
                        label = " ".join(filter(None, [
                            aircraft.get("registration") or aircraft["icao"],
                            aircraft.get("operator") or "",
                            aircraft.get("callsign") or "",
                        ]))
                        summary += f"  {label}: {aircraft['tracking_url']}\n"

                return [TextContent(type="text", text=summary)]

            elif name == "pager_start_decoding":
                if not self.sdr:
                    raise SDRError("No SDR connected. Use sdr_connect first.")
                if "pocsag" in self.active_decoders:
                    return [TextContent(type="text", text="POCSAG decoding already active")]

                baud_rate = arguments.get("baud_rate", 1200)
                self.pocsag_decoder.baud_rate = baud_rate

                # Start decoder task
                self.active_decoders["pocsag"] = asyncio.create_task(
                    self._pocsag_decoder_task()
                )

                return [TextContent(type="text", text=f"Started POCSAG pager decoding at {baud_rate} baud\nFrequency: {self.sdr.frequency/1e6:.3f} MHz")]

            elif name == "pager_stop_decoding":
                if "pocsag" in self.active_decoders:
                    self.active_decoders["pocsag"].cancel()
                    del self.active_decoders["pocsag"]
                    return [TextContent(type="text", text="Stopped POCSAG decoding")]
                else:
                    return [TextContent(type="text", text="POCSAG decoding not active")]

            elif name == "pager_get_messages":
                stats = self.pocsag_decoder.get_statistics()
                messages = self.pocsag_decoder.messages

                result = f"POCSAG Messages: {stats['total_messages']}\n"
                result += f"Messages stored: {stats['messages_stored']}\n"
                result += f"Addresses seen: {stats['addresses_seen']}\n\n"

                if not messages:
                    result += "No messages decoded yet\n"
                else:
                    for msg in messages[-20:]:  # Show last 20
                        result += f"Address: {msg['address']} (Function {msg['function']})\n"
                        result += f"Type: {msg['message_type']}\n"
                        result += f"Message: {msg['message']}\n"
                        result += f"Time: {msg['timestamp']}\n\n"

                return [TextContent(type="text", text=result)]

            elif name == "marine_track_vessels":
                if not self.sdr:
                    raise SDRError("No SDR connected. Use sdr_connect first.")
                if "ais" in self.active_decoders:
                    return [TextContent(type="text", text="AIS tracking already active")]

                channel = arguments.get("channel", "A")
                ais_freq = 161.975e6 if channel == "A" else 162.025e6

                # Set frequency for AIS
                await self.sdr.set_frequency(ais_freq)

                # Start decoder task
                self.active_decoders["ais"] = asyncio.create_task(
                    self._ais_decoder_task()
                )

                return [TextContent(type="text", text=f"Started AIS vessel tracking on channel {channel} ({ais_freq/1e6:.3f} MHz)")]

            elif name == "marine_stop_tracking":
                if "ais" in self.active_decoders:
                    self.active_decoders["ais"].cancel()
                    del self.active_decoders["ais"]
                    return [TextContent(type="text", text="Stopped AIS tracking")]
                else:
                    return [TextContent(type="text", text="AIS tracking not active")]

            elif name == "marine_get_vessels":
                vessels = self.ais_decoder.get_vessel_list()
                stats = self.ais_decoder.get_statistics()

                result = f"Tracking {len(vessels)} vessels\n"
                result += f"Total messages: {stats['total_messages']}\n"
                result += f"Total vessels seen: {stats['total_vessels']}\n"
                result += f"Active vessels: {stats['active_vessels']}\n\n"

                if not vessels:
                    result += "No vessels tracked yet\n"
                else:
                    for vessel in vessels:
                        result += f"MMSI: {vessel['mmsi']}"
                        if vessel.get('name'):
                            result += f" - {vessel['name']}"
                        if vessel.get('latitude') and vessel.get('longitude'):
                            result += f"\nPosition: {vessel['latitude']:.4f}, {vessel['longitude']:.4f}"
                        if vessel.get('speed'):
                            result += f" - Speed: {vessel['speed']:.1f} kts"
                        if vessel.get('heading'):
                            result += f" - Heading: {vessel['heading']:.0f}°"
                        if vessel.get('ship_type'):
                            result += f"\nType: {vessel['ship_type']}"
                        result += f"\nMessages: {vessel['message_count']}\n\n"

                return [TextContent(type="text", text=result)]

            elif name == "satellite_decode_meteor":
                satellite = arguments.get("satellite", "METEOR-M2-4")
                duration = arguments.get("duration", 600)
                gain = arguments.get("gain", 40)

                # Validate satellite name (prevents path traversal)
                sanitize_path_component(satellite)

                # Get satellite info
                sat_info = self.meteor_decoder.get_satellite_info(satellite)
                if not sat_info:
                    raise SDRError(f"Unknown satellite: {satellite}. Active: {', '.join(self.meteor_decoder.get_active_satellites())}")

                if sat_info["status"] != "active":
                    return [TextContent(type="text", text=f"Warning: {satellite} status is '{sat_info['status']}'. Decoding may fail.\n\nActive satellites: {', '.join(self.meteor_decoder.get_active_satellites())}")]

                freq = sat_info["frequency"]

                # Check for SatDump
                satdump_path = find_binary("satdump", "brew install satdump")
                if not satdump_path:
                    raise SDRError("SatDump not found! Install with: brew install satdump\n\nSatDump is required for Meteor-M LRPT decoding.")

                result = f"Decoding {satellite} LRPT transmission...\n"
                result += f"Frequency: {freq/1e6:.3f} MHz\n"
                result += f"Duration: {duration} seconds\n"
                result += f"Gain: {gain} dB\n\n"

                # Create output directory
                output_dir = f"/tmp/sdr_recordings/meteor_{satellite}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                os.makedirs(output_dir, exist_ok=True)

                # Build SatDump command
                cmd = self.meteor_decoder.build_satdump_command(
                    satellite, freq, output_dir, duration, gain
                )

                result += f"Running: {' '.join(cmd)}\n\n"
                result += "This will take several minutes. SatDump will:\n"
                result += "1. Tune RTL-SDR to " + f"{freq/1e6:.1f} MHz\n"
                result += "2. Demodulate OQPSK signal\n"
                result += "3. Decode LRPT frames with error correction\n"
                result += "4. Generate channel images (visible, infrared)\n"
                result += f"5. Save results to: {output_dir}/\n\n"

                # Run SatDump
                try:
                    logger.info(f"Starting SatDump for {satellite}")
                    logger.info(f"Command: {' '.join(cmd)}")

                    process = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT  # Combine stderr with stdout
                    )

                    # Wait for completion and capture output
                    stdout, _ = await process.communicate()

                    # Decode output
                    output_text = stdout.decode('utf-8', errors='replace') if stdout else ""

                    logger.info(f"SatDump return code: {process.returncode}")
                    if output_text:
                        logger.info(f"SatDump output (last 500 chars): {output_text[-500:]}")

                    if process.returncode == 0:
                        # Parse output
                        parsed = self.meteor_decoder.parse_satdump_output(output_dir)

                        if parsed["success"] and parsed["images"]:
                            result += f"✅ Successfully decoded {satellite}!\n\n"
                            result += f"Images generated: {len(parsed['images'])}\n"
                            result += f"Output directory: {output_dir}/\n\n"

                            result += "Decoded files:\n"
                            for img in parsed["images"]:
                                result += f"  - {os.path.basename(img)}\n"

                            # Create pass record
                            from .decoders.meteor_lrpt import MeteorPass
                            meteor_pass = MeteorPass(
                                satellite=satellite,
                                frequency=freq,
                                start_time=datetime.now(),
                                duration=duration,
                                output_dir=output_dir,
                                decoded_images=parsed["images"],
                                success=True,
                                channels_received=parsed["channels"]
                            )
                            self.meteor_decoder.add_pass(meteor_pass)

                        else:
                            result += "❌ Decoding completed but no images were generated.\n"
                            result += "This usually means:\n"
                            result += "- Satellite was below horizon (check pass prediction)\n"
                            result += "- Signal too weak (check antenna and gain)\n"
                            result += "- Incorrect frequency\n"
                    else:
                        result += f"❌ SatDump failed with return code {process.returncode}\n"
                        if output_text:
                            # Show last part of output which usually contains the error
                            result += f"\nOutput (last 1000 chars):\n{output_text[-1000:]}\n"

                except Exception as e:
                    result += f"❌ Error running SatDump: {str(e)}\n"
                    import traceback
                    result += f"\nTraceback:\n{traceback.format_exc()}\n"

                return [TextContent(type="text", text=result)]

            elif name == "spectrum_analyze":
                if not self.sdr:
                    raise SDRError("No SDR connected. Use sdr_connect first.")

                bandwidth = arguments.get("bandwidth", 2048000)
                fft_size = arguments.get("fft_size", 2048)
                window = arguments.get("window", "blackman-harris")
                averaging = arguments.get("averaging", True)

                # Validate FFT parameters
                if fft_size <= 0 or (fft_size & (fft_size - 1)) != 0:
                    raise SDRError(f"FFT size must be a power of 2, got {fft_size}")
                if fft_size > 65536:
                    raise SDRError("FFT size too large (max 65536)")
                if window not in VALID_WINDOWS:
                    raise SDRError(f"Unknown window function: {window}. Valid: {', '.join(VALID_WINDOWS)}")

                # Update analyzer settings
                self.spectrum_analyzer.fft_size = fft_size
                self.spectrum_analyzer.window_type = window
                self.spectrum_analyzer.window = self.spectrum_analyzer._get_window(window, fft_size)
                
                # Read samples
                samples = await self.sdr.read_samples(fft_size * 2)  # Double for overlap
                
                # Analyze spectrum
                frame = await self.spectrum_analyzer.analyze_spectrum(
                    samples[:fft_size],
                    self.sdr.sample_rate,
                    self.sdr.frequency
                )
                
                # Format results
                result = f"Spectrum Analysis at {frame.center_freq/1e6:.3f} MHz\n"
                result += f"Bandwidth: {bandwidth/1e6:.3f} MHz\n"
                result += f"Window: {window}\n"
                result += f"Peak power: {frame.peak_power:.1f} dB\n"
                result += f"Noise floor: {frame.noise_floor:.1f} dB\n"
                result += f"Dynamic range: {frame.peak_power - frame.noise_floor:.1f} dB\n"
                
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
                else:
                    result += "\nNo signals detected above threshold"
                    
                return [TextContent(type="text", text=result)]
                
            elif name == "spectrum_scan":
                if not self.sdr:
                    raise SDRError("No SDR connected. Use sdr_connect first.")
                    
                start_freq = arguments["start_freq"]
                stop_freq = arguments["stop_freq"]
                step = arguments.get("step", 1e6)
                dwell_time = arguments.get("dwell_time", 0.1)
                
                result = f"Scanning {start_freq/1e6:.1f} - {stop_freq/1e6:.1f} MHz...\n"
                
                # Perform scan
                scan_results = await self.frequency_scanner.scan_range(
                    self.sdr, start_freq, stop_freq, step, dwell_time
                )
                
                # Get summary
                summary = self.frequency_scanner.get_activity_summary()
                
                result += f"\nScan complete:\n"
                result += f"- Scanned {summary['scan_points']} frequencies\n"
                result += f"- Found {summary['total_signals']} signals\n"
                
                if summary['signal_types']:
                    result += "\nSignal types detected:\n"
                    for sig_type, count in summary['signal_types'].items():
                        result += f"  - {sig_type}: {count}\n"
                        
                if summary['strongest_signal']:
                    sig = summary['strongest_signal']
                    result += f"\nStrongest signal:\n"
                    result += f"  {sig['frequency']/1e6:.3f} MHz @ {sig['power']:.1f} dB"
                    if sig.get('type'):
                        result += f" [{sig['type']}]"
                        
                return [TextContent(type="text", text=result)]
                
            elif name == "recording_start":
                if not self.sdr:
                    raise SDRError("No SDR connected. Use sdr_connect first.")
                    
                description = arguments.get("description", "")
                
                # Start recording
                recording_id = await self.signal_recorder.start_recording(
                    self.sdr.frequency,
                    self.sdr.sample_rate,
                    self.sdr.gain,
                    description
                )
                
                # Start recording task
                self.active_decoders["recorder"] = asyncio.create_task(
                    self._recording_task()
                )
                
                return [TextContent(type="text", text=f"Started recording: {recording_id}")]
                
            elif name == "recording_stop":
                if "recorder" in self.active_decoders:
                    self.active_decoders["recorder"].cancel()
                    del self.active_decoders["recorder"]

                    metadata = await self.signal_recorder.stop_recording()

                    result = f"Recording stopped:\n"
                    result += f"- ID: {metadata.get('id', 'N/A')}\n"
                    result += f"- Duration: {metadata.get('duration', 0):.1f} seconds\n"
                    result += f"- Samples: {metadata.get('samples_recorded', 0):,}\n"

                    return [TextContent(type="text", text=result)]
                else:
                    return [TextContent(type="text", text="No recording in progress")]

            elif name == "audio_record_start":
                if not self.sdr:
                    raise SDRError("No SDR connected. Use sdr_connect first.")

                modulation = arguments.get("modulation", "FM")
                description = arguments.get("description", "")

                # Start audio recording
                recording_id = await self.audio_recorder.start_recording(
                    self.sdr.frequency,
                    self.sdr.sample_rate,
                    modulation,
                    description
                )

                # Start audio recording task
                self.active_decoders["audio_recorder"] = asyncio.create_task(
                    self._audio_recording_task(modulation)
                )

                return [TextContent(type="text", text=f"Started audio recording ({modulation}): {recording_id}\nSaving to: /tmp/sdr_recordings/{recording_id}.wav")]

            elif name == "audio_record_stop":
                if "audio_recorder" in self.active_decoders:
                    self.active_decoders["audio_recorder"].cancel()
                    del self.active_decoders["audio_recorder"]

                    metadata = await self.audio_recorder.stop_recording()

                    result = f"Audio recording stopped:\n"
                    result += f"- ID: {metadata.get('id', 'N/A')}\n"
                    result += f"- Duration: {metadata.get('duration', 0):.1f} seconds\n"
                    result += f"- Audio samples: {metadata.get('samples_recorded', 0):,}\n"
                    result += f"- Modulation: {metadata.get('modulation', 'N/A')}\n"
                    result += f"- File: /tmp/sdr_recordings/{metadata.get('id', 'N/A')}.wav"

                    return [TextContent(type="text", text=result)]
                else:
                    return [TextContent(type="text", text="No audio recording in progress")]

            elif name == "hackrf_set_tx_gain":
                if not isinstance(self.sdr, HackRFDevice):
                    raise SDRError("This command requires a HackRF device")
                    
                gain = arguments["gain"]
                await self.sdr.set_tx_gain(gain)
                return [TextContent(type="text", text=f"Set HackRF TX gain to {gain} dB")]
                
            elif name == "signal_generator":
                if not isinstance(self.sdr, HackRFDevice):
                    raise SDRError("Signal generation requires a HackRF device")

                frequency = arguments["frequency"]
                signal_type = arguments["signal_type"]
                duration = arguments.get("duration", 1.0)
                tone_freq = arguments.get("tone_freq", 1000)

                # Duration cap
                if duration > 60.0:
                    raise SDRError("Maximum transmission duration is 60 seconds")

                # Safety check
                if not self.sdr.validate_tx_safety(frequency):
                    raise SDRError("Cannot transmit on this frequency (safety restriction)")
                    
                # Generate signal
                num_samples = int(self.sdr.sample_rate * duration)
                t = np.arange(num_samples) / self.sdr.sample_rate
                
                if signal_type == "cw":
                    # Continuous wave (carrier only)
                    signal = np.ones(num_samples, dtype=complex)
                elif signal_type == "tone":
                    # Single tone
                    signal = np.exp(2j * np.pi * tone_freq * t)
                elif signal_type == "noise":
                    # White noise
                    signal = (np.random.randn(num_samples) + 
                            1j * np.random.randn(num_samples)) / np.sqrt(2)
                elif signal_type == "sweep":
                    # Frequency sweep
                    sweep_rate = self.sdr.sample_rate / 4 / duration
                    phase = 2 * np.pi * sweep_rate * t**2 / 2
                    signal = np.exp(1j * phase)
                else:
                    raise SDRError(f"Unknown signal type: {signal_type}")
                    
                # Set frequency and transmit
                await self.sdr.set_frequency(frequency)
                await self.sdr.write_samples(signal * 0.8)  # Scale for safety
                
                await asyncio.sleep(duration)
                
                return [TextContent(type="text",
                    text=f"Transmitted {signal_type} signal at {frequency/1e6:.3f} MHz for {duration} seconds")]

            elif name == "ism_start_scanning":
                if "rtl433" in self.active_decoders:
                    return [TextContent(type="text", text="ISM scanning already active")]

                # Get frequencies and hop interval
                freq_mhz_list = arguments.get("frequencies", [433.92, 315])
                hop_interval = arguments.get("hop_interval", 30)

                # Validate inputs
                if len(freq_mhz_list) > 10:
                    raise SDRError("Maximum 10 frequencies allowed")
                if not (1 <= hop_interval <= 3600):
                    raise SDRError("Hop interval must be 1-3600 seconds")

                # Convert MHz to Hz and validate range
                frequencies = []
                for f_mhz in freq_mhz_list:
                    f_hz = f_mhz * 1e6
                    if not (24e6 <= f_hz <= 1.766e9):
                        raise SDRError(f"Frequency {f_mhz} MHz out of RTL-SDR range (24-1766 MHz)")
                    frequencies.append(f_hz)

                # Update decoder settings
                self.rtl433_decoder.set_frequencies(frequencies)
                self.rtl433_decoder.set_hop_interval(hop_interval)

                # Start decoder task
                try:
                    self.active_decoders["rtl433"] = asyncio.create_task(self._rtl433_decoder_task())
                    await asyncio.sleep(2.0)  # Give it time to start

                    # Check if it failed immediately
                    if self.active_decoders["rtl433"].done():
                        try:
                            await self.active_decoders["rtl433"]
                        except Exception as e:
                            del self.active_decoders["rtl433"]
                            return [TextContent(type="text", text=f"Failed to start rtl_433: {str(e)}\n\nMake sure rtl_433 is installed and RTL-SDR is connected.")]

                    freq_str = ", ".join([f"{f:.2f} MHz" for f in freq_mhz_list])
                    return [TextContent(type="text", text=f"Started ISM band scanning with rtl_433\n\nFrequencies: {freq_str}\nHop interval: {hop_interval} seconds\n\nNOTE: Python SDR control is paused while scanning.\nUse ism_stop_scanning to regain SDR control.")]
                except Exception as e:
                    return [TextContent(type="text", text=f"Failed to start ISM scanning: {str(e)}")]

            elif name == "ism_stop_scanning":
                if "rtl433" in self.active_decoders:
                    self.active_decoders["rtl433"].cancel()
                    del self.active_decoders["rtl433"]
                    return [TextContent(type="text", text="Stopped ISM scanning")]
                else:
                    return [TextContent(type="text", text="ISM scanning not active")]

            elif name == "ism_get_devices":
                max_age = arguments.get("max_age", 300)
                devices = self.rtl433_decoder.get_device_list(max_age_seconds=max_age)
                stats = self.rtl433_decoder.get_statistics()

                result = f"ISM Band Devices Detected\n"
                result += f"=" * 50 + "\n\n"
                result += f"Total messages: {stats['total_messages']}\n"
                result += f"Unique devices: {stats['total_devices_seen']}\n"
                result += f"Active devices: {stats['active_devices']}\n"
                result += f"Scanning: {', '.join([f'{f:.2f} MHz' for f in stats['frequencies_MHz']])}\n"
                result += f"Hop interval: {stats['hop_interval_seconds']}s\n\n"

                if stats['device_types']:
                    result += "Device types seen:\n"
                    for device_type, count in stats['device_types'].items():
                        result += f"  • {device_type}: {count}\n"
                    result += "\n"

                if not devices:
                    result += "No devices detected in the last " + str(max_age) + " seconds.\n"
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

                return [TextContent(type="text", text=result)]

            else:
                raise SDRError(f"Unknown tool: {name}")
                
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
            if uri == "sdr://status":
                if not self.sdr:
                    status = {
                        "connected": False,
                        "message": "No SDR connected"
                    }
                else:
                    status = asdict(SDRStatus(
                        connected=True,
                        device_name=self.sdr.device_name,
                        frequency=self.sdr.frequency,
                        sample_rate=self.sdr.sample_rate,
                        gain=self.sdr.gain,
                        is_capturing=self.sdr.is_capturing,
                        active_decoders=list(self.active_decoders.keys())
                    ))
                return json.dumps(status, indent=2)
                
            elif uri == "aviation://aircraft":
                active_task = self.active_decoders.get("adsb")
                aircraft_data = {
                    "aircraft": self.adsb_decoder.get_aircraft_list(),
                    "statistics": self.adsb_decoder.get_statistics(),
                    "total_messages": self.adsb_decoder.message_count,
                    "raw_messages": self.adsb_decoder.raw_message_count,
                    "decoder_active": bool(active_task and not active_task.done())
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
                
            else:
                return f"Unknown resource: {uri}"
                
    async def _adsb_decoder_task(
        self,
        gain: str = "40",
        duration: int = 0,
        aggressive: bool = True,
        fix_crc: bool = True,
    ):
        """Background task for ADS-B decoding using dump1090 raw TCP output.

        NOTE: This temporarily disconnects Python SDR control and gives
        exclusive access to dump1090. Other SDR functions won't work while this runs.
        Stop tracking to regain SDR control.
        """
        logger.info("Starting ADS-B decoder with dump1090 subprocess")

        # Disconnect Python SDR to free the device
        python_sdr_was_connected = self.sdr is not None
        if self.sdr:
            logger.info("Releasing SDR device for dump1090")
            await self.sdr.disconnect()
            self.sdr = None

            # Force garbage collection and wait for USB release
            import gc
            gc.collect()
            await asyncio.sleep(0.5)  # Give OS time to release USB device
            logger.info("USB device released")

        process = None
        writer = None
        try:
            dump1090_path = find_binary("dump1090", "install dump1090 or dump1090-fa")
            cmd = [
                dump1090_path,
                "--net",
                "--gain", gain,
                "--quiet",
            ]
            if fix_crc:
                cmd.append("--fix")
            if aggressive:
                cmd.append("--aggressive")

            logger.info(f"Found dump1090 at: {dump1090_path}")
            logger.info(f"Starting dump1090 subprocess: {' '.join(cmd)}")

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE
            )

            logger.info(f"dump1090 started (PID: {process.pid})")

            # Monitor stderr for errors
            async def check_stderr():
                first_lines = []
                try:
                    for _ in range(15):
                        line = await asyncio.wait_for(process.stderr.readline(), timeout=0.3)
                        if line:
                            decoded = line.decode().strip()
                            first_lines.append(decoded)
                            logger.info(f"dump1090: {decoded}")
                            if 'error' in decoded.lower() or 'failed' in decoded.lower():
                                logger.error(f"dump1090 ERROR: {decoded}")
                except asyncio.TimeoutError:
                    pass  # No more stderr
                return first_lines

            logger.info("Reading dump1090 startup messages...")
            stderr_lines = await check_stderr()
            logger.info(f"Got {len(stderr_lines)} stderr lines")

            # Check if it started successfully
            if any('Failed' in line or 'error -' in line for line in stderr_lines):
                error_msg = '\n'.join(stderr_lines)
                logger.error(f"dump1090 FAILED TO START:\n{error_msg}")
                raise RuntimeError(f"dump1090 failed: {error_msg}")

            # dump1090 needs a moment before the Beast/raw TCP ports are ready.
            reader = None
            for attempt in range(10):
                if process.returncode is not None:
                    raise RuntimeError(f"dump1090 exited early with code {process.returncode}")
                try:
                    reader, writer = await asyncio.open_connection("127.0.0.1", 30002)
                    break
                except OSError:
                    await asyncio.sleep(0.5)

            if reader is None:
                raise RuntimeError("Could not connect to dump1090 raw output on 127.0.0.1:30002")

            msg_count = 0
            decode_count = 0
            last_log_time = asyncio.get_event_loop().time()
            deadline = (
                asyncio.get_event_loop().time() + duration
                if duration and duration > 0 else None
            )

            # Read and decode messages
            while True:
                if deadline and asyncio.get_event_loop().time() >= deadline:
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

                msg_line = line.decode().strip()
                if msg_line.startswith('*') and msg_line.endswith(';'):
                    msg_count += 1
                    msg_hex = msg_line[1:-1]  # Remove * and ;

                    if len(msg_hex) == 28:
                        decoded = self.adsb_decoder.decode_message(msg_hex)
                        if decoded:
                            decode_count += 1

                # Log every 10 seconds
                current_time = asyncio.get_event_loop().time()
                if current_time - last_log_time > 10:
                    logger.info(f"ADS-B: {msg_count} msgs, {decode_count} decoded, {len(self.adsb_decoder.aircraft)} aircraft")
                    last_log_time = current_time

        except asyncio.CancelledError:
            logger.info("ADS-B tracking stopped by user")
            raise
        except Exception as e:
            import traceback
            logger.error(f"ADS-B decoder error: {e}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            raise
        finally:
            # Cleanup
            if writer:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
            if process:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2.0)
                except (asyncio.TimeoutError, Exception):
                    process.kill()
                    try:
                        await process.wait()
                    except Exception:
                        pass

            # Reconnect Python SDR if it was connected before
            if python_sdr_was_connected:
                logger.info("Reconnecting Python SDR control")
                self.sdr = RTLSDRDevice()
                await self.sdr.connect()

    async def _pocsag_decoder_task(self):
        """Background task for POCSAG pager decoding"""
        logger.info("Starting POCSAG decoder task")

        try:
            while True:
                # Read samples
                chunk_size = int(self.sdr.sample_rate * 0.5)  # 500ms chunks
                samples = await self.sdr.read_samples(chunk_size)

                # Demodulate FSK
                from scipy import signal
                # Simple FSK demodulation using frequency discrimination
                instantaneous_phase = np.unwrap(np.angle(samples))
                instantaneous_frequency = np.diff(instantaneous_phase)

                # Low-pass filter
                b, a = signal.butter(5, self.pocsag_decoder.baud_rate * 2 / (self.sdr.sample_rate / 2), 'low')
                demod = signal.filtfilt(b, a, instantaneous_frequency)

                # Convert to bits (simple threshold)
                bits = (demod > np.mean(demod)).astype(int)

                # Decode POCSAG frames from bit stream
                # Look for sync pattern and decode codewords
                # This is simplified - real implementation needs proper frame sync
                logger.debug(f"POCSAG: processed {len(bits)} bits")

        except asyncio.CancelledError:
            logger.info("POCSAG decoder task cancelled")
            raise
        except Exception as e:
            logger.error(f"POCSAG decoder error: {e}")
            raise

    async def _ais_decoder_task(self):
        """Background task for AIS ship tracking"""
        logger.info("Starting AIS decoder task")

        try:
            while True:
                # Read samples
                chunk_size = int(self.sdr.sample_rate * 0.5)  # 500ms chunks
                samples = await self.sdr.read_samples(chunk_size)

                # Demodulate GMSK (simplified - AIS uses GMSK modulation)
                # This is a placeholder - real AIS decoding requires proper GMSK demodulation
                from scipy import signal

                # FM demodulation
                instantaneous_phase = np.unwrap(np.angle(samples))
                instantaneous_frequency = np.diff(instantaneous_phase)

                # Low-pass filter for 9600 baud
                b, a = signal.butter(5, 9600 * 2 / (self.sdr.sample_rate / 2), 'low')
                demod = signal.filtfilt(b, a, instantaneous_frequency)

                # Convert to bits
                bits = (demod > np.mean(demod)).astype(int)

                # In real implementation, would decode HDLC frames here
                logger.debug(f"AIS: processed {len(bits)} bits")

        except asyncio.CancelledError:
            logger.info("AIS decoder task cancelled")
            raise
        except Exception as e:
            logger.error(f"AIS decoder error: {e}")
            raise

    async def _recording_task(self):
        """Background task for recording IQ samples"""
        logger.info("Starting recording task")

        try:
            while True:
                # Read samples in chunks
                chunk_size = int(self.sdr.sample_rate * 0.1)  # 100ms chunks
                samples = await self.sdr.read_samples(chunk_size)

                # Add to recording
                await self.signal_recorder.add_samples(samples)

        except asyncio.CancelledError:
            logger.info("Recording task cancelled")
            raise

    async def _audio_recording_task(self, modulation: str = "FM"):
        """Background task for recording demodulated audio"""
        logger.info(f"Starting audio recording task ({modulation})")

        try:
            while True:
                # Read samples in chunks
                chunk_size = int(self.sdr.sample_rate * 0.1)  # 100ms chunks
                samples = await self.sdr.read_samples(chunk_size)

                # Demodulate and add to audio recording
                await self.audio_recorder.add_samples(
                    samples,
                    self.sdr.sample_rate,
                    modulation
                )

        except asyncio.CancelledError:
            logger.info("Audio recording task cancelled")
            raise

    async def _rtl433_decoder_task(self):
        """Background task for rtl_433 ISM band decoding using rtl_433 subprocess

        NOTE: This temporarily disconnects Python SDR control and gives
        exclusive access to rtl_433. Other SDR functions won't work while this runs.
        Stop scanning to regain SDR control.
        """
        logger.info("Starting rtl_433 decoder with subprocess")

        # Disconnect Python SDR to free the device
        python_sdr_was_connected = self.sdr is not None
        if self.sdr:
            logger.info("Releasing SDR device for rtl_433")
            await self.sdr.disconnect()
            self.sdr = None

            # Force garbage collection and wait for USB release
            import gc
            gc.collect()
            await asyncio.sleep(0.5)  # Give OS time to release USB device
            logger.info("USB device released")

        process = None
        try:
            # Find rtl_433 binary
            rtl_433_path = find_binary("rtl_433", "brew install rtl_433")

            logger.info(f"Found rtl_433 at: {rtl_433_path}")

            # Build command with frequency hopping
            cmd = [rtl_433_path, "-F", "json"]

            # Add frequencies
            for freq in self.rtl433_decoder.frequencies:
                cmd.extend(["-f", f"{int(freq)}"])

            # Add hop interval if multiple frequencies
            if len(self.rtl433_decoder.frequencies) > 1:
                cmd.extend(["-H", str(self.rtl433_decoder.hop_interval)])

            logger.info(f"Starting rtl_433 with command: {' '.join(cmd)}")

            # Start rtl_433 subprocess
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            logger.info(f"rtl_433 started (PID: {process.pid})")

            # Monitor stderr for errors in background
            async def check_stderr():
                try:
                    while True:
                        line = await process.stderr.readline()
                        if not line:
                            break
                        decoded = line.decode().strip()
                        if decoded:
                            logger.info(f"rtl_433: {decoded}")
                            if 'error' in decoded.lower() or 'failed' in decoded.lower():
                                logger.error(f"rtl_433 ERROR: {decoded}")
                except Exception as e:
                    logger.debug(f"stderr monitoring ended: {e}")

            # Start stderr monitoring
            asyncio.create_task(check_stderr())

            msg_count = 0
            last_log_time = asyncio.get_event_loop().time()

            # Read and decode JSON messages
            while True:
                line = await process.stdout.readline()
                if not line:
                    logger.error("rtl_433 output ended")
                    break

                json_line = line.decode().strip()
                if json_line:
                    device = self.rtl433_decoder.parse_message(json_line)
                    if device:
                        msg_count += 1
                        logger.debug(f"Decoded {device.model}: {self.rtl433_decoder.get_device_summary(device)}")

                # Log every 30 seconds
                current_time = asyncio.get_event_loop().time()
                if current_time - last_log_time > 30:
                    stats = self.rtl433_decoder.get_statistics()
                    logger.info(f"RTL_433: {msg_count} msgs, {stats['total_devices_seen']} devices, {stats['active_devices']} active")
                    last_log_time = current_time

        except asyncio.CancelledError:
            logger.info("RTL_433 scanning stopped by user")
            raise
        except Exception as e:
            import traceback
            logger.error(f"RTL_433 decoder error: {e}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            raise
        finally:
            # Cleanup
            if process:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2.0)
                except (asyncio.TimeoutError, Exception):
                    process.kill()

            # Reconnect Python SDR if it was connected before
            if python_sdr_was_connected:
                logger.info("Reconnecting Python SDR control")
                self.sdr = RTLSDRDevice()
                await self.sdr.connect()

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

    # Determine the best command to use
    uvx_path = shutil.which("uvx")
    if uvx_path:
        server_entry = {"command": uvx_path, "args": ["aetherlink"]}
    else:
        # Fall back to the installed entry point
        aetherlink_path = shutil.which("aetherlink")
        if aetherlink_path:
            server_entry = {"command": aetherlink_path, "args": []}
        else:
            # Fall back to python -m
            server_entry = {"command": sys.executable, "args": ["-m", "sdr_mcp.server"]}

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
        answer = input("Update AetherLink entry? (other servers untouched) [y/N] ").strip().lower()
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
    for name, desc, hint in [
        ("rtl_test", "RTL-SDR drivers", "brew install rtl-sdr"),
        ("dump1090", "ADS-B decoder", "install dump1090 or dump1090-fa"),
        ("rtl_433", "ISM band decoder", "brew install rtl_433"),
        ("satdump", "Satellite decoder", "brew install satdump"),
    ]:
        if shutil.which(name):
            print(f"  + {desc} ({name})")
        else:
            print(f"  - {desc} ({name}) -- install with: {hint}")


def run():
    """Synchronous entry point for console_scripts."""
    import sys

    if "--setup" in sys.argv or "setup" in sys.argv[1:2]:
        setup_claude_desktop()
    elif "--version" in sys.argv:
        from . import __version__
        print(f"aetherlink {__version__}")
    elif "--help" in sys.argv or "-h" in sys.argv:
        print("aetherlink - SDR MCP Server")
        print()
        print("Usage:")
        print("  aetherlink           Start the MCP server (stdio)")
        print("  aetherlink --setup   Configure Claude Desktop")
        print("  aetherlink --version Show version")
    else:
        asyncio.run(main())


if __name__ == "__main__":
    run()
