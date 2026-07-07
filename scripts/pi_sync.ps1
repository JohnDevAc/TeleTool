param(
  [string]$PiHost,
  [string]$PiUser,
  [string]$RemotePath,
  [string]$PiPassword,
  [switch]$IncludeConfig
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

function Import-DotEnv {
  param([Parameter(Mandatory = $true)][string]$Path)

  if (-not (Test-Path $Path)) { return }

  foreach ($line in Get-Content -LiteralPath $Path) {
    $trimmed = $line.Trim()
    if ($trimmed.Length -eq 0 -or $trimmed.StartsWith("#")) { continue }
    if ($trimmed -notmatch "^([^=\s]+)\s*=\s*(.*)$") { continue }

    $key = $Matches[1]
    $value = $Matches[2].Trim()
    if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
      $value = $value.Substring(1, $value.Length - 2)
    }

    [Environment]::SetEnvironmentVariable($key, $value, "Process")
  }
}

Import-DotEnv (Join-Path $ProjectRoot ".env.local")

if ([string]::IsNullOrWhiteSpace($PiHost)) { $PiHost = $env:TELETOOL_PI_HOST }
if ([string]::IsNullOrWhiteSpace($PiUser)) { $PiUser = $env:TELETOOL_PI_USER }
if ([string]::IsNullOrWhiteSpace($RemotePath)) { $RemotePath = $env:TELETOOL_PI_PATH }
if ([string]::IsNullOrWhiteSpace($PiPassword)) { $PiPassword = $env:TELETOOL_PI_PASSWORD }

if ([string]::IsNullOrWhiteSpace($PiHost)) { $PiHost = "raspberrypi.local" }
if ([string]::IsNullOrWhiteSpace($PiUser)) { $PiUser = "pi" }
if ([string]::IsNullOrWhiteSpace($RemotePath)) { $RemotePath = "/home/$PiUser/teletool" }

$Target = "$PiUser@$PiHost"
$SyncConfig = $IncludeConfig.IsPresent -or $env:TELETOOL_SYNC_CONFIG -eq "1"
$UsePutty = -not [string]::IsNullOrWhiteSpace($PiPassword)

function Invoke-External {
  param(
    [Parameter(Mandatory = $true)][string]$FilePath,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments
  )

  & $FilePath @Arguments
  if ($LASTEXITCODE -ne 0) {
    throw "$FilePath exited with code $LASTEXITCODE"
  }
}

if ($UsePutty) {
  $script:PlinkPath = (Get-Command plink -ErrorAction Stop).Source
  $script:PscpPath = (Get-Command pscp -ErrorAction Stop).Source
  Write-Host "Using PuTTY plink/pscp with password from local environment."
}

function Invoke-RemoteCommand {
  param([Parameter(Mandatory = $true)][string]$Command)

  if ($UsePutty) {
    Invoke-External $script:PlinkPath "-ssh" "-batch" "-pw" $PiPassword $Target $Command
  } else {
    Invoke-External ssh $Target $Command
  }
}

function Copy-ToRemote {
  param(
    [Parameter(Mandatory = $true)][string]$LocalPath,
    [Parameter(Mandatory = $true)][string]$RemoteSpec
  )

  if ($UsePutty) {
    Invoke-External $script:PscpPath "-batch" "-pw" $PiPassword $LocalPath $RemoteSpec
  } else {
    Invoke-External scp $LocalPath $RemoteSpec
  }
}

Write-Host "Syncing $ProjectRoot to $Target`:$RemotePath"
if (-not $SyncConfig) {
  Write-Host "Preserving remote config.json. Use -IncludeConfig to overwrite it."
}

Invoke-RemoteCommand "mkdir -p '$RemotePath'"

$rsync = Get-Command rsync -ErrorAction SilentlyContinue
if ($null -ne $rsync -and -not $UsePutty) {
  $exclude = @(
    ".git/",
    ".venv/",
    "venv/",
    "__pycache__/",
    ".pytest_cache/",
    ".env",
    ".env.*",
    "*.pyc"
  )
  if (-not $SyncConfig) {
    $exclude += "config.json"
  }

  $args = @("-az", "--delete")
  foreach ($item in $exclude) {
    $args += "--exclude=$item"
  }
  $args += "-e"
  $args += "ssh"
  $args += ($ProjectRoot.TrimEnd("\") + "\")
  $args += "$Target`:$RemotePath/"

  Invoke-External $rsync.Source @args
} else {
  $tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("teletool-sync-" + [System.Guid]::NewGuid().ToString("N"))
  $archive = Join-Path ([System.IO.Path]::GetTempPath()) ("teletool-sync-" + [System.Guid]::NewGuid().ToString("N") + ".tar.gz")

  New-Item -ItemType Directory -Path $tempRoot | Out-Null
  try {
    $robocopyArgs = @(
      $ProjectRoot,
      $tempRoot,
      "/E",
      "/XD",
      ".git",
      ".venv",
      "venv",
      "__pycache__",
      ".pytest_cache",
      "/DCOPY:D",
      "/XF",
      "*.pyc",
      ".env",
      ".env.*"
    )
    if (-not $SyncConfig) {
      $robocopyArgs += "config.json"
    }

    & robocopy @robocopyArgs | Out-Host
    if ($LASTEXITCODE -ge 8) {
      throw "robocopy exited with code $LASTEXITCODE"
    }

    $readOnly = [System.IO.FileAttributes]::ReadOnly
    @((Get-Item -LiteralPath $tempRoot)) + @(Get-ChildItem -LiteralPath $tempRoot -Force -Recurse) | ForEach-Object {
      if (($_.Attributes -band $readOnly) -ne 0) {
        $_.Attributes = $_.Attributes -band (-bnot $readOnly)
      }
    }

    Push-Location $tempRoot
    try {
      Invoke-External tar "-czf" $archive "."
    } finally {
      Pop-Location
    }

    Copy-ToRemote $archive "$Target`:/tmp/teletool-sync.tar.gz"
    Invoke-RemoteCommand "chmod -R u+w '$RemotePath' 2>/dev/null || true; tar --delay-directory-restore --no-same-permissions -xzf /tmp/teletool-sync.tar.gz -C '$RemotePath' && find '$RemotePath' -type d -exec chmod 755 {} \; && find '$RemotePath' -type f -exec chmod 644 {} \; && chmod +x '$RemotePath/scripts/pi_setup.sh' '$RemotePath/install_network_privileges.sh' 2>/dev/null || true; rm -f /tmp/teletool-sync.tar.gz"
  } finally {
    if (Test-Path $tempRoot) { Remove-Item -LiteralPath $tempRoot -Recurse -Force }
    if (Test-Path $archive) { Remove-Item -LiteralPath $archive -Force }
  }
}

Invoke-RemoteCommand "cd '$RemotePath' && if [ ! -f config.json ] && [ -f config.example.json ]; then cp config.example.json config.json; fi"

Write-Host "Sync complete."
