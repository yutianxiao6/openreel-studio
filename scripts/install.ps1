$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

try {
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
} catch {
  # Older PowerShell/.NET combinations may not expose this setting; continue.
}

$Repo = "yutianxiao6/openreel-studio"
$Api = "https://api.github.com/repos/$Repo/releases/latest"
$DownloadDir = if ($env:OPENREEL_DOWNLOAD_DIR) { $env:OPENREEL_DOWNLOAD_DIR } else { Join-Path $HOME "Downloads" }
$Headers = @{ "User-Agent" = "openreel-installer" }

function Invoke-WithRetry {
  param(
    [Parameter(Mandatory = $true)] [scriptblock] $Script,
    [int] $Attempts = 3
  )

  $lastError = $null
  for ($i = 1; $i -le $Attempts; $i++) {
    try {
      return & $Script
    } catch {
      $lastError = $_
      if ($i -lt $Attempts) {
        Start-Sleep -Seconds ([Math]::Min(2 * $i, 6))
      }
    }
  }
  throw $lastError
}

function Invoke-WebRequestCompat {
  param(
    [Parameter(Mandatory = $true)] [string] $Uri,
    [Parameter(Mandatory = $true)] [string] $OutFile
  )

  $params = @{
    Uri = $Uri
    OutFile = $OutFile
    Headers = $Headers
    TimeoutSec = 300
  }
  if ($PSVersionTable.PSVersion.Major -lt 6) {
    $params.UseBasicParsing = $true
  }

  Invoke-WithRetry -Script { Invoke-WebRequest @params } -Attempts 3
}

function Invoke-Download {
  param(
    [Parameter(Mandatory = $true)] [string] $Uri,
    [Parameter(Mandatory = $true)] [string] $OutFile
  )

  try {
    Invoke-WebRequestCompat -Uri $Uri -OutFile $OutFile
    return
  } catch {
    $firstError = $_
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($curl) {
      Write-Host "PowerShell download failed, retrying with curl.exe..."
      & $curl.Source -L --fail --retry 3 --retry-delay 2 -A "openreel-installer" -o "$OutFile" "$Uri"
      if ($LASTEXITCODE -eq 0) {
        return
      }
    }
    throw $firstError
  }
}

Write-Host "Fetching latest OpenReel Studio release..."
try {
  $Release = Invoke-WithRetry -Script { Invoke-RestMethod -Uri $Api -Headers $Headers -TimeoutSec 60 } -Attempts 3
} catch {
  throw @"
Failed to connect to GitHub API:
  $Api

Check whether this Windows machine can access github.com/api.github.com, or configure a proxy/VPN and run the command again.
Original error: $($_.Exception.Message)
"@
}

$Asset = $Release.assets |
  Where-Object { $_.name -match '\.exe$' -and $_.name -match 'Setup' } |
  Select-Object -First 1

if (-not $Asset) {
  $Asset = $Release.assets | Where-Object { $_.name -match '\.exe$' } | Select-Object -First 1
}

if (-not $Asset) {
  throw "No Windows installer asset was found in the latest release: $($Release.tag_name)"
}

New-Item -ItemType Directory -Force -Path $DownloadDir | Out-Null
$OutFile = Join-Path $DownloadDir $Asset.name

Write-Host "Downloading $($Asset.name)..."
try {
  Invoke-Download -Uri $Asset.browser_download_url -OutFile $OutFile
} catch {
  throw @"
Failed to download the Windows installer:
  $($Asset.browser_download_url)

If GitHub release assets are blocked on this network, download it in a browser or through a proxy/VPN:
  https://github.com/$Repo/releases/latest

Original error: $($_.Exception.Message)
"@
}

Write-Host "Downloaded: $OutFile"
if ($env:OPENREEL_NO_RUN -ne "1") {
  Write-Host "Starting installer..."
  Start-Process -FilePath $OutFile
}
