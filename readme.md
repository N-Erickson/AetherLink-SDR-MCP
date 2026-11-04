# AetherLink-SDR-MCP: Software Defined Radio Model Context Protocol Server

[![PyPI](https://img.shields.io/pypi/v/aetherlink)](https://pypi.org/project/aetherlink/)
[![Python](https://img.shields.io/pypi/pyversions/aetherlink)](https://pypi.org/project/aetherlink/)
[![License](https://img.shields.io/github/license/N-Erickson/AetherLink-SDR-MCP)](LICENSE)

Control Software Defined Radios and decode radio protocols through an AI-friendly Model Context Protocol interface.

## Features

- **Protocol Decoders**: ADS-B aircraft tracking, POCSAG pagers, AIS ship tracking, Meteor-M LRPT satellites, ISM band devices
- **Weather Satellites**: Meteor-M2-3/M2-4 LRPT decoding with SatDump
- **Advanced Analysis**: Real-time spectrum analysis, waterfall displays, signal detection, frequency scanning
- **Audio Recording**: Demodulate and record FM/AM audio as WAV files
- **ISM Band Scanning**: Decode 433MHz/315MHz devices (weather stations, sensors, doorbells, tire pressure monitors)
- **MCP Integration**: Seamless integration with Claude Desktop and other MCP clients
- **Satellite Pass Prediction**: Know when Meteor-M is overhead before you start a decode
- **34 MCP Tools**: Complete SDR control through natural language

## Installation

### From PyPI (Recommended)

```bash
pip install aetherlink
aetherlink --setup
```

That's it. The `--setup` command auto-configures Claude Desktop -- it detects your OS, finds the config file, and adds AetherLink without touching your other MCP servers. Then restart Claude Desktop.

Or if you prefer [uvx](https://docs.astral.sh/uv/) (no install needed):
```bash
uvx aetherlink --setup
```

> **Note:** You still need RTL-SDR system drivers installed. See [System Dependencies](#system-dependencies) below.

### Full Installer (includes system drivers)

**macOS / Linux:**
```bash
curl -fsSL https://raw.githubusercontent.com/N-Erickson/AetherLink-SDR-MCP/main/install.sh | bash
```

**Windows (PowerShell):**
```powershell
irm https://raw.githubusercontent.com/N-Erickson/AetherLink-SDR-MCP/main/install.ps1 | iex
```

The installer handles everything: system drivers, Python package, optional decoders, and Claude Desktop configuration.

> **Windows users:** You also need [Zadig](https://zadig.akeo.ie/) to replace the RTL-SDR USB driver with WinUSB. The installer will walk you through this.

### System Dependencies

AetherLink requires RTL-SDR drivers installed at the system level:

| Tool | macOS | Ubuntu/Debian | Purpose |
|------|-------|---------------|---------|
| RTL-SDR | `brew install librtlsdr` | `sudo apt install rtl-sdr librtlsdr-dev` | Required - SDR drivers (also provides `rtl_fm`) |
| dump1090 | `brew install dump1090-fa` | `sudo apt install dump1090-fa` | Optional - ADS-B aircraft tracking |
| rtl_433 | `brew install rtl_433` | `sudo apt install rtl-433` | Optional - ISM band devices |
| SatDump | `brew install satdump` | [PPA instructions](https://github.com/SatDump/SatDump) | Optional - satellite imaging |
| multimon-ng | Built from [source](https://github.com/EliasOenal/multimon-ng) by installer | `sudo apt install multimon-ng` | Optional - POCSAG pagers |
| AIS-catcher or rtl_ais | [AIS-catcher](https://github.com/jvde-github/AIS-catcher) or rtl_ais built from [source](https://github.com/dgiardini/rtl-ais) by installer | `sudo apt install rtl-ais` | Optional - AIS ship tracking |
| libhackrf | `brew install hackrf` | `sudo apt install hackrf libhackrf0` | Optional - HackRF One support (no Python package needed) |

<details>
<summary>Install from source (development)</summary>

```bash
git clone https://github.com/N-Erickson/AetherLink-SDR-MCP
cd AetherLink-SDR-MCP
python3 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"
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
- **"RTL-SDR library not available ... rtlsdr_set_dithering":** pyrtlsdr 0.4 and newer bind a function that only the rtl-sdr-blog driver provides. AetherLink pins `pyrtlsdr<0.4` for this reason; if you installed pyrtlsdr yourself, downgrade it or install the rtl-sdr-blog driver.
- **Recordings location:** files go to `~/AetherLink/recordings` by default; set `AETHERLINK_RECORDINGS_DIR` to change it.

## Supported Hardware

| Device    | RX Frequency      | TX Support | Status      | Tested |
|-----------|-------------------|------------|-------------|--------|
| RTL-SDR   | 24 MHz - 1766 MHz | ❌         | ✅ Stable   | ✅ Yes |
| HackRF One| 1 MHz - 6 GHz     | ✅         | ✅ Supported via system libhackrf | ⚠️ Not yet verified on hardware |
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
- Uses `dump1090` subprocess for demodulation and raw TCP output
- pyModeS for DF/CRC-filtered ADS-B message decoding
- Defaults to a 120-second capture; set `duration=0` to run until stopped
- Tracks callsign, altitude, speed, heading, vertical rate, message count
- Decodes positions from even/odd CPR frame pairs; with a receiver location it also decodes from single frames, shows distance and bearing, and lists nearest aircraft first
- Optional aircraft registration/type/operator lookup via hexdb.io
- Emits live tracking links for observed ICAO addresses
- **FULLY TESTED AND WORKING**

**POCSAG (152/454/929 MHz):**
- Runs `rtl_fm` piped into `multimon-ng` as a background pipeline
- Decodes 512/1200/2400 baud simultaneously (or a single rate on request)
- Alphanumeric, numeric and tone-only pages with address, function code and timestamp
- Common frequencies: 152-153 MHz, 454 MHz, 929-931 MHz (check RadioReference.com for your area)

**AIS (161.975/162.025 MHz):**
- Runs `AIS-catcher` (preferred) or `rtl_ais` as a background receiver; both channels at once
- NMEA sentences are assembled and decoded with `pyais` (position reports, static/voyage data)
- Tracks position, speed, course, heading, name, callsign, ship type, destination
- Requires a coastal or waterway location (AIS is line of sight)

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

## Available MCP Tools (34 Total)

All frequency arguments are in Hz (for example `152240000` for 152.240 MHz). Decoder tools run external programs that take exclusive control of the radio; Python-side control is paused while one runs and restored with its previous settings when it stops. Tools that return lists also return structured JSON (`structuredContent`) alongside the text.

Set `AETHERLINK_LATITUDE` and `AETHERLINK_LONGITUDE` (decimal degrees) in the MCP server's environment, or pass them as tool arguments, to get aircraft distances, single-frame position fixes and satellite pass prediction.

### Core SDR Control (9 tools)
- `sdr_list_devices` - List attached RTL-SDR and HackRF devices with serials
- `sdr_connect` - Connect to RTL-SDR or HackRF (by device index)
- `sdr_disconnect` - Stop everything and disconnect
- `sdr_set_frequency` - Set center frequency in Hz
- `sdr_set_sample_rate` - Set sample rate in Hz
- `sdr_set_gain` - Set gain (dB or 'auto')
- `sdr_set_ppm` - Set RTL-SDR frequency correction in PPM
- `sdr_set_bias_tee` - Switch antenna port power for an LNA
- `sdr_get_status` - Hardware status, running decoders, and who holds the radio

### Aviation (3 tools)
- `aviation_track_aircraft` - Start ADS-B tracking on 1090 MHz
- `aviation_stop_tracking` - Stop tracking
- `aviation_get_aircraft` - Tracked aircraft with callsign, altitude, speed, heading, position, and distance from the receiver

### Pager Decoding (4 tools)
- `pager_find_frequencies` - Scan the VHF/UHF/900 MHz paging bands for likely pager carriers
- `pager_start_decoding` - Start POCSAG decoding (`rtl_fm` + `multimon-ng`) on a frequency, optional baud rate, gain and duration
- `pager_stop_decoding` - Stop decoding
- `pager_get_messages` - Get decoded messages (newest last, filter by age)

### Marine (3 tools)
- `marine_track_vessels` - Start AIS ship tracking (`AIS-catcher` or `rtl_ais` + `pyais`), optional gain and duration
- `marine_stop_tracking` - Stop tracking
- `marine_get_vessels` - Get vessel list

### Weather Satellites (4 tools)
- `satellite_predict_passes` - Upcoming Meteor-M passes for your location (rise/set, peak elevation, azimuths)
- `satellite_decode_meteor` - Start a Meteor-M2-3/M2-4 LRPT decode with SatDump in the background
- `satellite_get_status` - Progress, results and image list of the current or last decode
- `satellite_stop_decoding` - End a decode early

### ISM Band Devices (3 tools)
- `ism_start_scanning` - Start scanning ISM bands (433/315/868/915 MHz, frequencies in Hz) with multi-frequency hopping
- `ism_stop_scanning` - Stop ISM band scanning
- `ism_get_devices` - Get detected devices (weather stations, sensors, etc.)

### Analysis (5 tools)
- `spectrum_analyze` - Analyze RF spectrum (FFT, signal detection)
- `spectrum_scan` - Scan frequency range
- `recording_start`/`recording_stop` - Record raw IQ samples (saved to `~/AetherLink/recordings/`, override with `AETHERLINK_RECORDINGS_DIR`)
- `audio_record_start`/`audio_record_stop` - Record demodulated audio as WAV (FM/AM), gap-free streaming capture

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
Find active channels first if you don't know one:
```
Find pager frequencies
```
Then:
```
Start decoding pagers on 152.240 MHz
```
Wait a few minutes (pager traffic is bursty), then:
```
Get pager messages
```
Then `Stop pager decoding` to hand the radio back. Requires `rtl_fm` (part of the RTL-SDR tools) and `multimon-ng`.

Note: Check RadioReference.com for active pager frequencies in your area.

### Track Ships (AIS)
```
Track vessels
```
After a minute or two:
```
Show me the vessels
```
Requires `AIS-catcher` or `rtl_ais` on your PATH and a location within line of sight of shipping.

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
Files saved to: `~/AetherLink/recordings/audio_YYYYMMDD_HHMMSS_XXXMHz_FM.wav`

### Record Raw IQ Samples
```
Set frequency to 103.7 MHz
Start recording with description "Raw baseband data"
```
Wait for desired duration, then:
```
Stop recording
```
Files saved to: `~/AetherLink/recordings/recording_YYYYMMDD_HHMMSS_XXXMHz.iq`

**Use case:** Advanced analysis, replay, or processing with GNU Radio/SDR#

### Meteor-M Weather Satellite (when overhead)
Find the next pass for your location:
```
When does Meteor-M2-4 pass over 44.0, -97.0 in the next 24 hours?
```
Then, a minute before it rises:
```
Decode Meteor-M2-4 satellite for 900 seconds
```
The decode runs in the background. While it runs:
```
What's the satellite decode status?
```

**Requirements:**
- SatDump installed (`brew install satdump`)
- Satellite pass overhead (`satellite_predict_passes` computes this from current Celestrak orbital elements; needs network access)
- Ideally a V-dipole antenna tuned for 137 MHz

**What you get:**
- Visible light channel images
- Infrared channel images
- Composite RGB images
- Saved to `~/AetherLink/recordings/meteor_METEOR-M2-4_*/`

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
│   ├── server.py              # Main MCP server (28 tools)
│   ├── cli.py                 # aetherlink console entry point (--setup, --version)
│   ├── __main__.py            # python -m sdr_mcp entry point
│   ├── hardware/
│   │   ├── base.py            # Abstract SDR device base class (sync reads + streaming)
│   │   ├── rtlsdr.py         # RTL-SDR interface (pyrtlsdr, gap-free async streaming)
│   │   ├── hackrf.py         # HackRF interface
│   │   └── _libhackrf.py     # ctypes binding for the system libhackrf library
│   ├── decoders/
│   │   ├── adsb.py           # ADS-B aircraft tracker (pyModeS)
│   │   ├── pocsag.py         # POCSAG pager decoder (multimon-ng output parser)
│   │   ├── ais.py            # AIS ship decoder (NMEA assembler + pyais)
│   │   ├── rtl433.py         # ISM band device decoder
│   │   ├── meteor_lrpt.py    # Meteor-M LRPT satellite decoder (SatDump)
│   │   └── satellite_passes.py # Meteor-M pass prediction (SGP4 + Celestrak TLEs)
│   ├── analysis/
│   │   └── spectrum.py        # Spectrum analysis, signal detection, IQ/audio recording
│   └── utils/
│       ├── validators.py      # Input validation and safety checks
│       └── paths.py           # Recordings directory (AETHERLINK_RECORDINGS_DIR)
├── tests/                     # All test scripts
├── pyproject.toml             # Package configuration
└── readme.md                  # This file
```

### Architecture

**Device Management:**
- RTL-SDR and subprocess decoders use **exclusive device access**
- Python SDR control and subprocess tools (dump1090, rtl_fm, rtl_433, AIS receivers, SatDump) cannot run simultaneously
- Subprocess-based decoders release the Python SDR connection, and `sdr_get_status` reports which program holds the radio
- Stopping a decoder reconnects Python SDR control with the previous frequency, sample rate and gain
- Only one Python-side capture (IQ recording, audio recording, spectrum read) runs at a time; a second start is refused instead of leaking file handles

**Decoders:**
- ADS-B: `dump1090` subprocess + pyModeS + optional hexdb.io lookup
- ISM Band: `rtl_433` subprocess with JSON output + multi-frequency hopping
- POCSAG: `rtl_fm` | `multimon-ng` pipeline, text output parsed into messages
- AIS: `AIS-catcher` or `rtl_ais` subprocess emitting NMEA, decoded with `pyais`
- Meteor-M: `satdump live` subprocess running in the background for the pass duration

**Recording:**
- IQ and audio capture use the RTL-SDR's asynchronous USB streaming, so chunks are contiguous
- Audio demodulation uses stateful vectorised filters (about 8 ms of CPU per 100 ms of RF at 2.048 Msps)
- HackRF is driven through a small ctypes binding of the system `libhackrf`; RX buffers are handed to the event loop thread-safely
