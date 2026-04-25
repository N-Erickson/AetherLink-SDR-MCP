# AetherLink SDR MCP - Windows Installer
# Run: powershell -ExecutionPolicy Bypass -File install.ps1

$ErrorActionPreference = "Stop"

$RepoUrl = "https://github.com/N-Erickson/AetherLink-SDR-MCP"
$InstallDir = if ($env:AETHERLINK_DIR) { $env:AETHERLINK_DIR } else { "$env:USERPROFILE\AetherLink-SDR-MCP" }
$PythonMin = "3.10"

# ─── Helpers ──────────────────────────────────────────────────────────────────

function Write-Info  { Write-Host "[INFO] " -ForegroundColor Blue -NoNewline; Write-Host $args }
function Write-Ok    { Write-Host "[OK] " -ForegroundColor Green -NoNewline; Write-Host $args }
function Write-Warn  { Write-Host "[WARN] " -ForegroundColor Yellow -NoNewline; Write-Host $args }
function Write-Fail  { Write-Host "[ERROR] " -ForegroundColor Red -NoNewline; Write-Host $args; exit 1 }

function Prompt-YesNo {
    param([string]$Message)
    $answer = Read-Host "$Message [Y/n]"
    if ([string]::IsNullOrEmpty($answer)) { $answer = "Y" }
    return $answer -match "^[Yy]"
}

# ─── Check Python ─────────────────────────────────────────────────────────────

function Check-Python {
    $python = $null
    foreach ($candidate in @("python", "python3", "py")) {
        try {
            $ver = & $candidate -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
            if ($ver) {
                $parts = $ver.Split(".")
                if ([int]$parts[0] -ge 3 -and [int]$parts[1] -ge 10) {
                    $python = $candidate
                    break
                }
            }
        } catch { }
    }

    if (-not $python) {
        Write-Fail "Python >= $PythonMin not found. Download from https://www.python.org/downloads/"
    }

    $script:Python = $python
    $version = & $python --version 2>&1
    Write-Ok "Python: $version"
}

# ─── Check Git ────────────────────────────────────────────────────────────────

function Check-Git {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Write-Fail "Git not found. Download from https://git-scm.com/download/win"
    }
    Write-Ok "Git found"
}

# ─── System dependencies info ─────────────────────────────────────────────────

function Install-SystemDeps {
    Write-Info "Checking system dependencies..."
    Write-Host ""

    # RTL-SDR drivers
    if (Get-Command rtl_test -ErrorAction SilentlyContinue) {
        Write-Ok "RTL-SDR drivers found"
    } else {
        Write-Warn "RTL-SDR drivers not found"
        Write-Host "  Download from: https://osmocom.org/projects/rtl-sdr/wiki"
        Write-Host "  Or use: winget install osmocom.rtl-sdr (if available)"
        Write-Host "  After installing, add the bin directory to your PATH"
        Write-Host ""
    }

    # rtl_433
    if (Get-Command rtl_433 -ErrorAction SilentlyContinue) {
        Write-Ok "rtl_433 found"
    } else {
        Write-Warn "rtl_433 not found (optional - needed for ISM band scanning)"
        Write-Host "  Download from: https://github.com/merbanan/rtl_433/releases"
        Write-Host ""
    }

    # SatDump
    if (Get-Command satdump -ErrorAction SilentlyContinue) {
        Write-Ok "SatDump found"
    } else {
        Write-Warn "SatDump not found (optional - needed for satellite decoding)"
        Write-Host "  Download from: https://github.com/SatDump/SatDump/releases"
        Write-Host ""
    }

    # Check for Zadig (USB driver)
    Write-Host ""
    Write-Info "IMPORTANT: Windows requires Zadig to replace the RTL-SDR USB driver."
    Write-Host "  1. Download Zadig from https://zadig.akeo.ie/"
    Write-Host "  2. Plug in your RTL-SDR"
    Write-Host "  3. In Zadig: Options > List All Devices"
    Write-Host "  4. Select 'Bulk-In, Interface (Interface 0)'"
    Write-Host "  5. Set driver to 'WinUSB' and click 'Replace Driver'"
    Write-Host ""
}

# ─── Clone or update repo ────────────────────────────────────────────────────

