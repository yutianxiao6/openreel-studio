param(
  [switch] $SkipInstall,
  [switch] $SkipPreflight
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$ApiDist = Join-Path $Root "dist\openreel-api"
$ApiStage = Join-Path $Root "apps\desktop\dist\resources\api\openreel-api"
$WebStage = Join-Path $Root "apps\desktop\dist\resources\web"
$InstallerDir = Join-Path $Root "dist\installers"
$Spec = Join-Path $Root "packaging\pyinstaller\openreel-api.spec"
$SymlinkJunctionPreload = Join-Path $Root "scripts\desktop\windows-symlink-junction.cjs"

function Write-Step {
  param([string] $Message)
  Write-Host ""
  Write-Host "==> $Message"
}

function Invoke-Native {
  param(
    [string] $Label,
    [string] $Command,
    [string[]] $Arguments
  )
  Write-Step $Label
  & $Command @Arguments
  if ($LASTEXITCODE -ne 0) {
    throw "$Label failed with exit code $LASTEXITCODE"
  }
}

Push-Location $Root
try {
  if (-not $SkipPreflight) {
    & (Join-Path $PSScriptRoot "check-windows.ps1")
  }

  $env:NEXT_PUBLIC_BASE_PATH = ""
  $env:NEXT_PUBLIC_API_BASE_URL = ""
  $env:INTERNAL_API_BASE_URL = "http://127.0.0.1:8000"

  if (-not $SkipInstall) {
    Invoke-Native "Install JavaScript dependencies" "pnpm" @("install", "--frozen-lockfile")
  }

  $PreviousNodeOptions = $env:NODE_OPTIONS
  try {
    $env:NODE_OPTIONS = "--require=$SymlinkJunctionPreload"
    if ($PreviousNodeOptions) {
      $env:NODE_OPTIONS = "$env:NODE_OPTIONS $PreviousNodeOptions"
    }
    Invoke-Native "Build Next.js standalone web runtime" "pnpm" @("--filter", "web", "build")
  }
  finally {
    $env:NODE_OPTIONS = $PreviousNodeOptions
  }
  Invoke-Native "Stage web runtime for Electron" "pnpm" @("desktop:stage:web")
  if (-not (Test-Path (Join-Path $WebStage "apps\web\server.js"))) {
    throw "Staged web server.js was not found under apps\desktop\dist\resources\web."
  }

  if (Test-Path $ApiDist) {
    Remove-Item $ApiDist -Recurse -Force
  }
  Invoke-Native "Package FastAPI runtime with PyInstaller" "uv" @(
    "run",
    "--project",
    (Join-Path $Root "apps\api"),
    "--with",
    "pyinstaller",
    "pyinstaller",
    $Spec,
    "--noconfirm"
  )

  if (Test-Path $ApiStage) {
    Remove-Item $ApiStage -Recurse -Force
  }
  New-Item -ItemType Directory -Force -Path (Split-Path $ApiStage) | Out-Null
  Copy-Item $ApiDist $ApiStage -Recurse
  if (-not (Test-Path (Join-Path $ApiStage "openreel-api.exe"))) {
    throw "Staged API executable was not found under apps\desktop\dist\resources\api\openreel-api."
  }

  Invoke-Native "Build Windows NSIS installer" "pnpm" @("--filter", "desktop", "package:win")

  Write-Step "Installer output"
  if (Test-Path $InstallerDir) {
    Get-ChildItem $InstallerDir | Sort-Object LastWriteTime -Descending | Select-Object -First 8 | Format-Table Name, Length, LastWriteTime
  } else {
    throw "Installer output directory was not created: $InstallerDir"
  }
}
finally {
  Pop-Location
}
