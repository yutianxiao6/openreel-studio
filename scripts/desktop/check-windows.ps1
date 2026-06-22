Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")

function Write-Check {
  param([string] $Message)
  Write-Host "[check] $Message"
}

function Assert-Command {
  param(
    [string] $Name,
    [string] $Hint
  )
  $cmd = Get-Command $Name -ErrorAction SilentlyContinue
  if ($null -eq $cmd) {
    throw "$Name was not found. $Hint"
  }
}

function Assert-NodeVersion {
  $versionText = (& node --version).Trim()
  if ($LASTEXITCODE -ne 0) {
    throw "node --version failed."
  }
  $major = [int]($versionText.TrimStart("v").Split(".")[0])
  if ($major -lt 20) {
    throw "Node.js 20+ is required. Current version: $versionText"
  }
  Write-Check "Node.js $versionText"
}

function Assert-PythonVersion {
  $versionText = (& python --version 2>&1).Trim()
  if ($LASTEXITCODE -ne 0) {
    throw "python --version failed."
  }
  if ($versionText -notmatch "Python\s+(\d+)\.(\d+)") {
    throw "Could not parse Python version: $versionText"
  }
  $major = [int]$Matches[1]
  $minor = [int]$Matches[2]
  if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 11)) {
    throw "Python 3.11+ is required. Current version: $versionText"
  }
  Write-Check $versionText
}

if ($env:OS -ne "Windows_NT") {
  throw "Windows packaging must run on Windows. PyInstaller cannot cross-build the Windows API executable from Linux/macOS."
}

if ($env:PROCESSOR_ARCHITECTURE -ne "AMD64") {
  Write-Warning "This script targets x64 installers. Current PROCESSOR_ARCHITECTURE=$env:PROCESSOR_ARCHITECTURE"
}

Write-Check "repo root: $Root"

Assert-Command "node" "Install Node.js 20 LTS from https://nodejs.org/."
Assert-NodeVersion
Assert-Command "npm" "Install Node.js 20 LTS from https://nodejs.org/."
Assert-Command "pnpm" "Run: npm install -g pnpm"
Write-Check "pnpm $((& pnpm --version).Trim())"
Assert-Command "python" "Install Python 3.11+ from https://www.python.org/downloads/windows/ and enable PATH."
Assert-PythonVersion
Assert-Command "uv" "Install uv with: pip install uv"
Write-Check "uv $((& uv --version).Trim())"

$requiredPaths = @(
  "package.json",
  "pnpm-lock.yaml",
  "apps\web\package.json",
  "apps\api\pyproject.toml",
  "apps\api\app\desktop_server.py",
  "apps\desktop\package.json",
  "packaging\pyinstaller\openreel-api.spec",
  "scripts\desktop\stage-web.mjs"
)

foreach ($relative in $requiredPaths) {
  $path = Join-Path $Root $relative
  if (-not (Test-Path $path)) {
    throw "Required packaging input is missing: $relative"
  }
}

Write-Host "Windows packaging prerequisites look OK."
