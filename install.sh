#!/usr/bin/env bash
set -euo pipefail

# AetherLink SDR MCP - Installer
# Supports macOS (Homebrew), Debian/Ubuntu (apt), Fedora/RHEL (dnf), Arch (pacman)

PYTHON_MIN="3.10"

# Colors (disabled if not a terminal)
if [ -t 1 ]; then
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
else
    RED=''; GREEN=''; YELLOW=''; BLUE=''; NC=''
fi

info()  { echo -e "${BLUE}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
fail()  { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

prompt_yn() {
    local msg="$1"
    if [ "${NONINTERACTIVE:-}" = "1" ]; then
        return 0  # Default yes in non-interactive mode
    fi
    read -rp "  $msg [Y/n] " answer
    answer="${answer:-Y}"
    [[ "$answer" =~ ^[Yy] ]]
}

# ─── Detect OS and package manager ───────────────────────────────────────────

detect_os() {
    case "$(uname -s)" in
        Darwin*) OS="macos"; PKG_MGR="brew" ;;
        Linux*)
            OS="linux"
            if command -v apt-get &>/dev/null; then
                PKG_MGR="apt"
            elif command -v dnf &>/dev/null; then
                PKG_MGR="dnf"
            elif command -v pacman &>/dev/null; then
                PKG_MGR="pacman"
            else
                PKG_MGR="unknown"
            fi
            ;;
        *)  fail "Unsupported OS: $(uname -s). This installer supports macOS and Linux." ;;
    esac
    info "Detected: $OS ($PKG_MGR)"
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

# ─── Package installation helpers ─────────────────────────────────────────────

pkg_install() {
    local pkg="$1"

    # Check if already installed
    if command -v "$pkg" &>/dev/null; then
        ok "$pkg already installed"
        return 0
    fi

    case "$PKG_MGR" in
        brew)
            if brew list "$pkg" &>/dev/null; then ok "$pkg already installed"; return 0; fi
            info "Installing $pkg..."
            brew install "$pkg"
            ;;
        apt)
            if dpkg -l "$pkg" 2>/dev/null | grep -q "^ii"; then ok "$pkg already installed"; return 0; fi
            info "Installing $pkg..."
            sudo apt-get install -y -qq "$pkg"
            ;;
        dnf)
            if rpm -q "$pkg" &>/dev/null; then ok "$pkg already installed"; return 0; fi
            info "Installing $pkg..."
            sudo dnf install -y -q "$pkg"
            ;;
        pacman)
            if pacman -Qi "$pkg" &>/dev/null; then ok "$pkg already installed"; return 0; fi
            info "Installing $pkg..."
            sudo pacman -S --noconfirm "$pkg"
            ;;
        *)
            warn "Cannot auto-install $pkg (unknown package manager)"
            return 1
            ;;
    esac
    ok "$pkg installed"
}

pkg_install_optional() {
    local pkg="$1" desc="$2"
    if command -v "$pkg" &>/dev/null; then
        ok "$pkg already installed"
        return 0
    fi

    if prompt_yn "Install $pkg ($desc)?"; then
        pkg_install "$pkg" || warn "Failed to install $pkg -- install manually"
    else
        warn "Skipped $pkg"
    fi
}

# ─── Install system dependencies ──────────────────────────────────────────────

install_system_deps() {
    info "Installing system dependencies..."

    case "$PKG_MGR" in
        brew)
            pkg_install librtlsdr
            pkg_install rtl-sdr
            ;;
        apt)
            sudo apt-get update -qq
            pkg_install rtl-sdr
            # librtlsdr-dev maps to the same binary package check
            sudo apt-get install -y -qq librtlsdr-dev 2>/dev/null || true
            ;;
        dnf)
            pkg_install rtl-sdr
            sudo dnf install -y -q rtl-sdr-devel 2>/dev/null || true
            ;;
        pacman)
            pkg_install rtl-sdr
            ;;
        *)
            warn "Cannot auto-install RTL-SDR drivers. Install manually:"
            warn "  https://osmocom.org/projects/rtl-sdr/wiki"
            ;;
    esac

    # Linux-specific: blacklist conflicting kernel module
    if [ "$OS" = "linux" ]; then
        setup_linux_udev
    fi

    # Optional decoders
    echo ""
    info "Optional dependencies (recommended):"

    # rtl_433
    pkg_install_optional rtl_433 "ISM band device decoding (weather stations, sensors)"

    # SatDump
    install_satdump

    # multimon-ng
    install_multimon_ng
}

