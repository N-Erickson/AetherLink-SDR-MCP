# AetherLink: Software Defined Radio Model Context Protocol Server

Control Software Defined Radios and decode radio protocols through an AI-friendly Model Context Protocol interface.

## 🚀 Features

- **Direct Hardware Control**: RTL-SDR and HackRF support without GNU Radio
- **Protocol Decoders**: ADS-B, AIS, NOAA weather satellites, amateur radio, and more
- **Advanced Analysis**: Real-time spectrum analysis, waterfall displays, signal identification
- **MCP Integration**: Seamless integration with Claude and other MCP clients

## 📦 Installation

### Prerequisites

1. **RTL-SDR Drivers**:
   ```bash
   # Ubuntu/Debian
   sudo apt-get install rtl-sdr librtlsdr-dev
   
   # macOS
   brew install librtlsdr
   
   # Windows
   # Download and install from https://osmocom.org/projects/rtl-sdr/wiki
   ```

2. **Python 3.10+**

### Install from Source

```bash
# Clone the repository
git clone https://github.com/yourusername/aetherlink
cd aetherlink

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install with basic dependencies
pip install -e .

# Install with all protocol decoders
pip install -e ".[decoders]"

# Install with HackRF support
pip install -e ".[hackrf]"
```

## 🎯 Quick Start

### Configure MCP Client

Add to your Claude Desktop configuration (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "aetherlink": {
      "command": "python",
      "args": ["-m", "sdr_mcp.server"],
      "env": {}
    }
  }
}
```

### Basic Usage

```
# Connect to RTL-SDR
"Connect to my RTL-SDR"

# Track aircraft
"Track aircraft in my area"

# Analyze spectrum
"Show me what's on the FM broadcast band"

# Scan for signals
"Scan 430-440 MHz for amateur radio activity"
```

## 📡 Supported Hardware

| Device    | RX Frequency      | TX Support | Status      |
|-----------|-------------------|------------|-------------|
| RTL-SDR   | 24 MHz - 1.7 GHz  | ❌         | ✅ Stable   |
| HackRF    | 1 MHz - 6 GHz     | ✅         | 🚧 Beta     |
| PlutoSDR  | 70 MHz - 6 GHz    | ✅         | 📋 Planned  |
| USRP      | Varies            | ✅         | 📋 Planned  |

## 📊 Protocol Support

| Protocol  | Description          | Status      |
|-----------|---------------------|-------------|
| ADS-B     | Aircraft tracking   | ✅ Stable   |
| AIS       | Ship tracking       | 🚧 Beta     |
| NOAA APT  | Weather satellites  | 🚧 Beta     |
| FT8/WSPR  | Amateur radio       | 📋 Planned  |
| LoRa      | IoT devices         | 📋 Planned  |

## 🛠️ Available Tools

| Tool                    | Description                  |
|------------------------|------------------------------|
| `sdr_connect`          | Connect to SDR hardware      |
| `sdr_disconnect`       | Disconnect from SDR          |
| `sdr_set_frequency`    | Set center frequency         |
| `sdr_set_gain`         | Set gain                     |
| `sdr_get_status`       | Get hardware status          |
| `aviation_track_aircraft` | Start ADS-B tracking      |
| `spectrum_analyze`     | Analyze RF spectrum          |
| `spectrum_scan`        | Scan frequency range         |
| `recording_start/stop` | Record IQ samples            |

## 🔧 Development

### Running Tests

```bash
pytest tests/
```

### Docker Support

```bash
docker-compose up
```

