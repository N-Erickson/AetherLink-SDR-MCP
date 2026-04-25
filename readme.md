# AetherLink-SDR-MCP: Software Defined Radio Model Context Protocol Server

Control Software Defined Radios and decode radio protocols through an AI-friendly Model Context Protocol interface.

## Features

- **Protocol Decoders**: ADS-B aircraft tracking, POCSAG pagers, AIS ship tracking, Meteor-M LRPT satellites, ISM band devices
- **Weather Satellites**: Meteor-M2-3/M2-4 LRPT decoding with SatDump
- **Advanced Analysis**: Real-time spectrum analysis, waterfall displays, signal detection, frequency scanning
- **Audio Recording**: Demodulate and record FM/AM audio as WAV files
- **ISM Band Scanning**: Decode 433MHz/315MHz devices (weather stations, sensors, doorbells, tire pressure monitors)
- **MCP Integration**: Seamless integration with Claude Desktop and other MCP clients
- **26 MCP Tools**: Complete SDR control through natural language

## Installation

### Quick Install (Recommended)

**macOS / Linux:**
```bash
curl -fsSL https://raw.githubusercontent.com/N-Erickson/AetherLink-SDR-MCP/main/install.sh | bash
```

**Windows (PowerShell):**
```powershell
irm https://raw.githubusercontent.com/N-Erickson/AetherLink-SDR-MCP/main/install.ps1 | iex
```

The installer will:
- Install RTL-SDR drivers and tools (via Homebrew / apt / manual links)
- Prompt for optional decoders (rtl_433, SatDump)
- Clone the repo, create a Python venv, and install dependencies
- Offer to configure Claude Desktop automatically

