#!/usr/bin/env python3
"""
SDR MCP Server - Direct control for RTL-SDR with protocol decoding
"""

import asyncio
import json
import logging
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass, asdict
from datetime import datetime
import numpy as np
from contextlib import asynccontextmanager

# MCP imports
from mcp.server import Server
from mcp.types import Tool, TextContent, Resource
import mcp.server.stdio

# Import hardware drivers
from .hardware.rtlsdr import RTLSDRDevice, RTLSDR_AVAILABLE
from .hardware.hackrf import HackRFDevice, HACKRF_AVAILABLE, HackRFMode

# Import analysis modules
from .analysis.spectrum import SpectrumAnalyzer, SignalRecorder, FrequencyScanner, AudioRecorder

# Protocol decoder imports
try:
    import pyModeS as pms
    ADSB_AVAILABLE = True
except ImportError:
    ADSB_AVAILABLE = False
    logging.warning("ADS-B decoder not available. Install with: pip install pyModeS")

from .decoders.pocsag import POCSAGDecoder
from .decoders.ais import AISDecoder
from .decoders.noaa_apt import NOAAAPTDecoder

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Constants
DEFAULT_SAMPLE_RATE = 2.048e6
DEFAULT_GAIN = 'auto'
ADSB_FREQUENCY = 1090e6
ADSB_SAMPLE_RATE = 2e6

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
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    last_seen: datetime = None
    message_count: int = 0

class ADSBDecoder:
    """ADS-B protocol decoder"""
    def __init__(self):
        self.aircraft: Dict[str, Aircraft] = {}
        self.message_count = 0
        
    def decode_message(self, msg_hex: str) -> Optional[Dict[str, Any]]:
        """Decode a single ADS-B message from hex string"""
        if not ADSB_AVAILABLE:
            return None

        try:
            # pyModeS expects hex strings, not bytes
            if len(msg_hex) != 28:  # 14 bytes * 2 hex chars
                return None

            # Get ICAO address
            icao = pms.adsb.icao(msg_hex)
            if not icao:
                return None

            # Update or create aircraft entry
            if icao not in self.aircraft:
                self.aircraft[icao] = Aircraft(icao=icao, last_seen=datetime.now())

            aircraft = self.aircraft[icao]
            aircraft.last_seen = datetime.now()
            aircraft.message_count += 1
            self.message_count += 1

            # Decode message type
            tc = pms.adsb.typecode(msg_hex)

            # Aircraft identification (callsign)
            if 1 <= tc <= 4:
                callsign = pms.adsb.callsign(msg_hex)
                if callsign:
                    aircraft.callsign = callsign.strip()

            # Airborne position
            elif tc in [9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 20, 21, 22]:
                alt = pms.adsb.altitude(msg_hex)
                if alt:
                    aircraft.altitude = alt

            # Airborne velocity
            elif tc == 19:
                velocity = pms.adsb.velocity(msg_hex)
                if velocity:
                    speed, heading, _, _ = velocity
                    if speed:
                        aircraft.speed = speed
                    if heading:
                        aircraft.heading = heading

            return {
                "icao": icao,
                "aircraft": asdict(aircraft),
                "message_type": tc,
                "raw": msg_hex
            }

        except Exception as e:
            logger.debug(f"Failed to decode ADS-B message: {e}")
            return None
            
    def get_aircraft_list(self) -> List[Dict[str, Any]]:
        """Get list of all tracked aircraft"""
        now = datetime.now()
        active_aircraft = []

        for icao, aircraft in self.aircraft.items():
            # Only include aircraft seen in last 120 seconds (2 minutes)
            time_diff = (now - aircraft.last_seen).total_seconds()
            if time_diff < 120:
                active_aircraft.append(asdict(aircraft))

        return active_aircraft

