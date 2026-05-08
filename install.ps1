# AetherLink SDR MCP - Windows Installer
# Run: powershell -ExecutionPolicy Bypass -File install.ps1

$ErrorActionPreference = "Stop"
$PythonMin = "3.10"

# ─── Helpers ──────────────────────────────────────────────────────────────────

function Write-Info  { Write-Host "[INFO] " -ForegroundColor Blue -NoNewline; Write-Host $args }
function Write-Ok    { Write-Host "[OK] " -ForegroundColor Green -NoNewline; Write-Host $args }
function Write-Warn  { Write-Host "[WARN] " -ForegroundColor Yellow -NoNewline; Write-Host $args }
function Write-Fail  { Write-Host "[ERROR] " -ForegroundColor Red -NoNewline; Write-Host $args; exit 1 }

function Prompt-YesNo {
    param([string]$Message)
    $answer = Read-Host "  $Message [Y/n]"
    if ([string]::IsNullOrEmpty($answer)) { $answer = "Y" }
    return $answer -match "^[Yy]"
}

# ─── Check prerequisites ─────────────────────────────────────────────────────

function Check-Git {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Write-Fail "Git not found. Download from https://git-scm.com/download/win"
    }
    Write-Ok "Git found"
}

function Check-Python {
    $script:Python = $null
    foreach ($candidate in @("python", "python3", "py")) {
        try {
            $ver = & $candidate -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
            if ($ver) {
                $parts = $ver.Split(".")
                if ([int]$parts[0] -ge 3 -and [int]$parts[1] -ge 10) {
                    $script:Python = $candidate
                    break
                }
            }
        } catch { }
    }

    if (-not $script:Python) {
        Write-Fail "Python >= $PythonMin not found. Download from https://www.python.org/downloads/"
    }

    $version = & $script:Python --version 2>&1
    Write-Ok "Python: $version"
}

# ─── System dependencies ─────────────────────────────────────────────────────

function Install-SystemDeps {
    Write-Info "Checking system dependencies..."
    Write-Host ""

    # RTL-SDR drivers
    if (Get-Command rtl_test -ErrorAction SilentlyContinue) {
        Write-Ok "RTL-SDR drivers found"
    } else {
        Write-Warn "RTL-SDR drivers not found"
        Write-Host ""
        Write-Host "  RTL-SDR requires two things on Windows:" -ForegroundColor Cyan
        Write-Host ""
        Write-Host "  1. RTL-SDR binaries:"
        Write-Host "     Download from: https://ftp.osmocom.org/binaries/windows/rtl-sdr/"
        Write-Host "     Extract and add the folder to your system PATH"
        Write-Host ""
        Write-Host "  2. Zadig USB driver (REQUIRED):"
        Write-Host "     Download from: https://zadig.akeo.ie/"
        Write-Host "     - Run as Administrator"
        Write-Host "     - Options -> List All Devices"
        Write-Host "     - Select 'Bulk-In, Interface (Interface 0)'"
        Write-Host "     - Set driver to 'WinUSB'"
        Write-Host "     - Click 'Replace Driver'"
        Write-Host ""
        Write-Host "  If driver install fails, disable Memory Integrity:" -ForegroundColor Yellow
        Write-Host "     Settings -> Privacy & security -> Windows Security ->"
        Write-Host "     Device security -> Core isolation -> Memory integrity OFF"
        Write-Host ""

        if (Prompt-YesNo "Open RTL-SDR download page in browser?") {
            Start-Process "https://ftp.osmocom.org/binaries/windows/rtl-sdr/"
        }
        if (Prompt-YesNo "Open Zadig download page in browser?") {
            Start-Process "https://zadig.akeo.ie/"
        }
    }

    # rtl_433
    if (Get-Command rtl_433 -ErrorAction SilentlyContinue) {
        Write-Ok "rtl_433 found"
    } else {
        if (Prompt-YesNo "Open rtl_433 download page? (ISM band device decoding)") {
            Start-Process "https://github.com/merbanan/rtl_433/releases"
        } else {
            Write-Warn "Skipped rtl_433 (optional)"
        }
    }

    # SatDump
    if (Get-Command satdump -ErrorAction SilentlyContinue) {
        Write-Ok "SatDump found"
    } else {
        if (Prompt-YesNo "Open SatDump download page? (satellite image decoding)") {
            Start-Process "https://github.com/SatDump/SatDump/releases"
        } else {
            Write-Warn "Skipped SatDump (optional)"
        }
    }

    # dump1090
    if (Get-Command dump1090 -ErrorAction SilentlyContinue) {
        Write-Ok "dump1090 found"
    } else {
        if (Prompt-YesNo "Open dump1090 download page? (ADS-B aircraft tracking)") {
            Start-Process "https://github.com/flightaware/dump1090"
        } else {
            Write-Warn "Skipped dump1090 (optional)"
        }
    }

    Write-Host ""
}

# ─── Install AetherLink ──────────────────────────────────────────────────────

