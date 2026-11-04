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
    if (Get-Command git -ErrorAction SilentlyContinue) {
        Write-Ok "Git found"
    } else {
        Write-Warn "Git not found (only needed for installing from source)"
    }
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

    # multimon-ng (POCSAG)
    if (Get-Command multimon-ng -ErrorAction SilentlyContinue) {
        Write-Ok "multimon-ng found"
    } else {
        if (Prompt-YesNo "Open multimon-ng page? (POCSAG pager decoding, needs rtl_fm from the RTL-SDR binaries)") {
            Start-Process "https://github.com/EliasOenal/multimon-ng"
        } else {
            Write-Warn "Skipped multimon-ng (optional)"
        }
    }

    # AIS-catcher (AIS)
    if ((Get-Command AIS-catcher -ErrorAction SilentlyContinue) -or (Get-Command rtl_ais -ErrorAction SilentlyContinue)) {
        Write-Ok "AIS receiver found"
    } else {
        if (Prompt-YesNo "Open AIS-catcher download page? (AIS ship tracking)") {
            Start-Process "https://github.com/jvde-github/AIS-catcher/releases"
        } else {
            Write-Warn "Skipped AIS-catcher (optional)"
        }
    }

    Write-Host ""
}

# ─── Install AetherLink ──────────────────────────────────────────────────────

function Resolve-Aetherlink {
    # The installed console script, wherever the chosen installer put it
    $candidates = @()
    $found = Get-Command aetherlink -ErrorAction SilentlyContinue
    if ($found) { $candidates += $found.Source }
    $candidates += "$env:USERPROFILE\.local\bin\aetherlink.exe"
    $candidates += "$env:LOCALAPPDATA\uv\tools\aetherlink\Scripts\aetherlink.exe"
    $candidates += "$env:USERPROFILE\.local\pipx\venvs\aetherlink\Scripts\aetherlink.exe"
    $candidates += "$env:USERPROFILE\.aetherlink\Scripts\aetherlink.exe"
    foreach ($c in $candidates) {
        if ($c -and (Test-Path $c)) { return $c }
    }
    return $null
}

function Install-AetherLink {
    Write-Info "Installing AetherLink..."

    $script:AetherlinkCmd = $null
    $script:AetherlinkArgs = @()

    if (Get-Command uv -ErrorAction SilentlyContinue) {
        & uv tool install --force aetherlink 2>&1 | Select-Object -Last 3
        Write-Ok "AetherLink installed via uv"
    }
    elseif (Get-Command pipx -ErrorAction SilentlyContinue) {
        & pipx install --force aetherlink 2>&1 | Select-Object -Last 3
        Write-Ok "AetherLink installed via pipx"
    }
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
        Write-Ok "AetherLink installed in $venvDir"
    }

    $exe = Resolve-Aetherlink
    if ($exe) {
        $script:AetherlinkCmd = $exe
    } elseif (Get-Command uvx -ErrorAction SilentlyContinue) {
        $script:AetherlinkCmd = (Get-Command uvx).Source
        $script:AetherlinkArgs = @("aetherlink")
    } else {
        Write-Fail "AetherLink was installed but its executable could not be found. Open a new terminal and run: aetherlink --setup"
    }

    # Verify
    try {
        $ver = & $script:AetherlinkCmd @($script:AetherlinkArgs + "--version") 2>&1
        if ($LASTEXITCODE -eq 0) { Write-Ok "$ver" } else { throw "non-zero exit" }
    } catch {
        Write-Warn "Package installed but 'aetherlink --version' failed: $_"
    }
}

# ─── Configure Claude Desktop ────────────────────────────────────────────────

function Setup-ClaudeDesktop {
    Write-Host ""
    Write-Info "Configuring Claude Desktop..."

    $configDir = "$env:APPDATA\Claude"
    $configFile = "$configDir\claude_desktop_config.json"

    # Try the built-in --setup first (it pins the config to this very install)
    try {
        & $script:AetherlinkCmd @($script:AetherlinkArgs + "--setup") 2>&1
        if ($LASTEXITCODE -eq 0) { return }
    } catch { }

    # Manual fallback
    $config = @{ mcpServers = @{} }
    if (Test-Path $configFile) {
        try {
            $existing = Get-Content $configFile -Raw | ConvertFrom-Json
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
        args = @($script:AetherlinkArgs)
    }

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
        @("multimon-ng", "POCSAG decoder"),
        @("rtl_fm",      "FM demodulator for POCSAG"),
        @("AIS-catcher", "AIS receiver")
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
