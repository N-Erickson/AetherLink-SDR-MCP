#!/usr/bin/env bash
set -euo pipefail

# AetherLink SDR MCP - Installer
# Supports macOS (Homebrew) and Linux (apt)

REPO_URL="https://github.com/N-Erickson/AetherLink-SDR-MCP"
INSTALL_DIR="${AETHERLINK_DIR:-$HOME/AetherLink-SDR-MCP}"
PYTHON_MIN="3.10"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

info()  { echo -e "${BLUE}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
fail()  { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ─── Detect OS ────────────────────────────────────────────────────────────────

detect_os() {
    case "$(uname -s)" in
        Darwin*) OS="macos" ;;
        Linux*)  OS="linux" ;;
        *)       fail "Unsupported OS: $(uname -s). This installer supports macOS and Linux." ;;
    esac
    info "Detected OS: $OS"
}

# ─── Check Python ─────────────────────────────────────────────────────────────

check_python() {
    local py=""
    for candidate in python3 python; do
        if command -v "$candidate" &>/dev/null; then
            local ver
            ver=$("$candidate" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || true)
            if [ -n "$ver" ]; then
                local major minor
                major=$(echo "$ver" | cut -d. -f1)
                minor=$(echo "$ver" | cut -d. -f2)
                if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
                    py="$candidate"
                    break
                fi
            fi
        fi
    done

    if [ -z "$py" ]; then
        fail "Python >= $PYTHON_MIN not found. Install Python 3.10+ first."
    fi

    PYTHON="$py"
    ok "Python: $($PYTHON --version)"
}

# ─── Install system dependencies ──────────────────────────────────────────────

install_system_deps() {
    info "Installing system dependencies..."

    if [ "$OS" = "macos" ]; then
        if ! command -v brew &>/dev/null; then
            fail "Homebrew not found. Install from https://brew.sh"
        fi

        # Core (required)
        brew_install librtlsdr
        brew_install rtl-sdr

        # Optional decoders
        echo ""
        info "Optional dependencies (recommended):"
        prompt_install_brew rtl_433 "ISM band device decoding (weather stations, sensors, etc.)"
        prompt_install_brew satdump "Meteor-M weather satellite image decoding"

    elif [ "$OS" = "linux" ]; then
        if ! command -v apt-get &>/dev/null; then
            warn "apt-get not found. You may need to install dependencies manually."
            warn "Required: rtl-sdr librtlsdr-dev"
            return
        fi

        info "Installing via apt (may require sudo)..."
        sudo apt-get update -qq
        sudo apt-get install -y -qq rtl-sdr librtlsdr-dev

        # Optional
        echo ""
        info "Optional dependencies (recommended):"
        prompt_install_apt rtl-433 "ISM band device decoding (weather stations, sensors, etc.)"
        prompt_install_apt satdump "Meteor-M weather satellite image decoding" "ppa:satdump/satdump"
    fi
}

brew_install() {
    if brew list "$1" &>/dev/null; then
        ok "$1 already installed"
    else
        info "Installing $1..."
        brew install "$1"
        ok "$1 installed"
    fi
}

prompt_install_brew() {
    local pkg="$1" desc="$2"
    if brew list "$pkg" &>/dev/null; then
        ok "$pkg already installed"
        return
    fi
    read -rp "  Install $pkg ($desc)? [Y/n] " answer
    answer="${answer:-Y}"
    if [[ "$answer" =~ ^[Yy] ]]; then
        brew install "$pkg"
        ok "$pkg installed"

        # SatDump macOS fixup
        if [ "$pkg" = "satdump" ] && [ "$OS" = "macos" ]; then
            fix_satdump_macos
        fi
    else
        warn "Skipped $pkg"
    fi
}

prompt_install_apt() {
    local pkg="$1" desc="$2" ppa="${3:-}"
    if dpkg -l "$pkg" 2>/dev/null | grep -q "^ii"; then
        ok "$pkg already installed"
        return
    fi
    read -rp "  Install $pkg ($desc)? [Y/n] " answer
    answer="${answer:-Y}"
    if [[ "$answer" =~ ^[Yy] ]]; then
        if [ -n "$ppa" ]; then
            sudo add-apt-repository -y "$ppa"
            sudo apt-get update -qq
        fi
        sudo apt-get install -y -qq "$pkg"
        ok "$pkg installed"
    else
        warn "Skipped $pkg"
    fi
}

