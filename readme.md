# AetherLink-SDR-MCP: Software Defined Radio Model Context Protocol Server

Control Software Defined Radios and decode radio protocols through an AI-friendly Model Context Protocol interface.

## 🚀 Features

- **Direct Hardware Control**: RTL-SDR and HackRF One
- **Protocol Decoders**: ADS-B aircraft tracking, POCSAG pagers, AIS ship tracking, NOAA weather satellites
- **Advanced Analysis**: Real-time spectrum analysis, waterfall displays, signal detection, frequency scanning
- **Audio Recording**: Demodulate and record FM/AM audio as WAV files
- **MCP Integration**: Seamless integration with Claude Desktop and other MCP clients
- **23 MCP Tools**: Complete SDR control through natural language

## 📦 Installation

### Prerequisites

**1. RTL-SDR Drivers:**
```bash
# macOS
brew install librtlsdr

# Ubuntu/Debian
sudo apt-get install rtl-sdr librtlsdr-dev

# Windows
# Download from https://osmocom.org/projects/rtl-sdr/wiki
```

**2. RTL-SDR Tools:**
```bash
# macOS
brew install rtl-sdr

# Ubuntu/Debian
sudo apt-get install rtl-sdr

# Includes: rtl_fm, rtl_adsb, rtl_test
```

**3. POCSAG Decoder (optional but recommended):**
```bash
# Clone and build multimon-ng
cd /tmp
git clone https://github.com/EliasOenal/multimon-ng.git
cd multimon-ng
mkdir build && cd build
cmake ..
make

# Binary will be at: /tmp/multimon-ng/build/multimon-ng
```

**4. Python 3.10+**

### Install from Source

```bash
# Clone the repository
git clone https://github.com/yourusername/AetherLink-SDR-MCP
cd AetherLink-SDR-MCP

# Create virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -e .
```

### Required Python Packages

The following are installed automatically:
- `pyrtlsdr` - RTL-SDR hardware interface
- `numpy` - Signal processing
- `scipy` - Filtering and demodulation
- `mcp` - Model Context Protocol server
- `pyModeS` - ADS-B decoding

## 🎯 Quick Start

### 1. Configure Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or equivalent:

```json
{
  "mcpServers": {
    "sdr": {
      "command": "/Users/YOUR_USERNAME/Documents/GitHub/AetherLink-SDR-MCP/venv/bin/python",
      "args": ["-m", "sdr_mcp.server"],
      "env": {}
    }
  }
}
```

**Important:** Use absolute paths for the Python interpreter.

### 2. Restart Claude Desktop

Quit and restart Claude Desktop to load the MCP server.

### 3. Test the Connection

In Claude Desktop:
```
Connect to my RTL-SDR
```

You should see: "Successfully connected to RTL-SDR"

## 📡 Supported Hardware

| Device    | RX Frequency      | TX Support | Status      | Tested |
|-----------|-------------------|------------|-------------|--------|
| RTL-SDR   | 24 MHz - 1766 MHz | ❌         | ✅ Stable   | ✅ Yes |
| HackRF One| 1 MHz - 6 GHz     | ✅         | ✅ Working  | ⚠️ Limited |

**Tested RTL-SDR:**
- Rafael Micro R820T tuner
- 24-1766 MHz continuous coverage
- No L-band gap (unlike E4000 tuner)

## 📊 Protocol Support

| Protocol    | Description          | Status      | Real-World Tested |
|-------------|---------------------|-------------|-------------------|
| **ADS-B**   | Aircraft tracking   | ✅ **WORKING** | ✅ 244 msgs/20s, 104 aircraft |
| **POCSAG**  | Pager decoding      | ✅ Ready    | ⏰ Needs patience |
| **AIS**     | Ship tracking       | ✅ Ready    | ❌ No ships nearby |
| **NOAA APT**| Weather satellites  | ✅ Ready    | ⏰ Needs satellite pass |

### Protocol Details

**ADS-B (1090 MHz):**
- Uses `rtl_adsb` subprocess for demodulation
- pyModeS for message decoding
- Tracks aircraft position, speed, altitude, callsign
- **FULLY TESTED AND WORKING**

**POCSAG (152/454/929 MHz):**
- Uses `multimon-ng` for professional decoding
- Supports 512/1200/2400 baud
- Alphanumeric and numeric messages
- 13 active frequencies found in Sioux Falls, SD

**AIS (161.975/162.025 MHz):**
- GMSK demodulation (simplified)
- Decodes ship position, speed, type
- Requires coastal location