> **Windows users:** You also need [Zadig](https://zadig.akeo.ie/) to replace the RTL-SDR USB driver with WinUSB. The installer will walk you through this.

### Manual Install

<details>
<summary>Click to expand manual installation steps</summary>

**Prerequisites:** Python 3.10+, Git

**1. System dependencies:**

| Tool | macOS | Ubuntu/Debian | Purpose |
|------|-------|---------------|---------|
| RTL-SDR | `brew install rtl-sdr` | `sudo apt install rtl-sdr librtlsdr-dev` | Required - SDR drivers |
| rtl_433 | `brew install rtl_433` | `sudo apt install rtl-433` | Optional - ISM band devices |
| SatDump | `brew install satdump` | [PPA instructions](https://github.com/SatDump/SatDump) | Optional - satellite imaging |
| multimon-ng | Build from [source](https://github.com/EliasOenal/multimon-ng) | `sudo apt install multimon-ng` | Optional - POCSAG pagers |

**2. Install AetherLink:**
```bash
git clone https://github.com/N-Erickson/AetherLink-SDR-MCP
cd AetherLink-SDR-MCP
python3 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -e .
```

</details>

## Quick Start

### 1. Configure Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or equivalent:

```json
{
  "mcpServers": {
    "aetherlink": {
      "command": "/path/to/AetherLink-SDR-MCP/venv/bin/python",
      "args": ["-m", "sdr_mcp.server"],
      "cwd": "/path/to/AetherLink-SDR-MCP"
    }
  }
}
```

**Important:** Replace `/path/to/` with your actual path. Run `which python` with your venv activated to find the correct Python path.

### 2. Restart Claude Desktop

Quit completely (Cmd+Q) and restart Claude Desktop to load the MCP server.

### 3. Test the Connection

In Claude Desktop:
```
Connect to my RTL-SDR
```

You should see: "Successfully connected to RTL-SDR"

### Troubleshooting

- **Server not appearing:** Check logs at `~/Library/Logs/Claude/mcp-server-aetherlink.log`
- **"Device busy" errors:** Only one program can use the SDR at a time. Close GQRX, SDR#, or other SDR software.
- **Linux device permissions:** Add udev rules for RTL-SDR (`/etc/udev/rules.d/20-rtlsdr.rules`) and blacklist the `dvb_usb_rtl28xxu` kernel module.
- **E4000 tuner gap:** Frequencies 1084-1239 MHz may not work on E4000-based dongles. This is normal hardware behavior.

## Supported Hardware

| Device    | RX Frequency      | TX Support | Status      | Tested |
|-----------|-------------------|------------|-------------|--------|
| RTL-SDR   | 24 MHz - 1766 MHz | ❌         | ✅ Stable   | ✅ Yes |
| HackRF One| 1 MHz - 6 GHz     | ✅         | ✅ Working  | ⚠️ Limited |
| Nooelec E4000| 55 MHz - 2300 MHz | ❌      | ✅ Stable   | ✅ Yes |



## Protocol Support

| Protocol    | Description          | Status      |
|-------------|---------------------|-------------|
| **ADS-B**   | Aircraft tracking   | ✅ Ready    |
| **POCSAG**  | Pager decoding      | ✅ Ready    |
| **AIS**     | Ship tracking       | ✅ Ready    |
| **Meteor-M LRPT**| Weather satellites (M2-3, M2-4) | ✅ Ready |
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

**Meteor-M LRPT (137 MHz):**
- Uses `satdump` subprocess for OQPSK demodulation
- Digital LRPT transmission with error correction
- Decodes visible and infrared channels
- Active satellites: Meteor-M2-3 (137.9 MHz), Meteor-M2-4 (137.9 MHz primary, 137.1 MHz backup)
- **CURRENT WEATHER SATELLITE STANDARD** (replaced NOAA APT)

**ISM Band (433/315/868/915 MHz):**
- Uses `rtl_433` subprocess for decoding
- Multi-frequency hopping support
- Decodes 200+ device types automatically
- Weather stations, sensors, doorbells, tire pressure monitors, remote controls
- Common frequencies: 433.92 MHz (EU/Asia), 315 MHz (NA), 868 MHz (EU), 915 MHz (NA)

## Available MCP Tools (26 Total)

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

### Weather Satellites (1 tool)
- `satellite_decode_meteor` - Decode Meteor-M2-3/M2-4 LRPT satellite pass

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

## Usage Examples

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

### Meteor-M Weather Satellite (when overhead)
```
Decode Meteor-M2-4 satellite for 600 seconds
```

**Requirements:**
- SatDump installed (`brew install satdump`)
- Satellite pass overhead (use tools like Gpredict, N2YO, or Heavens-Above to predict passes)
- Ideally a V-dipole antenna tuned for 137 MHz

**What you get:**
- Visible light channel images
- Infrared channel images
- Composite RGB images
- Saved to `/tmp/sdr_recordings/meteor_METEOR-M2-4_*/`

**Tips:**
- Meteor-M2-4 transmits on 137.9 MHz (primary) or 137.1 MHz (backup)
- Best results with satellite elevation >30°
- Full pass is typically 10-15 minutes
- Use higher gain (40-49 dB) for weak signals

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

## Development

### Project Structure

```
AetherLink-SDR-MCP/
├── sdr_mcp/
│   ├── server.py              # Main MCP server (26 tools)
│   ├── __main__.py            # python -m sdr_mcp entry point
│   ├── hardware/
│   │   ├── base.py            # Abstract SDR device base class
│   │   ├── rtlsdr.py         # RTL-SDR interface
│   │   └── hackrf.py         # HackRF interface
│   ├── decoders/
│   │   ├── pocsag.py         # POCSAG pager decoder
│   │   ├── ais.py            # AIS ship decoder
│   │   ├── rtl433.py         # ISM band device decoder
│   │   └── meteor_lrpt.py    # Meteor-M LRPT satellite decoder
│   ├── analysis/
│   │   └── spectrum.py        # Spectrum analysis, signal detection
│   └── utils/
│       └── validators.py      # Input validation and safety checks
├── tests/                     # All test scripts
├── pyproject.toml             # Package configuration
└── readme.md                  # This file
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