function Install-AetherLink {
    Write-Info "Installing AetherLink..."

    $script:AetherlinkCmd = ""

    # Check for uv
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        & uv tool install aetherlink 2>&1 | Select-Object -Last 3
        $script:AetherlinkCmd = "uvx aetherlink"
        Write-Ok "AetherLink installed via uv"
    }
    # Check for pipx
    elseif (Get-Command pipx -ErrorAction SilentlyContinue) {
        try {
            & pipx install aetherlink 2>&1 | Select-Object -Last 3
        } catch {
            & pipx upgrade aetherlink 2>&1 | Select-Object -Last 3
        }
        $aetherlinkPath = (Get-Command aetherlink -ErrorAction SilentlyContinue).Source
        $script:AetherlinkCmd = if ($aetherlinkPath) { $aetherlinkPath } else { "pipx run aetherlink" }
        Write-Ok "AetherLink installed via pipx"
    }
    # Fallback: create venv
    else {
        $venvDir = "$env:USERPROFILE\.aetherlink"

        if (Test-Path "$venvDir\Scripts\python.exe") {
            Write-Info "Upgrading existing install at $venvDir..."
            & "$venvDir\Scripts\pip" install --upgrade aetherlink -q 2>&1 | Select-Object -Last 3
        } else {
            Write-Info "Creating isolated environment at $venvDir..."
            & $script:Python -m venv $venvDir
            & "$venvDir\Scripts\pip" install --upgrade pip -q 2>&1 | Select-Object -Last 1
            & "$venvDir\Scripts\pip" install aetherlink -q 2>&1 | Select-Object -Last 3
        }
        $script:AetherlinkCmd = "$venvDir\Scripts\aetherlink.exe"
        Write-Ok "AetherLink installed in $venvDir"
    }

    # Verify
    try {
        $ver = & $script:AetherlinkCmd --version 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Ok $ver
        } else {
            throw "non-zero exit"
        }
    } catch {
        # Fallback verify via Python import
        $testPython = "$env:USERPROFILE\.aetherlink\Scripts\python.exe"
        if (Test-Path $testPython) {
            try {
                $ver = & $testPython -c "from sdr_mcp import __version__; print(f'aetherlink {__version__}')" 2>&1
                Write-Ok $ver
            } catch {
                Write-Warn "Package installed but verification failed -- try running 'aetherlink --version' manually"
            }
        } else {
            Write-Warn "Package installed but verification failed"
        }
    }
}

# ─── Configure Claude Desktop ────────────────────────────────────────────────

function Setup-ClaudeDesktop {
    Write-Host ""
    Write-Info "Configuring Claude Desktop..."

    $configDir = "$env:APPDATA\Claude"
    $configFile = "$configDir\claude_desktop_config.json"

    # Try --setup first
    try {
        & $script:AetherlinkCmd --setup 2>&1
        if ($LASTEXITCODE -eq 0) { return }
    } catch { }

    # Manual fallback
    $cmdJson = $script:AetherlinkCmd.Replace("\", "\\")

    # Load or create config
    $config = @{ mcpServers = @{} }
    if (Test-Path $configFile) {
        try {
            $existing = Get-Content $configFile -Raw | ConvertFrom-Json
            # Preserve existing config
            $config = @{}
            $existing.PSObject.Properties | ForEach-Object { $config[$_.Name] = $_.Value }
            if (-not $config.ContainsKey("mcpServers")) {
                $config["mcpServers"] = @{}
            }
        } catch {
            Write-Warn "Could not parse existing config, creating backup..."
            Copy-Item $configFile "$configFile.bak" -Force
        }
    }

    # Check existing
    $mcpServers = $config["mcpServers"]
    if ($mcpServers -is [PSCustomObject]) {
        $hash = @{}
        $mcpServers.PSObject.Properties | ForEach-Object { $hash[$_.Name] = $_.Value }
        $mcpServers = $hash
        $config["mcpServers"] = $hash
    }

    if ($mcpServers.ContainsKey("aetherlink")) {
        Write-Host "  AetherLink already configured"
        if (-not (Prompt-YesNo "Update AetherLink entry? (other servers untouched)")) {
            Write-Host "  No changes made."
            return
        }
    }

    $otherServers = $mcpServers.Keys | Where-Object { $_ -ne "aetherlink" }
    if ($otherServers) {
        Write-Host "  Existing MCP servers (untouched): $($otherServers -join ', ')"
    }

    $mcpServers["aetherlink"] = @{
        command = $script:AetherlinkCmd
        args = @()
    }

    # Write config
    New-Item -ItemType Directory -Force -Path $configDir | Out-Null
    $config | ConvertTo-Json -Depth 10 | Out-File -Encoding utf8 $configFile
    Write-Ok "Added AetherLink to $configFile"
    Write-Host "  Restart Claude Desktop to activate."
}

# ─── Summary ──────────────────────────────────────────────────────────────────

function Print-Summary {
    Write-Host ""
    Write-Host "========================================================" -ForegroundColor Green
    Write-Host "  AetherLink SDR MCP - Installation Complete" -ForegroundColor Green
    Write-Host "========================================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "  System tools:"

    $tools = @(
        @("rtl_test",    "RTL-SDR drivers"),
        @("dump1090",    "ADS-B decoder"),
        @("rtl_433",     "ISM band decoder"),
        @("satdump",     "Satellite decoder"),
        @("multimon-ng", "POCSAG decoder")
    )

    foreach ($tool in $tools) {
        if (Get-Command $tool[0] -ErrorAction SilentlyContinue) {
            Write-Host "    " -NoNewline; Write-Host "+" -ForegroundColor Green -NoNewline; Write-Host " $($tool[1]) ($($tool[0]))"
        } else {
            Write-Host "    " -NoNewline; Write-Host "-" -ForegroundColor Yellow -NoNewline; Write-Host " $($tool[1]) ($($tool[0])) - not installed"
        }
    }

    Write-Host ""
    Write-Host "  Next steps:"
    Write-Host "    1. Install RTL-SDR USB driver via Zadig (if not done)"
    Write-Host "    2. Plug in your RTL-SDR or HackRF"
    Write-Host "    3. Restart Claude Desktop"
    Write-Host '    4. Ask Claude: "Connect to my RTL-SDR"'
    Write-Host ""
}

# ─── Main ─────────────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "  AetherLink SDR MCP - Windows Installer"
Write-Host "  ======================================="
Write-Host ""

Check-Git
Check-Python
Install-SystemDeps
Install-AetherLink
Setup-ClaudeDesktop
Print-Summary
