# mmg-daemon.ps1 - exact-instance mmg serve watchdog
$ErrorActionPreference = 'Stop'

$scriptDir = $PSScriptRoot
$envFile = Join-Path $scriptDir '.env'
$stateFile = Join-Path $scriptDir '.mmg-instance.json'
$userHome = $env:USERPROFILE
$mmg = Join-Path $userHome '.local\bin\mmg.exe'
if (-not (Test-Path -LiteralPath $mmg)) {
  $resolved = Get-Command mmg -ErrorAction SilentlyContinue
  if ($resolved) { $mmg = $resolved.Source }
}
if (-not (Test-Path -LiteralPath $mmg)) {
  Write-Error 'mmg.exe not found. Install via: uv tool install mcp-manager-gateway'
  exit 1
}

function Read-EnvConfig {
  $result = @{}
  if (Test-Path -LiteralPath $envFile) {
    foreach ($line in (Get-Content -LiteralPath $envFile -ErrorAction Stop)) {
      $text = $line.Trim()
      if ($text -eq '' -or $text.StartsWith('#')) { continue }
      $index = $text.IndexOf('=')
      if ($index -le 0) { continue }
      $name = $text.Substring(0, $index).Trim().ToUpperInvariant()
      $value = $text.Substring($index + 1).Trim()
      if ($value.Length -ge 2 -and (
          ($value.StartsWith("'") -and $value.EndsWith("'")) -or
          ($value.StartsWith('"') -and $value.EndsWith('"')))) {
        $value = $value.Substring(1, $value.Length - 2)
      }
      $result[$name] = $value
    }
  }
  if (-not $result['HOST']) { $result['HOST'] = '127.0.0.1' }
  if (-not $result['PORT']) { $result['PORT'] = '8765' }
  if (-not $result['PUBLIC_URL']) {
    $result['PUBLIC_URL'] = "http://$($result['HOST']):$($result['PORT'])"
  }
  return $result
}

$cfg = Read-EnvConfig
$env:HOST = $cfg['HOST']
$env:PORT = $cfg['PORT']
$env:PUBLIC_URL = $cfg['PUBLIC_URL']
if ($cfg['DATA_DIR']) { $env:DATA_DIR = $cfg['DATA_DIR'] }
if ($cfg['DATABASE_URL']) { $env:DATABASE_URL = $cfg['DATABASE_URL'] }
if ($cfg['COOKIE_SECURE']) { $env:COOKIE_SECURE = $cfg['COOKIE_SECURE'] }
$workDir = Join-Path $userHome '.mcp-manager'
if (-not (Test-Path -LiteralPath $workDir)) {
  New-Item -ItemType Directory -Path $workDir -Force | Out-Null
}
$env:MCP_MANAGER_HOME = $workDir
$daemonStarted = (Get-Process -Id $PID).StartTime.ToUniversalTime().ToString('o')

function Write-InstanceState([System.Diagnostics.Process]$child) {
  $state = [ordered]@{
    daemon_pid = $PID
    daemon_started_at = $daemonStarted
    child_pid = $child.Id
    child_started_at = $child.StartTime.ToUniversalTime().ToString('o')
    executable = [IO.Path]::GetFullPath($mmg)
    script_path = [IO.Path]::GetFullPath($PSCommandPath)
    home_path = [IO.Path]::GetFullPath($workDir)
    port = [int]$cfg['PORT']
  }
  $temporary = "$stateFile.$PID.tmp"
  $state | ConvertTo-Json -Compress | Set-Content -LiteralPath $temporary -Encoding UTF8
  Move-Item -LiteralPath $temporary -Destination $stateFile -Force
}

Set-Location -LiteralPath $workDir
try {
  while ($true) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = $mmg
    $start.Arguments = 'serve'
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.WorkingDirectory = $workDir
    $process = [System.Diagnostics.Process]::Start($start)
    Write-InstanceState $process
    $process.WaitForExit()
    $exitCode = $process.ExitCode
    $ErrorActionPreference = $previous
    if ($exitCode -eq 0) { exit 0 }
    Start-Sleep -Seconds 10
  }
} finally {
  if (Test-Path -LiteralPath $stateFile) {
    try {
      $saved = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
      if ([int]$saved.daemon_pid -eq $PID) {
        Remove-Item -LiteralPath $stateFile -Force -ErrorAction SilentlyContinue
      }
    } catch {}
  }
}
