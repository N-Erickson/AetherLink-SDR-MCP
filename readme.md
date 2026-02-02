# AetherLink-SDR-MCP: Software Defined Radio Model Context Protocol Server

Control Software Defined Radios and decode radio protocols through an AI-friendly Model Context Protocol interface.

## 🚀 Features

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
| Nooelec E4000| 55 MHz - 2300 MHz | ❌      | ✅ Stable   | ✅ Yes |



## 📊 Protocol Support

| Protocol    | Description          | Status      |
|-------------|---------------------|-------------|
| **ADS-B**   | Aircraft tracking   | ✅ Ready    |
| **POCSAG**  | Pager decoding      | ✅ Ready    |
| **AIS**     | Ship tracking       | ✅ Ready    |
| **NOAA APT**| Weather satellites  | ✅ Ready    |

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
- Common frequencies: 152.240 MHz, 454 MHz, 929-931 MHz

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

### Decode Pagers
```
Set frequency to 152.240 MHz
Start paging decoder at 1200 baud
```
Wait a few minutes, then:
```
Get pager messages
```

Note: Check RadioReference.com for active pager frequencies in your area.

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
