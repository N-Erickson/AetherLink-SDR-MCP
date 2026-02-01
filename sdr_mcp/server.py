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
from .analysis.spectrum import SpectrumAnalyzer, SignalRecorder, FrequencyScanner

# Protocol decoder imports
try:
    import pyModeS as pms
    ADSB_AVAILABLE = True
except ImportError:
    ADSB_AVAILABLE = False
    logging.warning("ADS-B decoder not available. Install with: pip install pyModeS")

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
        
    def decode_message(self, msg: bytes) -> Optional[Dict[str, Any]]:
        """Decode a single ADS-B message"""
        if not ADSB_AVAILABLE:
            return None
            
        try:
            if len(msg) != 14:  # Standard ADS-B message length
                return None
                
            # Get ICAO address
            icao = pms.icao(msg)
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
            tc = pms.typecode(msg)
            
            # Aircraft identification (callsign)
            if 1 <= tc <= 4:
                callsign = pms.adsb.callsign(msg)
                if callsign:
                    aircraft.callsign = callsign.strip()
                    
            # Airborne position
            elif tc in [9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 20, 21, 22]:
                alt = pms.adsb.altitude(msg)
                if alt:
                    aircraft.altitude = alt
                    
            # Airborne velocity
            elif tc == 19:
                velocity = pms.adsb.velocity(msg)
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
                "raw": msg.hex()
            }
            
        except Exception as e:
            logger.debug(f"Failed to decode ADS-B message: {e}")
            return None
            
    def get_aircraft_list(self) -> List[Dict[str, Any]]:
        """Get list of all tracked aircraft"""
        now = datetime.now()
        active_aircraft = []
        
        for icao, aircraft in self.aircraft.items():
            # Only include aircraft seen in last 60 seconds
            if (now - aircraft.last_seen).seconds < 60:
                active_aircraft.append(asdict(aircraft))
                
        return active_aircraft

class SDRMCPServer:
    """MCP Server for SDR control"""
    
    def __init__(self):
        self.server = Server("sdr-mcp")
        self.sdr: Optional[SDRDevice] = None
        self.adsb_decoder = ADSBDecoder()
        self.active_decoders: Dict[str, asyncio.Task] = {}
        
        # Analysis modules
        self.spectrum_analyzer = SpectrumAnalyzer()
        self.signal_recorder = SignalRecorder()
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
                    if not self.sdr:
                        return [TextContent(type="text", text="No SDR connected")]

                    if "adsb" in self.active_decoders:
                        return [TextContent(type="text", text="ADS-B tracking already active")]

                    # Check if frequency is in E4000 gap
                    if 1084e6 <= ADSB_FREQUENCY <= 1239e6:
                        tuner_info = await self.sdr.get_info()
                        tuner_type = tuner_info.get('tuner_type', 'Unknown')

                        # Check if this is an E4000 tuner
                        if tuner_type == '1' or 'E4000' in str(tuner_type):
                            return [TextContent(
                                type="text",
                                text="❌ ADS-B tracking not available with E4000 tuner.\n\n"
                                     "ADS-B operates at 1090 MHz, which falls in the E4000's "
                                     "L-band gap (1084-1239 MHz). The E4000 tuner cannot lock "
                                     "to frequencies in this range due to hardware limitations.\n\n"
                                     "Alternative options:\n"
                                     "• Use an RTL-SDR with R820T/R820T2 tuner (supports 24-1766 MHz continuously)\n"
                                     "• Try other aviation frequencies outside the gap:\n"
                                     "  - VHF Air Band: 118-137 MHz (voice)\n"
                                     "  - UAT (978 MHz) - also in gap, won't work\n\n"
                                     "Your E4000 works great for: FM radio, amateur radio, "
                                     "weather satellites, and most other SDR applications."
                            )]

                    # Configure for ADS-B
                    try:
                        await self.sdr.set_frequency(ADSB_FREQUENCY)
                        await self.sdr.set_sample_rate(ADSB_SAMPLE_RATE)
                        await self.sdr.set_gain('auto')
                    except Exception as e:
                        return [TextContent(
                            type="text",
                            text=f"Failed to configure SDR for ADS-B: {str(e)}\n\n"
                                 "This may be due to tuner limitations. "
                                 "Check that your SDR supports 1090 MHz."
                        )]

                    # Start ADS-B decoder task
                    self.active_decoders["adsb"] = asyncio.create_task(self._adsb_decoder_task())

                    return [TextContent(type="text", text="Started ADS-B aircraft tracking on 1090 MHz")]
                    
                elif name == "aviation_stop_tracking":
                    if "adsb" in self.active_decoders:
                        self.active_decoders["adsb"].cancel()
                        del self.active_decoders["adsb"]
                        return [TextContent(type="text", text="Stopped ADS-B tracking")]
                    else:
                        return [TextContent(type="text", text="ADS-B tracking not active")]
                        
                elif name == "aviation_get_aircraft":
                    aircraft_list = self.adsb_decoder.get_aircraft_list()
                    summary = f"Tracking {len(aircraft_list)} aircraft\n"
                    summary += f"Total messages: {self.adsb_decoder.message_count}\n\n"
                    
                    for aircraft in aircraft_list:
                        summary += f"ICAO: {aircraft['icao']}"
                        if aircraft['callsign']:
                            summary += f" ({aircraft['callsign']})"
                        if aircraft['altitude']:
                            summary += f" - Alt: {aircraft['altitude']} ft"
                        if aircraft['speed']:
                            summary += f" - Speed: {aircraft['speed']} kts"
                        summary += f" - Messages: {aircraft['message_count']}\n"
                        
                    return [TextContent(type="text", text=summary)]
                    
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
        """Background task for ADS-B decoding"""
        logger.info("Starting ADS-B decoder task")
        self.sdr.is_capturing = True
        
        try:
            while True:
                # This is a simplified decoder - real implementation would need
                # proper ADS-B frame detection and error checking
                samples = await self.sdr.read_samples(int(ADSB_SAMPLE_RATE * 0.1))  # 100ms chunks
                
                # Here you would implement actual ADS-B demodulation
                # For now, this is a placeholder
                await asyncio.sleep(0.1)
                
        except asyncio.CancelledError:
            logger.info("ADS-B decoder task cancelled")
            self.sdr.is_capturing = False
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