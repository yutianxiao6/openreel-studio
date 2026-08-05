param(
  [switch] $SkipInstall,
  [switch] $SkipPreflight
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$ApiDist = Join-Path $Root "dist\openreel-api"
$ApiStage = Join-Path $Root "apps\desktop\dist\resources\api\openreel-api"
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
  $env:INTERNAL_API_BASE_URL = "http://127.0.0.1:7860"

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
  Invoke-Native "Verify staged web runtime" "pnpm" @("desktop:verify:web")
  Invoke-Native "Smoke-test staged web runtime" "pnpm" @("desktop:smoke:web")

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

  $SmokeRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("openreel-packaging-smoke-" + [guid]::NewGuid().ToString("N"))
  New-Item -ItemType Directory -Force -Path $SmokeRoot | Out-Null
  $PreviousUserDataDir = $env:OPENREEL_USER_DATA_DIR
  $PreviousProjectRoot = $env:PROJECT_ROOT
  $PreviousPackagingSmoke = $env:OPENREEL_PACKAGING_SMOKE
  try {
    $env:OPENREEL_USER_DATA_DIR = $SmokeRoot
    $env:PROJECT_ROOT = $SmokeRoot
    $env:OPENREEL_PACKAGING_SMOKE = "1"
    Write-Step "Smoke-test packaged API resources"
    # PyInstaller uses the Windows GUI bootloader so media subprocesses do not
    # flash a console window. PowerShell does not wait for GUI executables when
    # they are invoked with `&`, so wait explicitly before inspecting output.
    $SmokeProcess = Start-Process `
      -FilePath (Join-Path $ApiStage "openreel-api.exe") `
      -Wait `
      -PassThru
    if ($SmokeProcess.ExitCode -ne 0) {
      throw "Smoke-test packaged API resources failed with exit code $($SmokeProcess.ExitCode)"
    }
    $UmaImageTargetCatalog = Join-Path $SmokeRoot "config\universal_model_adapter\image_targets\catalog.json"
    if (-not (Test-Path $UmaImageTargetCatalog)) {
      throw "Packaged UMA image target catalog was not installed."
    }
    $UmaTargetCatalog = Join-Path $SmokeRoot "config\universal_model_adapter\video_targets\catalog.json"
    if (-not (Test-Path $UmaTargetCatalog)) {
      throw "Packaged UMA video target catalog was not installed."
    }
    $UmaAudioTargetCatalog = Join-Path $SmokeRoot "config\universal_model_adapter\audio_targets\catalog.json"
    if (-not (Test-Path $UmaAudioTargetCatalog)) {
      throw "Packaged UMA audio target catalog was not installed."
    }
    $UmaVideoProtocol = Join-Path $SmokeRoot "config\universal_model_adapter\protocols\volcengine-seedance-video-task.json"
    if (-not (Test-Path $UmaVideoProtocol)) {
      throw "Packaged UMA video protocols were not installed."
    }
    $UmaAudioProtocol = Join-Path $SmokeRoot "config\universal_model_adapter\protocols\newapi-suno-music-task.json"
    if (-not (Test-Path $UmaAudioProtocol)) {
      throw "Packaged UMA audio protocols were not installed."
    }
    $UmaImageProtocol = Join-Path $SmokeRoot "config\universal_model_adapter\protocols\openai-compatible-images-generations.json"
    if (-not (Test-Path $UmaImageProtocol)) {
      throw "Packaged UMA image protocols were not installed."
    }
  }
  finally {
    $env:OPENREEL_USER_DATA_DIR = $PreviousUserDataDir
    $env:PROJECT_ROOT = $PreviousProjectRoot
    $env:OPENREEL_PACKAGING_SMOKE = $PreviousPackagingSmoke
    if (Test-Path $SmokeRoot) {
      Remove-Item $SmokeRoot -Recurse -Force
    }
  }

  Invoke-Native "Build Windows NSIS installer" "pnpm" @("--filter", "desktop", "package:win")
  Invoke-Native "Check Windows installer size budget" "node" @(
    "scripts/desktop/check-installer-size.mjs",
    "--target",
    "windows"
  )

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
