# AetherLink-SDR-MCP: Software Defined Radio Model Context Protocol Server

Control Software Defined Radios and decode radio protocols through an AI-friendly Model Context Protocol interface.

## 🚀 Features

- **Protocol Decoders**: ADS-B aircraft tracking, POCSAG pagers, AIS ship tracking, NOAA weather satellites, ISM band devices
- **Advanced Analysis**: Real-time spectrum analysis, waterfall displays, signal detection, frequency scanning
- **Audio Recording**: Demodulate and record FM/AM audio as WAV files
- **ISM Band Scanning**: Decode 433MHz/315MHz devices (weather stations, sensors, doorbells, tire pressure monitors)
- **MCP Integration**: Seamless integration with Claude Desktop and other MCP clients
- **26 MCP Tools**: Complete SDR control through natural language

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

**3. rtl_433 for ISM band decoding (optional but recommended):**
```bash
# macOS
brew install rtl_433

# Ubuntu/Debian
sudo apt-get install rtl-433

# Enables decoding of 433MHz/315MHz devices
```

**4. POCSAG Decoder (optional):**
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

**5. Python 3.10+**

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
| **ISM Band**| 433MHz/315MHz devices | ✅ Ready  |

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

**ISM Band (433/315/868/915 MHz):**
- Uses `rtl_433` subprocess for decoding
- Multi-frequency hopping support
- Decodes 200+ device types automatically
- Weather stations, sensors, doorbells, tire pressure monitors, remote controls
- Common frequencies: 433.92 MHz (EU/Asia), 315 MHz (NA), 868 MHz (EU), 915 MHz (NA)

## 🛠️ Available MCP Tools (26 Total)

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

### ISM Band Devices (3 tools)
- `ism_start_scanning` - Start scanning ISM bands (433/315/868/915 MHz) with multi-frequency hopping
- `ism_stop_scanning` - Stop ISM band scanning
- `ism_get_devices` - Get detected devices (weather stations, sensors, etc.)

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

### Scan ISM Band Devices
```
Start ISM scanning on 433.92 MHz and 315 MHz with 30 second hop interval
```
Wait 1-2 minutes for devices to transmit, then:
```
Show me the ISM devices
```

**Common devices detected:**
- Weather stations (temperature, humidity, wind, rain)
- Wireless thermometers
- Tire pressure monitoring systems (TPMS)
- Door/window sensors
- Doorbells and remote controls
- Soil moisture sensors

**Tips:**
- Weather stations typically transmit every 30-60 seconds
- 433.92 MHz is common in Europe/Asia
- 315 MHz is common in North America
- Try different frequency combinations: `[433.92, 315]` or `[868, 915]`
- Increase hop interval for more dwell time per frequency

## 🔧 Development

### Project Structure

```
AetherLink-SDR-MCP/
├── sdr_mcp/
│   ├── server.py              # Main MCP server (26 tools)
│   ├── hardware/
│   │   ├── rtlsdr.py         # RTL-SDR interface
│   │   └── hackrf.py         # HackRF interface
│   ├── decoders/
│   │   ├── pocsag.py         # POCSAG pager decoder
│   │   ├── ais.py            # AIS ship decoder
│   │   ├── noaa_apt.py       # NOAA satellite decoder
│   │   └── rtl433.py         # ISM band device decoder
│   └── analysis/
│       └── spectrum.py        # Spectrum analysis, signal detection
├── tests/                     # All test scripts
└── README.md                  # This file
```

### Architecture

**Device Management:**
- RTL-SDR and subprocess decoders use **exclusive device access**
- Python SDR control and subprocess tools (rtl_adsb, rtl_433) cannot run simultaneously
- Subprocess-based decoders automatically disconnect Python SDR
- Stopping decoder reconnects Python SDR control

**Decoders:**
- ADS-B: `rtl_adsb` subprocess + pyModeS
- ISM Band: `rtl_433` subprocess with JSON output + multi-frequency hopping
- POCSAG: `rtl_fm` + `multimon-ng` pipeline
- AIS: Built-in GMSK demodulator (simplified)
- NOAA: Built-in AM demodulator + sync detection
