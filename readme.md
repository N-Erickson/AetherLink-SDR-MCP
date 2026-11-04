# AetherLink-SDR-MCP: Software Defined Radio Model Context Protocol Server

[![PyPI](https://img.shields.io/pypi/v/aetherlink)](https://pypi.org/project/aetherlink/)
[![Python](https://img.shields.io/pypi/pyversions/aetherlink)](https://pypi.org/project/aetherlink/)
[![License](https://img.shields.io/github/license/N-Erickson/AetherLink-SDR-MCP)](LICENSE)

Control Software Defined Radios and decode radio protocols through an AI-friendly Model Context Protocol interface.

## Features

- **Protocol Decoders**: ADS-B aircraft tracking, POCSAG pagers, AIS ship tracking, Meteor-M LRPT satellites, ISM band devices
- **Built-in pager and AIS decoders**: POCSAG and AIS are decoded in Python from the dongle's own samples; no multimon-ng or AIS-catcher required
- **Zero setup in daily use**: the first RTL-SDR is opened automatically and the receiver location is detected automatically
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
| dump1090 | `brew install dump1090-fa` | `sudo apt install dump1090-fa` | Optional - preferred ADS-B receiver; `rtl_adsb` from the RTL-SDR drivers is used when it is absent |
| rtl_433 | `brew install rtl_433` | `sudo apt install rtl-433` | Optional - ISM band devices |
| SatDump | `brew install satdump` | [PPA instructions](https://github.com/SatDump/SatDump) | Optional - satellite imaging |
| multimon-ng | Built from [source](https://github.com/EliasOenal/multimon-ng) by installer | `sudo apt install multimon-ng` | Optional - alternative external POCSAG engine (`engine: multimon`); the built-in decoder needs nothing |
| AIS-catcher or rtl_ais | [AIS-catcher](https://github.com/jvde-github/AIS-catcher) or rtl_ais built from [source](https://github.com/dgiardini/rtl-ais) by installer | `sudo apt install rtl-ais` | Optional - alternative external AIS engine (`engine: external`); the built-in receiver needs nothing |
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

### 1. Point Claude Desktop at AetherLink

```bash
aetherlink --setup
```

This writes the `aetherlink` entry into your Claude Desktop config (macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`, Windows: `%APPDATA%\Claude\claude_desktop_config.json`, Linux: `~/.config/Claude/claude_desktop_config.json`) without touching your other MCP servers, and points it at the exact install you just made. The installers run this step for you.

<details>
<summary>Manual configuration</summary>

If you prefer to edit the config yourself, use the interpreter that has AetherLink installed:

```json
{
  "mcpServers": {
    "aetherlink": {
      "command": "/path/to/python",
      "args": ["-m", "sdr_mcp"]
    }
  }
}
```

</details>

### 2. Restart Claude Desktop

Quit completely (Cmd+Q on macOS) and start it again so the server is loaded.

### 3. Try it

Plug in the RTL-SDR and ask, in Claude Desktop:
```
What aircraft are overhead right now?
```
or
```
Analyze the spectrum at 100 MHz
```

There is no connect step: the first attached RTL-SDR is opened automatically, and the receiver location is detected automatically. `Connect to my HackRF` is only needed for HackRF hardware or to pick a specific dongle.

### Troubleshooting

- **Server not appearing:** Check logs at `~/Library/Logs/Claude/mcp-server-aetherlink.log`
- **"Device busy" errors:** Only one program can use the SDR at a time. Close GQRX, SDR#, or other SDR software.
- **Linux device permissions:** Add udev rules for RTL-SDR (`/etc/udev/rules.d/20-rtlsdr.rules`) and blacklist the `dvb_usb_rtl28xxu` kernel module.
- **E4000 tuner gap:** Frequencies 1084-1239 MHz may not work on E4000-based dongles. This is normal hardware behavior.
- **"No RTL-SDR was detected":** check `sdr_list_devices`; on Linux make sure the DVB kernel module is blacklisted (the installer does this); on Windows the WinUSB driver must be installed with Zadig.
- **Weak ADS-B reception:** 1090 MHz signals are weak. A short indoor antenna only hears aircraft nearly overhead; a 1090 MHz antenna near a window or outdoors makes the biggest difference. `aviation_get_aircraft` tells you which receiver program is running and whether it switched to `rtl_adsb`.
- **"RTL-SDR library not available ... rtlsdr_set_dithering":** pyrtlsdr 0.4 and newer bind a function that only the rtl-sdr-blog driver provides. AetherLink pins `pyrtlsdr<0.4` for this reason; if you installed pyrtlsdr yourself, downgrade it or install the rtl-sdr-blog driver.
- **Recordings location:** files go to `~/AetherLink/recordings` by default; set `AETHERLINK_RECORDINGS_DIR` to change it.
- **Testing without hardware:** set `AETHERLINK_MOCK_SDR=1` to use a mock RTL-SDR; `AETHERLINK_MOCK_IQ_FILE` can point at a `.npy` file of complex samples that the mock plays back in a loop (useful for exercising the built-in decoders).

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
| **POCSAG**  | Pager decoding      | ✅ Built-in decoder, no extra software |
| **AIS**     | Ship tracking       | ✅ Built-in receiver, no extra software |
| **Meteor-M LRPT**| Weather satellites (M2-3, M2-4) | ✅ Ready (SatDump, runs in the background with pass prediction) |
| **ISM Band**| 433MHz/315MHz devices | ✅ Ready  |

### Protocol Details

**ADS-B (1090 MHz):**
- Uses `dump1090` when installed, otherwise `rtl_adsb` from the RTL-SDR drivers; if dump1090 hears nothing for 45 s (for example when strong broadband noise swamps its demodulator) the tool switches to `rtl_adsb` automatically. `engine: dump1090` / `rtl_adsb` forces one.
- Starts at maximum tuner gain (49.6 dB) and lets dump1090-fa adapt it downwards; 1090 MHz signals are weak, and a short, cheap antenna indoors only hears aircraft nearly overhead
- pyModeS for DF/CRC-filtered ADS-B message decoding
- Defaults to a 120-second capture; set `duration=0` to run until stopped
- Tracks callsign, altitude, speed, heading, vertical rate, message count
- Decodes positions from even/odd CPR frame pairs; with a receiver location it also decodes from single frames, shows distance and bearing, and lists nearest aircraft first
- Optional aircraft registration/type/operator lookup via hexdb.io
- Emits live tracking links for observed ICAO addresses
- Verified live: aircraft decoded with positions matching a public ADS-B feed

**POCSAG (152/454/929 MHz):**
- Built-in decoder: channel filter, FM discriminator, automatic frequency control (absorbs dongle ppm error), bit timing recovery, BCH(31,21) error correction
- Decodes 512/1200/2400 baud simultaneously (or a single rate on request); alphanumeric, numeric and tone-only pages
- Reports whether a signal is present on the channel and the measured carrier offset, so an empty result is explainable
- `pager_find_frequencies` scans the paging bands for active carriers; common allocations are 152-153 MHz, 454 MHz, 929-931 MHz
- `engine: multimon` switches to an external `rtl_fm | multimon-ng` pipeline if you prefer it

**AIS (161.975/162.025 MHz):**
- Built-in receiver: both channels from one capture, burst detection, GMSK demodulation, NRZI/HDLC deframing with CRC-16, shared frequency correction
- Frames are armored into NMEA and decoded with `pyais` (position reports, static/voyage data); vessels get MarineTraffic links
- Tracks position, speed, course, heading, name, callsign, ship type, destination
- Requires a coastal or waterway location (AIS is line of sight)
- `engine: external` switches to `AIS-catcher` or `rtl_ais` if installed

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

All frequency arguments are in Hz (for example `152240000` for 152.240 MHz). Tools that need the radio open it automatically (first RTL-SDR); `sdr_connect` is only needed to pick a specific device or a HackRF. ADS-B, ISM and Meteor decoding run external programs (dump1090, rtl_433, SatDump) that take exclusive control of the radio; Python-side control is paused while one runs and restored with its previous settings when it stops. Pager and AIS decoding are built in and keep the radio under Python control. Tools that return lists also return structured JSON (`structuredContent`) alongside the text.

The receiver location is detected automatically (city-level IP geolocation, cached, then refined from decoded aircraft positions) and used for aircraft distances, single-frame position fixes and satellite pass prediction. Override it with `latitude`/`longitude` arguments or the `AETHERLINK_LATITUDE` / `AETHERLINK_LONGITUDE` environment variables.

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
- `pager_start_decoding` - Start built-in POCSAG decoding on a frequency; optional baud rate, gain, duration and `engine` (`multimon` for the external pipeline)
- `pager_stop_decoding` - Stop decoding
- `pager_get_messages` - Get decoded messages (newest last, filter by age)

### Marine (3 tools)
- `marine_track_vessels` - Start built-in AIS ship tracking (both channels); optional gain, duration and `engine` (`external` for AIS-catcher / rtl_ais)
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
The report says whether a carrier is present on the channel, so "no messages" and "nothing transmitting" are distinguishable. `Stop pager decoding` releases the channel. Nothing beyond the RTL-SDR driver is required.

Note: Check RadioReference.com for active pager frequencies in your area.

### Track Ships (AIS)
```
Track vessels
```
After a minute or two:
```
Show me the vessels
```
Nothing beyond the RTL-SDR driver is required; you do need a location within line of sight of shipping.

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
│   ├── server.py              # Main MCP server (34 tools)
│   ├── cli.py                 # aetherlink console entry point (--setup, --version)
│   ├── __main__.py            # python -m sdr_mcp entry point
│   ├── hardware/
│   │   ├── base.py            # Abstract SDR device base class (sync reads + streaming)
│   │   ├── rtlsdr.py         # RTL-SDR interface (pyrtlsdr, gap-free async streaming)
│   │   ├── hackrf.py         # HackRF interface
│   │   └── _libhackrf.py     # ctypes binding for the system libhackrf library
│   ├── decoders/
│   │   ├── adsb.py           # ADS-B aircraft tracker (pyModeS) + cpr.py position decoding
│   │   ├── pocsag.py         # POCSAG message store and multimon-ng output parser
│   │   ├── pocsag_native.py  # Built-in POCSAG decoder (IQ in, messages out)
│   │   ├── ais.py            # AIS NMEA assembler + pyais vessel tracker
│   │   ├── ais_native.py     # Built-in AIS receiver (IQ in, NMEA out)
│   │   ├── rtl433.py         # ISM band device decoder
│   │   ├── meteor_lrpt.py    # Meteor-M LRPT satellite decoder (SatDump)
│   │   └── satellite_passes.py # Meteor-M pass prediction (SGP4 + Celestrak TLEs)
│   ├── analysis/
│   │   ├── spectrum.py        # Spectrum analysis, signal detection, IQ/audio recording
│   │   └── channelizer.py     # Shared DSP: channel selection, FM discriminator, bit slicer
│   └── utils/
│       ├── validators.py      # Input validation and safety checks
│       ├── location.py        # Automatic receiver location
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
- POCSAG: built-in decoder in `decoders/pocsag_native.py` (channelizer, FM discriminator, AFC, bit slicer, BCH); external `rtl_fm | multimon-ng` pipeline as an alternative engine
- AIS: built-in receiver in `decoders/ais_native.py` (both channels, burst detection, GMSK, HDLC/CRC) producing NMEA for `pyais`; `AIS-catcher` / `rtl_ais` as an alternative engine
- Location: `utils/location.py` resolves arguments, environment, cache, IP geolocation, then an estimate from aircraft positions
- Meteor-M: `satdump live` subprocess running in the background for the pass duration

**Recording:**
- IQ and audio capture use the RTL-SDR's asynchronous USB streaming, so chunks are contiguous
- Audio demodulation uses stateful vectorised filters (about 8 ms of CPU per 100 ms of RF at 2.048 Msps)
- HackRF is driven through a small ctypes binding of the system `libhackrf`; RX buffers are handed to the event loop thread-safely