function Setup-Repo {
    if (Test-Path "$InstallDir\.git") {
        Write-Info "Repository exists at $InstallDir, pulling latest..."
        try {
            git -C $InstallDir pull --ff-only 2>$null
        } catch {
            Write-Warn "Could not pull latest (you may have local changes)"
        }
    } else {
        Write-Info "Cloning repository to $InstallDir..."
        git clone $RepoUrl $InstallDir
    }
    Write-Ok "Repository ready at $InstallDir"
}

# ─── Set up Python environment ────────────────────────────────────────────────

function Setup-PythonEnv {
    Set-Location $InstallDir

    if (-not (Test-Path "venv")) {
        Write-Info "Creating virtual environment..."
        & $script:Python -m venv venv
    }

    Write-Info "Installing Python dependencies..."
    & venv\Scripts\pip install --upgrade pip -q 2>$null
    & venv\Scripts\pip install -e . -q 2>$null
    Write-Ok "Python environment ready"

    # Verify
    & venv\Scripts\python -c "from sdr_mcp.server import SDRMCPServer; print('AetherLink imports OK')" 2>$null
    Write-Ok "AetherLink verified"
}

# ─── Generate Claude Desktop config ──────────────────────────────────────────

function Setup-ClaudeDesktop {
    $configDir = "$env:APPDATA\Claude"
    $configFile = "$configDir\claude_desktop_config.json"
    $pythonPath = "$InstallDir\venv\Scripts\python.exe"
    # Escape backslashes for JSON
    $pythonPathJson = $pythonPath.Replace("\", "\\")
    $installDirJson = $InstallDir.Replace("\", "\\")

    Write-Host ""
    Write-Info "Claude Desktop MCP configuration:"

    $snippet = @"
{
  "mcpServers": {
    "aetherlink": {
      "command": "$pythonPathJson",
      "args": ["-m", "sdr_mcp.server"],
      "cwd": "$installDirJson"
    }
  }
}
"@

    if (Test-Path $configFile) {
        $content = Get-Content $configFile -Raw
        if ($content -match "aetherlink") {
            Write-Ok "Claude Desktop already configured for AetherLink"
            return
        }

        Write-Warn "Existing config found at $configFile"
        Write-Host "  Add this to your mcpServers section:"
        Write-Host ""
        Write-Host "    `"aetherlink`": {" -ForegroundColor Yellow
        Write-Host "      `"command`": `"$pythonPathJson`"," -ForegroundColor Yellow
        Write-Host "      `"args`": [`"-m`", `"sdr_mcp.server`"]," -ForegroundColor Yellow
        Write-Host "      `"cwd`": `"$installDirJson`"" -ForegroundColor Yellow
        Write-Host "    }" -ForegroundColor Yellow
    } else {
        if (Prompt-YesNo "  Create Claude Desktop config automatically?") {
            New-Item -ItemType Directory -Force -Path $configDir | Out-Null
            $snippet | Out-File -Encoding utf8 $configFile
            Write-Ok "Config written to $configFile"
        } else {
            Write-Host ""
            Write-Host "  Add this to $configFile`:"
            Write-Host ""
            Write-Host $snippet
        }
    }
}

# ─── Summary ──────────────────────────────────────────────────────────────────

function Print-Summary {
    Write-Host ""
    Write-Host "========================================================" -ForegroundColor Green
    Write-Host "  AetherLink SDR MCP - Installation Complete" -ForegroundColor Green
    Write-Host "========================================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "  Install location: $InstallDir"
    Write-Host "  Python:           $InstallDir\venv\Scripts\python.exe"
    Write-Host ""
    Write-Host "  System tools:"

    foreach ($tool in @(
        @("rtl_test",    "RTL-SDR drivers"),
        @("rtl_adsb",    "ADS-B decoder"),
        @("rtl_433",     "ISM band decoder"),
        @("satdump",     "Satellite decoder"),
        @("multimon-ng", "POCSAG decoder")
    )) {
        if (Get-Command $tool[0] -ErrorAction SilentlyContinue) {
            Write-Host "    " -NoNewline; Write-Host "+" -ForegroundColor Green -NoNewline; Write-Host " $($tool[1]) ($($tool[0]))"
        } else {
            Write-Host "    " -NoNewline; Write-Host "x" -ForegroundColor Yellow -NoNewline; Write-Host " $($tool[1]) ($($tool[0])) - not installed"
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
Setup-Repo
Setup-PythonEnv
Setup-ClaudeDesktop
Print-Summary