class SDRMCPServer:
    """MCP Server for SDR control"""

    def __init__(self):
        self.server = Server("sdr-mcp")
        self.sdr: Optional[SDRDevice] = None
        self.adsb_decoder = ADSBDecoder()
        self.pocsag_decoder = POCSAGDecoder()
        self.ais_decoder = AISDecoder()
        self.noaa_decoder = NOAAAPTDecoder()
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
                    description="Start tracking aircraft via ADS-B on 1090 MHz",
                    inputSchema={"type": "object", "properties": {}}
                ),
                Tool(
                    name="aviation_stop_tracking",
                    description="Stop tracking aircraft",
                    inputSchema={"type": "object", "properties": {}}
                ),
                Tool(
                    name="aviation_get_aircraft",
                    description="Get list of currently tracked aircraft",
                    inputSchema={"type": "object", "properties": {}}
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
                    name="satellite_decode_noaa",
                    description="Decode NOAA weather satellite APT transmission from recorded IQ samples",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "satellite": {
                                "type": "string",
                                "enum": ["NOAA-15", "NOAA-18", "NOAA-19"],
                                "description": "NOAA satellite identifier"
                            },
                            "duration": {
                                "type": "number",
                                "description": "Recording duration in seconds (typically 600-900 for full pass)",
                                "default": 600
                            }
                        },
                        "required": ["satellite"]
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
                )
            ]
            return tools
            
        @self.server.call_tool()
        async def call_tool(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
            """Handle tool calls"""
            
            try:
                if name == "sdr_connect":
                    device_type = arguments.get("device_type", "rtlsdr")
                    device_index = arguments.get("device_index", 0)
                    
                    if device_type == "rtlsdr":
                        self.sdr = RTLSDRDevice()
                        success = await self.sdr.connect()
                        if success:
                            return [TextContent(type="text", text="Successfully connected to RTL-SDR")]
                        else:
                            return [TextContent(type="text", text="Failed to connect to RTL-SDR. Check device connection.")]
                    elif device_type == "hackrf":
                        self.sdr = HackRFDevice(device_index)
                        success = await self.sdr.connect()
                        if success:
                            return [TextContent(type="text", text="Successfully connected to HackRF")]
                        else:
                            return [TextContent(type="text", text="Failed to connect to HackRF. Check device connection.")]
                    else:
                        return [TextContent(type="text", text=f"Unsupported device type: {device_type}")]
                        
                elif name == "sdr_disconnect":
                    if self.sdr:
                        # Stop all active decoders
                        for decoder_name, task in self.active_decoders.items():
                            task.cancel()
                        self.active_decoders.clear()
                        
                        await self.sdr.disconnect()
                        self.sdr = None
                        return [TextContent(type="text", text="Disconnected from SDR")]
                    else:
                        return [TextContent(type="text", text="No SDR connected")]
                        
                elif name == "sdr_set_frequency":
                    if not self.sdr:
                        return [TextContent(type="text", text="No SDR connected")]
                    freq = arguments["frequency"]
                    await self.sdr.set_frequency(freq)
                    return [TextContent(type="text", text=f"Set frequency to {freq/1e6:.3f} MHz")]
                    
                elif name == "sdr_set_gain":
                    if not self.sdr:
                        return [TextContent(type="text", text="No SDR connected")]
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
                    if "adsb" in self.active_decoders:
                        return [TextContent(type="text", text="ADS-B tracking already active")]

                    # Start ADS-B decoder task (will handle SDR access)
                    try:
                        self.active_decoders["adsb"] = asyncio.create_task(self._adsb_decoder_task())
                        await asyncio.sleep(2.0)  # Give it time to disconnect SDR and start rtl_adsb

                        # Check if it failed immediately
                        if self.active_decoders["adsb"].done():
                            try:
                                await self.active_decoders["adsb"]
                            except Exception as e:
                                del self.active_decoders["adsb"]
                                return [TextContent(type="text", text=f"Failed to start ADS-B: {str(e)}\n\nMake sure the RTL-SDR is connected.")]

                        return [TextContent(type="text", text="Started ADS-B aircraft tracking on 1090 MHz\n\nNOTE: Python SDR control is paused while tracking.\nUse aviation_stop_tracking to regain SDR control.")]
                    except Exception as e:
                        return [TextContent(type="text", text=f"Failed to start ADS-B tracking: {str(e)}")]
                    
                elif name == "aviation_stop_tracking":
                    if "adsb" in self.active_decoders:
                        self.active_decoders["adsb"].cancel()
                        del self.active_decoders["adsb"]
                        return [TextContent(type="text", text="Stopped ADS-B tracking")]
                    else:
                        return [TextContent(type="text", text="ADS-B tracking not active")]
                        
                elif name == "aviation_get_aircraft":
                    # Get aircraft list and log details
                    aircraft_list = self.adsb_decoder.get_aircraft_list()
                    total_tracked = len(self.adsb_decoder.aircraft)

                    logger.info(f"Total aircraft ever seen: {total_tracked}")
                    logger.info(f"Active aircraft (last 2 min): {len(aircraft_list)}")
                    logger.info(f"Total messages decoded: {self.adsb_decoder.message_count}")

                    summary = f"Tracking {len(aircraft_list)} active aircraft\n"
                    summary += f"Total messages decoded: {self.adsb_decoder.message_count}\n"
                    summary += f"Total aircraft seen: {total_tracked}\n\n"

                    if not aircraft_list and total_tracked > 0:
                        summary += "⚠️  Aircraft were detected but none are active in the last 2 minutes.\n"
                        summary += "This is normal - aircraft may have flown out of range.\n\n"

                    for aircraft in aircraft_list:
                        summary += f"ICAO: {aircraft['icao']}"
                        if aircraft['callsign']:
                            summary += f" ({aircraft['callsign']})"
                        if aircraft['altitude']:
                            summary += f" - Alt: {aircraft['altitude']:,.0f} ft"
                        if aircraft['speed']:
                            summary += f" - Speed: {aircraft['speed']:.0f} kts"
                        summary += f" - Messages: {aircraft['message_count']}\n"

                    return [TextContent(type="text", text=summary)]

                elif name == "pager_start_decoding":
                    if not self.sdr:
                        return [TextContent(type="text", text="No SDR connected")]
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
                        return [TextContent(type="text", text="No SDR connected")]
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

                elif name == "satellite_decode_noaa":
                    if not self.sdr:
                        return [TextContent(type="text", text="No SDR connected")]

                    satellite = arguments["satellite"]
                    duration = arguments.get("duration", 600)

                    # Get NOAA frequency
                    from .decoders.noaa_apt import NOAA_FREQUENCIES
                    if satellite not in NOAA_FREQUENCIES:
                        return [TextContent(type="text", text=f"Unknown satellite: {satellite}")]

                    freq = NOAA_FREQUENCIES[satellite]
                    await self.sdr.set_frequency(freq)

                    result = f"Recording {satellite} APT transmission...\n"
                    result += f"Frequency: {freq/1e6:.3f} MHz\n"
                    result += f"Duration: {duration} seconds\n\n"

                    # Record IQ samples
                    logger.info(f"Recording {satellite} for {duration} seconds")
                    samples_list = []
                    chunks = int(duration / 5)  # 5 second chunks

                    for i in range(chunks):
                        chunk = await self.sdr.read_samples(int(self.sdr.sample_rate * 5))
                        samples_list.append(chunk)
                        if i % 6 == 0:  # Log every 30 seconds
                            logger.info(f"Recording progress: {(i+1)*5}/{duration} seconds")

                    all_samples = np.concatenate(samples_list)

                    # Decode APT
                    logger.info("Decoding NOAA APT image...")
                    image = self.noaa_decoder.decode_pass(all_samples, int(self.sdr.sample_rate), satellite)

                    if image:
                        # Save image
                        import os
                        output_dir = "recordings"
                        os.makedirs(output_dir, exist_ok=True)
                        filename = f"{output_dir}/noaa_{satellite}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                        self.noaa_decoder.save_image(image, filename)

                        result += f"✅ Successfully decoded!\n"
                        result += f"Lines decoded: {image.num_lines}\n"
                        result += f"Quality: {image.quality*100:.1f}%\n"
                        result += f"Saved to: {filename}\n"
                    else:
                        result += "❌ Failed to decode image - no valid sync patterns found\n"

                    return [TextContent(type="text", text=result)]

                elif name == "spectrum_analyze":
                    if not self.sdr:
                        return [TextContent(type="text", text="No SDR connected")]
                        
                    bandwidth = arguments.get("bandwidth", 2048000)
                    fft_size = arguments.get("fft_size", 2048)
                    window = arguments.get("window", "blackman-harris")
                    averaging = arguments.get("averaging", True)
                    
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
                        return [TextContent(type="text", text="No SDR connected")]
                        
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
                        return [TextContent(type="text", text="No SDR connected")]
                        
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
                        return [TextContent(type="text", text="No SDR connected")]

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
                        return [TextContent(type="text", text="This command requires a HackRF device")]
                        
                    gain = arguments["gain"]
                    await self.sdr.set_tx_gain(gain)
                    return [TextContent(type="text", text=f"Set HackRF TX gain to {gain} dB")]
                    
                elif name == "signal_generator":
                    if not isinstance(self.sdr, HackRFDevice):
                        return [TextContent(type="text", text="Signal generation requires a HackRF device")]
                        
                    frequency = arguments["frequency"]
                    signal_type = arguments["signal_type"]
                    duration = arguments.get("duration", 1.0)
                    tone_freq = arguments.get("tone_freq", 1000)
                    
                    # Safety check
                    if not self.sdr.validate_tx_safety(frequency):
                        return [TextContent(type="text", text="Cannot transmit on this frequency (safety restriction)")]
                        
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
                        return [TextContent(type="text", text=f"Unknown signal type: {signal_type}")]
                        
                    # Set frequency and transmit
                    await self.sdr.set_frequency(frequency)
                    await self.sdr.write_samples(signal * 0.8)  # Scale for safety
                    
                    await asyncio.sleep(duration)
                    
                    return [TextContent(type="text", 
                        text=f"Transmitted {signal_type} signal at {frequency/1e6:.3f} MHz for {duration} seconds")]
                    
                else:
                    return [TextContent(type="text", text=f"Unknown tool: {name}")]
                    
            except Exception as e:
                return [TextContent(type="text", text=f"Error: {str(e)}")]
                
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
                aircraft_data = {
                    "aircraft": self.adsb_decoder.get_aircraft_list(),
                    "total_messages": self.adsb_decoder.message_count,
                    "decoder_active": "adsb" in self.active_decoders
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
                
    async def _adsb_decoder_task(self):
        """Background task for ADS-B decoding using rtl_adsb subprocess

        NOTE: This temporarily disconnects Python SDR control and gives
        exclusive access to rtl_adsb. Other SDR functions won't work while this runs.
        Stop tracking to regain SDR control.
        """
        logger.info("Starting ADS-B decoder with rtl_adsb subprocess")

        # Disconnect Python SDR to free the device
        python_sdr_was_connected = self.sdr is not None
        if self.sdr:
            logger.info("Releasing SDR device for rtl_adsb")
            await self.sdr.disconnect()
            self.sdr = None

            # Force garbage collection and wait for USB release
            import gc
            gc.collect()
            await asyncio.sleep(0.5)  # Give OS time to release USB device
            logger.info("USB device released")

        process = None
        try:
            # Find rtl_adsb binary
            import shutil
            import os
            rtl_adsb_path = shutil.which('rtl_adsb') or '/opt/homebrew/bin/rtl_adsb'

            if not rtl_adsb_path or not os.path.exists(rtl_adsb_path):
                logger.error(f"rtl_adsb not found! Checked: {rtl_adsb_path}")
                raise FileNotFoundError(f"rtl_adsb not found at {rtl_adsb_path}")

            logger.info(f"Found rtl_adsb at: {rtl_adsb_path}")
            logger.info("Starting rtl_adsb subprocess...")

            # Start rtl_adsb subprocess
            process = await asyncio.create_subprocess_exec(
                rtl_adsb_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            logger.info(f"rtl_adsb started (PID: {process.pid})")

            # Monitor stderr for errors
            async def check_stderr():
                first_lines = []
                try:
                    for _ in range(15):
                        line = await asyncio.wait_for(process.stderr.readline(), timeout=0.3)
                        if line:
                            decoded = line.decode().strip()
                            first_lines.append(decoded)
                            logger.info(f"rtl_adsb: {decoded}")
                            if 'error' in decoded.lower() or 'failed' in decoded.lower():
                                logger.error(f"rtl_adsb ERROR: {decoded}")
                except asyncio.TimeoutError:
                    pass  # No more stderr
                return first_lines

            logger.info("Reading rtl_adsb startup messages...")
            stderr_lines = await check_stderr()
            logger.info(f"Got {len(stderr_lines)} stderr lines")

            # Check if it started successfully
            if any('Failed' in line or 'error -' in line for line in stderr_lines):
                error_msg = '\n'.join(stderr_lines)
                logger.error(f"rtl_adsb FAILED TO START:\n{error_msg}")
                raise RuntimeError(f"rtl_adsb failed: {error_msg}")

            msg_count = 0
            decode_count = 0
            last_log_time = asyncio.get_event_loop().time()

            # Read and decode messages
            while True:
                line = await process.stdout.readline()
                if not line:
                    logger.error("rtl_adsb output ended")
                    break

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
            if process:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2.0)
                except:
                    process.kill()

            # Reconnect Python SDR if it was connected before
            if python_sdr_was_connected:
                logger.info("Reconnecting Python SDR control")
                from .hardware.rtlsdr import RTLSDRDevice
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

    async def run(self):
        """Run the MCP server"""
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await self.server.run(
                read_stream,
                write_stream,
                self.server.create_initialization_options()
            )

async def main():
    """Main entry point"""
    server = SDRMCPServer()
    await server.run()

if __name__ == "__main__":
    asyncio.run(main())