setup_linux_udev() {
    local blacklist_file="/etc/modprobe.d/blacklist-rtlsdr.conf"
    if [ -f "$blacklist_file" ]; then
        ok "RTL-SDR kernel module blacklist already configured"
        return
    fi

    if lsmod 2>/dev/null | grep -q dvb_usb_rtl28xxu; then
        warn "DVB kernel driver is loaded and will conflict with RTL-SDR"
    fi

    if prompt_yn "Blacklist conflicting DVB kernel module? (required for RTL-SDR)"; then
        sudo tee "$blacklist_file" > /dev/null <<EOF
# Blacklist DVB drivers that conflict with RTL-SDR
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
blacklist dvb_usb_v2
EOF
        sudo udevadm control --reload-rules 2>/dev/null || true
        sudo udevadm trigger 2>/dev/null || true
        ok "Kernel module blacklisted (reboot may be required)"
    fi
}

install_satdump() {
    if command -v satdump &>/dev/null; then
        ok "satdump already installed"
        return
    fi

    if ! prompt_yn "Install satdump (Meteor-M weather satellite decoding)?"; then
        warn "Skipped satdump"
        return
    fi

    case "$PKG_MGR" in
        brew)
            # Try cask first (GUI + CLI), fall back to formula
            if brew install --cask satdump 2>/dev/null; then
                ok "satdump installed (cask)"
                # Fix resource paths for cask install
                if [ -d "/Applications/SatDump.app/Contents/Resources" ]; then
                    info "Configuring SatDump resource paths..."
                    sudo mkdir -p /usr/local/share/satdump
                    sudo cp -R /Applications/SatDump.app/Contents/Resources/* /usr/local/share/satdump/ 2>/dev/null || true
                    sudo mkdir -p /usr/local/lib/satdump
                    sudo ln -sf /Applications/SatDump.app/Contents/Resources/plugins /usr/local/lib/satdump/plugins 2>/dev/null || true
                fi
            elif brew install satdump 2>/dev/null; then
                ok "satdump installed (formula)"
            else
                warn "Could not install satdump. Download from: https://github.com/SatDump/SatDump/releases"
            fi
            ;;
        apt)
            # Try PPA
            if sudo add-apt-repository -y ppa:satdump/satdump 2>/dev/null; then
                sudo apt-get update -qq
                sudo apt-get install -y -qq satdump && ok "satdump installed" || warn "satdump install failed"
            else
                warn "SatDump PPA not available. Download from: https://github.com/SatDump/SatDump/releases"
            fi
            ;;
        dnf|pacman)
            warn "SatDump not in standard repos. Download from: https://github.com/SatDump/SatDump/releases"
            ;;
        *)
            warn "Install SatDump manually: https://github.com/SatDump/SatDump/releases"
            ;;
    esac
}

install_multimon_ng() {
    if command -v multimon-ng &>/dev/null; then
        ok "multimon-ng already installed"
        return
    fi

    if ! prompt_yn "Install multimon-ng (POCSAG pager decoding)?"; then
        warn "Skipped multimon-ng"
        return
    fi

    # Try package manager first (works on apt, dnf, pacman -- not brew)
    case "$PKG_MGR" in
        apt|dnf|pacman)
            if pkg_install multimon-ng 2>/dev/null; then
                return
            fi
            info "Package not available, building from source..."
            ;;
        brew)
            info "Not in Homebrew, building from source..."
            ;;
    esac

    # Build from source
    info "Building multimon-ng from source..."

    # Ensure build tools
    case "$PKG_MGR" in
        brew)   command -v cmake &>/dev/null || brew install cmake ;;
        apt)    sudo apt-get install -y -qq build-essential cmake libpulse-dev 2>/dev/null || true ;;
        dnf)    sudo dnf install -y -q gcc-c++ cmake pulseaudio-libs-devel 2>/dev/null || true ;;
        pacman) sudo pacman -S --noconfirm --needed cmake base-devel libpulse 2>/dev/null || true ;;
    esac

    local build_dir
    build_dir=$(mktemp -d)
    if git clone --depth 1 https://github.com/EliasOenal/multimon-ng.git "$build_dir/multimon-ng" 2>/dev/null; then
        cd "$build_dir/multimon-ng"
        mkdir -p build && cd build
        if cmake .. 2>&1 | tail -2 && make -j"$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 2)" 2>&1 | tail -2; then
            if [ -f multimon-ng ]; then
                sudo cp multimon-ng /usr/local/bin/
                ok "multimon-ng installed to /usr/local/bin/"
            else
                warn "multimon-ng binary not found after build"
            fi
        else
            warn "multimon-ng build failed"
        fi
        cd /
    else
        warn "Failed to clone multimon-ng repository"
    fi
    rm -rf "$build_dir"
}

# ─── Install AetherLink Python package ────────────────────────────────────────

install_aetherlink() {
    info "Installing AetherLink..."

    AETHERLINK_CMD=""

    # Prefer uv > pipx > venv (in order of best UX)
    if command -v uv &>/dev/null; then
        uv tool install aetherlink 2>&1 | tail -3 || true
        AETHERLINK_CMD="$(command -v aetherlink 2>/dev/null || echo "uvx aetherlink")"
        ok "AetherLink installed via uv"

    elif command -v pipx &>/dev/null; then
        pipx install aetherlink 2>&1 | tail -3 || pipx upgrade aetherlink 2>&1 | tail -3 || true
        AETHERLINK_CMD="$(command -v aetherlink 2>/dev/null || echo "pipx run aetherlink")"
        ok "AetherLink installed via pipx"

    else
        # Create a dedicated venv -- works everywhere
        local venv_dir="$HOME/.aetherlink"
        if [ -d "$venv_dir" ]; then
            info "Upgrading existing install at $venv_dir..."
            "$venv_dir/bin/pip" install --upgrade aetherlink -q 2>&1 | tail -3
        else
            info "Creating isolated environment at $venv_dir..."
            $PYTHON -m venv "$venv_dir"
            "$venv_dir/bin/pip" install --upgrade pip -q 2>&1 | tail -1
            "$venv_dir/bin/pip" install aetherlink -q 2>&1 | tail -3
        fi
        AETHERLINK_CMD="$venv_dir/bin/aetherlink"
        ok "AetherLink installed in $venv_dir"
    fi

    # Verify
    if $AETHERLINK_CMD --version 2>/dev/null; then
        ok "Verified"
    else
        # Fallback: check via Python import
        local test_python="${venv_dir:-$HOME/.aetherlink}/bin/python"
        if [ -f "$test_python" ] && $test_python -c "from sdr_mcp import __version__; print(f'aetherlink {__version__}')" 2>/dev/null; then
            ok "Verified (via Python import)"
        else
            warn "Package installed but verification failed -- this may be OK, try running 'aetherlink --version' manually"
        fi
    fi
}

# ─── Configure Claude Desktop ────────────────────────────────────────────────

setup_claude_desktop() {
    echo ""
    info "Configuring Claude Desktop..."

    # Try the built-in --setup command
    if $AETHERLINK_CMD --setup 2>/dev/null; then
        return
    fi

    # Fallback: manual instructions
    warn "Auto-configuration not available. Configure manually:"
    echo ""
    echo "  Add this to your Claude Desktop config file:"
    echo ""
    echo "    \"aetherlink\": {"
    echo "      \"command\": \"$AETHERLINK_CMD\","
    echo "      \"args\": []"
    echo "    }"
    echo ""

    local config_path
    if [ "$OS" = "macos" ]; then
        config_path="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
    else
        config_path="$HOME/.config/Claude/claude_desktop_config.json"
    fi
    echo "  Config file: $config_path"
}

# ─── Summary ──────────────────────────────────────────────────────────────────

print_summary() {
    echo ""
    echo -e "${GREEN}════════════════════════════════════════════════════════${NC}"
    echo -e "${GREEN}  AetherLink SDR MCP - Installation Complete${NC}"
    echo -e "${GREEN}════════════════════════════════════════════════════════${NC}"
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
    if [ "$OS" = "linux" ]; then
        echo "    2. You may need to reboot if kernel module was blacklisted"
        echo "    3. Restart Claude Desktop"
        echo "    4. Ask Claude: \"Connect to my RTL-SDR\""
    else
        echo "    2. Restart Claude Desktop"
        echo "    3. Ask Claude: \"Connect to my RTL-SDR\""
    fi
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
    install_aetherlink
    setup_claude_desktop
    print_summary
}

main "$@"