**NOAA APT (137 MHz):**
- AM demodulation
- Decodes weather satellite images
- Requires satellite overhead (2 passes/day)

## 🛠️ Available MCP Tools (23 Total)

### Core SDR Control (5 tools)
- `sdr_connect` - Connect to RTL-SDR or HackRF
- `sdr_disconnect` - Disconnect from SDR
- `sdr_set_frequency` - Set center frequency in Hz
- `sdr_set_gain` - Set gain (dB or 'auto')
- `sdr_get_status` - Get hardware status

### Aviation (3 tools)
- `aviation_track_aircraft` - Start ADS-B tracking on 1090 MHz
- `aviation_stop_tracking` - Stop tracking
- `aviation_get_aircraft` - Get list of tracked aircraft

### Pager Decoding (3 tools)
- `pager_start_decoding` - Start POCSAG decoder
- `pager_stop_decoding` - Stop decoding
- `pager_get_messages` - Get decoded messages

### Marine (3 tools)
- `marine_track_vessels` - Start AIS ship tracking
- `marine_stop_tracking` - Stop tracking
- `marine_get_vessels` - Get vessel list

### Satellite (1 tool)
- `satellite_decode_noaa` - Decode NOAA weather satellite pass

### Analysis (5 tools)
- `spectrum_analyze` - Analyze RF spectrum (FFT, signal detection)
- `spectrum_scan` - Scan frequency range
- `recording_start`/`recording_stop` - Record raw IQ samples (saved to `/tmp/sdr_recordings/`)
- `audio_record_start`/`audio_record_stop` - Record demodulated audio as WAV (FM/AM)

### HackRF Transmit (2 tools)
- `hackrf_set_tx_gain` - Set transmit gain
- `signal_generator` - Generate and transmit signals

## 📖 Usage Examples

### Track Aircraft
```
Track aircraft in my area
```
After 30-60 seconds:
```
Show me the aircraft
```

### Decode Pagers (Sioux Falls specific)
```
Set frequency to 167.8625 MHz
Start paging decoder at 1200 baud
```
Wait a few minutes, then:
```
Get pager messages
```

### Analyze Spectrum
```
Set frequency to 100 MHz
Analyze the spectrum
```

### Scan for Signals
```
Scan from 430 MHz to 440 MHz with 1 MHz steps
```

### Record Audio from FM Radio
```
Set frequency to 103.7 MHz
Start audio recording with FM modulation and description "Local FM station"
```
Wait for desired duration (e.g., 30 seconds), then:
```
Stop audio recording
```
Files saved to: `/tmp/sdr_recordings/audio_YYYYMMDD_HHMMSS_XXXMHz_FM.wav`

**Features:**
- FM demodulation with 75μs de-emphasis (US standard)
- AM demodulation with envelope detection
- 48 kHz WAV output (playable with any audio player)
- Automatic normalization to prevent clipping

### Record Raw IQ Samples
```
Set frequency to 103.7 MHz
Start recording with description "Raw baseband data"
```
Wait for desired duration, then:
```
Stop recording
```
Files saved to: `/tmp/sdr_recordings/recording_YYYYMMDD_HHMMSS_XXXMHz.iq`

**Use case:** Advanced analysis, replay, or processing with GNU Radio/SDR#

### NOAA Satellite (when overhead)
```
Decode NOAA-19 satellite for 600 seconds
```

## 🧪 Testing

### CI Tests (Automated)