fix_satdump_macos() {
    if [ -d "/Applications/SatDump.app/Contents/Resources" ]; then
        info "Fixing SatDump resource paths for macOS..."
        sudo mkdir -p /usr/local/share/satdump
        sudo cp -R /Applications/SatDump.app/Contents/Resources/* /usr/local/share/satdump/ 2>/dev/null || true
        sudo mkdir -p /usr/local/lib/satdump
        sudo ln -sf /Applications/SatDump.app/Contents/Resources/plugins /usr/local/lib/satdump/plugins 2>/dev/null || true
        ok "SatDump paths configured"
    fi
}

# ─── Clone or update repo ────────────────────────────────────────────────────

setup_repo() {
    if [ -d "$INSTALL_DIR/.git" ]; then
        info "Repository exists at $INSTALL_DIR, pulling latest..."
        git -C "$INSTALL_DIR" pull --ff-only || warn "Could not pull latest (you may have local changes)"
    else
        info "Cloning repository to $INSTALL_DIR..."
        git clone "$REPO_URL" "$INSTALL_DIR"
    fi
    ok "Repository ready at $INSTALL_DIR"
}

# ─── Set up Python environment ────────────────────────────────────────────────

setup_python_env() {
    cd "$INSTALL_DIR"

    if [ ! -d "venv" ]; then
        info "Creating virtual environment..."
        $PYTHON -m venv venv
    fi

    info "Installing Python dependencies..."
    venv/bin/pip install --upgrade pip -q
    venv/bin/pip install -e . -q
    ok "Python environment ready"

    # Verify
    venv/bin/python -c "from sdr_mcp.server import SDRMCPServer; print('AetherLink imports OK')" 2>/dev/null
    ok "AetherLink verified"
}

# ─── Generate Claude Desktop config ──────────────────────────────────────────

setup_claude_desktop() {
    local config_dir config_file python_path

    if [ "$OS" = "macos" ]; then
        config_dir="$HOME/Library/Application Support/Claude"
    else
        config_dir="$HOME/.config/Claude"
    fi
    config_file="$config_dir/claude_desktop_config.json"
    python_path="$INSTALL_DIR/venv/bin/python"

    echo ""
    info "Claude Desktop MCP configuration:"
    echo ""

    local snippet
    snippet=$(cat <<EOF
{
  "mcpServers": {
    "aetherlink": {
      "command": "$python_path",
      "args": ["-m", "sdr_mcp.server"],
      "cwd": "$INSTALL_DIR"
    }
  }
}
EOF
)

    if [ -f "$config_file" ]; then
        # Config exists - check if aetherlink already configured
        if grep -q "aetherlink" "$config_file" 2>/dev/null; then
            ok "Claude Desktop already configured for AetherLink"
            return
        fi

        warn "Existing config found at $config_file"
        echo "  Add this to your mcpServers section:"
        echo ""
        echo -e "${YELLOW}    \"aetherlink\": {"
        echo "      \"command\": \"$python_path\","
        echo "      \"args\": [\"-m\", \"sdr_mcp.server\"],"
        echo -e "      \"cwd\": \"$INSTALL_DIR\"\n    }${NC}"
    else
        read -rp "  Create Claude Desktop config automatically? [Y/n] " answer
        answer="${answer:-Y}"
        if [[ "$answer" =~ ^[Yy] ]]; then
            mkdir -p "$config_dir"
            echo "$snippet" > "$config_file"
            ok "Config written to $config_file"
        else
            echo ""
            echo "  Add this to $config_file:"
            echo ""
            echo "$snippet"
        fi
    fi
}

# ─── Summary ──────────────────────────────────────────────────────────────────

print_summary() {
    echo ""
    echo -e "${GREEN}════════════════════════════════════════════════════════${NC}"
    echo -e "${GREEN}  AetherLink SDR MCP - Installation Complete${NC}"
    echo -e "${GREEN}════════════════════════════════════════════════════════${NC}"
    echo ""
    echo "  Install location: $INSTALL_DIR"
    echo "  Python:           $INSTALL_DIR/venv/bin/python"
    echo ""
    echo "  System tools:"
    check_tool rtl_test    "RTL-SDR drivers"
    check_tool rtl_adsb    "ADS-B decoder"
    check_tool rtl_433     "ISM band decoder"
    check_tool satdump     "Satellite decoder"
    check_tool multimon-ng "POCSAG decoder"
    echo ""
    echo "  Next steps:"
    echo "    1. Plug in your RTL-SDR or HackRF"
    echo "    2. Restart Claude Desktop"
    echo "    3. Ask Claude: \"Connect to my RTL-SDR\""
    echo ""
}

check_tool() {
    if command -v "$1" &>/dev/null; then
        echo -e "    ${GREEN}✓${NC} $2 ($1)"
    else
        echo -e "    ${YELLOW}✗${NC} $2 ($1) - not installed"
    fi
}

# ─── Main ─────────────────────────────────────────────────────────────────────

main() {
    echo ""
    echo "  AetherLink SDR MCP - Installer"
    echo "  ==============================="
    echo ""

    detect_os
    check_python
    install_system_deps
    setup_repo
    setup_python_env
    setup_claude_desktop
    print_summary
}

main "$@"
