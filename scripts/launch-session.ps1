# Jarvis -- Launch Session (Windows)
# Counterpart to launch-session.sh. Invoked by scripts/clap-trigger.py
# on Windows.

$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Workspace = Split-Path -Parent $ScriptDir

# Load .env from the project root if present so JARVIS_AUTH_TOKEN is
# available to both the server and to Invoke-WebRequest below.
$EnvFile = Join-Path $Workspace '.env'
if (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith('#') -and $line.Contains('=')) {
            $key, $value = $line.Split('=', 2)
            $key = $key.Trim()
            $value = $value.Trim().Trim('"').Trim("'")
            [Environment]::SetEnvironmentVariable($key, $value, 'Process')
        }
    }
}

$AuthHeader = @{}
if ($env:JARVIS_AUTH_TOKEN) {
    $AuthHeader = @{ 'X-Jarvis-Token' = $env:JARVIS_AUTH_TOKEN }
}

function Invoke-Wake {
    try {
        Invoke-WebRequest -Uri 'http://localhost:8340/activate' `
            -UseBasicParsing -TimeoutSec 3 -Headers $AuthHeader | Out-Null
    } catch {
        Write-Host "[jarvis] Wake-Signal fehlgeschlagen: $_"
    }
}

# 1. Server starten falls Port 8340 nicht gebunden ist.
$portOpen = $false
try {
    $portOpen = (Test-NetConnection -ComputerName 'localhost' -Port 8340 `
        -InformationLevel Quiet -WarningAction SilentlyContinue)
} catch { $portOpen = $false }

if (-not $portOpen) {
    Start-Process -FilePath 'python' `
        -ArgumentList @('server.py') `
        -WorkingDirectory $Workspace `
        -RedirectStandardOutput (Join-Path $Workspace 'jarvis.log') `
        -RedirectStandardError  (Join-Path $Workspace 'jarvis.log') `
        -WindowStyle Hidden | Out-Null
    Start-Sleep -Seconds 3
}

# 2. Chrome: existiert bereits ein Jarvis-Tab? Dann nur Wake-Signal.
$chromeRunning = $false
$chromeProcs = Get-Process -Name 'chrome' -ErrorAction SilentlyContinue
if ($chromeProcs) {
    foreach ($p in $chromeProcs) {
        if ($p.MainWindowTitle -match 'localhost:8340|J\.A\.R\.V\.I\.S') {
            $chromeRunning = $true; break
        }
    }
}

if ($chromeRunning) {
    Invoke-Wake
    Write-Host '[jarvis] Wake-Signal gesendet.'
}
else {
    # Chrome neu starten mit Autoplay-Flag im --app-Modus.
    $chromeExe = $null
    foreach ($candidate in @(
        "${env:ProgramFiles}\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
        "${env:LocalAppData}\Google\Chrome\Application\chrome.exe"
    )) {
        if (Test-Path $candidate) { $chromeExe = $candidate; break }
    }
    if (-not $chromeExe) {
        Write-Host '[jarvis] Google Chrome nicht gefunden. Bitte installieren.'
        exit 1
    }

    Start-Process -FilePath $chromeExe `
        -ArgumentList @('--autoplay-policy=no-user-gesture-required',
                        '--app=http://localhost:8340') | Out-Null

    Start-Sleep -Seconds 3
    Invoke-Wake
    Write-Host '[jarvis] Session gestartet.'
}