[![Sanity Checks](https://github.com/yourusername/AetherLink-SDR-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/yourusername/AetherLink-SDR-MCP/actions/workflows/ci.yml)

**What CI tests:**
- ✅ Code imports without errors
- ✅ Server initializes properly
- ✅ DSP algorithms (FM/AM demod, AGC, resampling)
- ✅ Decoders instantiate correctly

**What CI does NOT test:**
- ❌ Real SDR hardware (no USB on runners)
- ❌ RF signal reception (no antennas in data centers)
- ❌ Actual decoder performance

Run CI tests locally:
```bash
pytest tests/test_sanity.py -v
```

### Manual Tests (Requires Hardware)

Located in `tests/manual/` - **cannot run in CI**

**ADS-B:**
- `test_complete_adsb.py` - Full end-to-end test ✅ WORKING

**Audio Recording:**
- `test_audio_recording.py` - Basic functionality
- `test_smooth_audio.py` - Quality verification

**POCSAG Pagers:**
- `scan_sioux_falls_pagers.py` - Scan 13 local frequencies
- `monitor_pagers.sh` - Long-term monitor (run for hours)

**NOAA Satellites:**
- `test_real_noaa.py` - Real satellite pass decoding

**General:**
- `test_all_functions.py` - All core SDR functions

### Running Manual Tests

```bash
# Requires RTL-SDR/HackRF hardware connected
cd tests/manual

# ADS-B test (needs aircraft overhead)
python test_complete_adsb.py

# Audio quality test (needs FM station)
python test_smooth_audio.py

# See tests/manual/README.md for full details
```

## 📍 Location-Specific Information

### Sioux Falls, SD

**Coordinates:** 43.5446°N, 96.7311°W

**Active Pager Frequencies (13 found):**
- 929.000 MHz - 900 MHz Paging (356 signals) - MOST ACTIVE
- 931.000 MHz - 900 MHz Paging (331 signals)
- 152.480 MHz - VHF Paging (330 signals)
- 454.000 MHz - UHF Paging (327 signals)
- **167.8625 MHz - VA Medical Center** (confirmed)
- Plus 8 more active frequencies

**NOAA Satellite Pass Lookup:**
- https://www.n2yo.com/passes/?s=33591&lat=43.5446&lng=-96.7311
- https://www.heavens-above.com/ (enter "Sioux Falls, SD")

See [SIOUX_FALLS_RESULTS.md](SIOUX_FALLS_RESULTS.md) for complete details.

## 🔧 Development

### Project Structure

```
AetherLink-SDR-MCP/
├── sdr_mcp/
│   ├── server.py              # Main MCP server (21 tools)
│   ├── hardware/
│   │   ├── rtlsdr.py         # RTL-SDR interface
│   │   └── hackrf.py         # HackRF interface
│   ├── decoders/
│   │   ├── pocsag.py         # POCSAG pager decoder
│   │   ├── ais.py            # AIS ship decoder
│   │   └── noaa_apt.py       # NOAA satellite decoder
│   └── analysis/
│       └── spectrum.py        # Spectrum analysis, signal detection
├── tests/                     # All test scripts
└── README.md                  # This file
```

### Architecture

**Device Management:**
- RTL-SDR and ADS-B use **exclusive device access**
- Python SDR control and rtl_adsb cannot run simultaneously
- `aviation_track_aircraft` automatically disconnects Python SDR
- `aviation_stop_tracking` reconnects Python SDR

**Decoders:**
- ADS-B: `rtl_adsb` subprocess + pyModeS
- POCSAG: `rtl_fm` + `multimon-ng` pipeline
- AIS: Built-in GMSK demodulator (simplified)
- NOAA: Built-in AM demodulator + sync detection

## 🐛 Known Issues

1. **USB Device Conflict**: Only one process can access RTL-SDR at a time
   - Solution: Stop other processes before connecting

2. **"PLL not locked" Warning**: Harmless warning from RTL-SDR library
   - Solution: Ignore, doesn't affect functionality

3. **POCSAG Messages Intermittent**: Pagers only transmit when someone gets paged
   - Solution: Run long-term monitor (hours/days)

4. **NOAA Requires Satellite Overhead**: Only works during passes
   - Solution: Check n2yo.com for pass times

## 📚 Documentation

- [FUNCTION_TEST_GUIDE.md](FUNCTION_TEST_GUIDE.md) - Complete testing guide
- [SIOUX_FALLS_RESULTS.md](SIOUX_FALLS_RESULTS.md) - Local testing results
- [REAL_WORLD_TESTING.md](REAL_WORLD_TESTING.md) - RF validation results

## 🤝 Contributing

This project is in active development. Current status:
- ✅ ADS-B fully working and tested
- ✅ POCSAG decoder integrated (multimon-ng)
- ✅ 13 active pager frequencies identified (Sioux Falls)
- ✅ All MCP tools implemented
- ⏰ Waiting for real pager messages / satellite passes

## 📄 License

MIT License - See LICENSE file for details

## 🙏 Acknowledgments

- RTL-SDR community
- pyModeS for ADS-B decoding
- multimon-ng for POCSAG decoding
- Claude Desktop MCP integration

## 🔗 Links

- RTL-SDR: https://www.rtl-sdr.com/
- Model Context Protocol: https://modelcontextprotocol.io/
- Claude Desktop: https://claude.ai/desktop
- multimon-ng: https://github.com/EliasOenal/multimon-ng

---

**Status:** Active development - ADS-B working, pager/satellite ready for testing
