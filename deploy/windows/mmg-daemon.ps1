# mmg-daemon.ps1 - mmg serve daemon (crash self-heal via while loop)
# Generic: uses $env:USERPROFILE / $env:ProgramFiles; no hardcoded user path.
# Reads settings from .env in the same directory (written by manage.ps1) and
# maps them to MCP_MANAGER_* env vars for the mmg child process.
# No logging: all mmg output is discarded.
$ErrorActionPreference = 'Stop'

# --- Resolve paths (no hardcoded username) ---
$scriptDir = $PSScriptRoot
$userHome  = $env:USERPROFILE
$mmgDir    = Join-Path $userHome '.local\bin'
$mmg       = Join-Path $mmgDir 'mmg.exe'
if (-not (Test-Path $mmg)) {
  Write-Error "mmg.exe not found at $mmg. Install via: uv tool install mcp-manager-gateway"
  exit 1
}

# --- Read .env (same directory as this script) and map to MCP_MANAGER_* env vars ---
# .env format (written by manage.ps1), no MCP_MANAGER_ prefix:
#   HOST=127.0.0.1
#   PORT=8765
#   PUBLIC_URL=http://127.0.0.1:8765
#   # DATA_DIR=./mcp-manager/
#   # DATABASE_URL=mysql://user:pass@localhost:3306/mcp_manager?charset=utf8mb4
#   # COOKIE_SECURE=false
$envFile = Join-Path $scriptDir '.env'
$cfg = @{}
if (Test-Path $envFile) {
  foreach ($line in (Get-Content -LiteralPath $envFile -ErrorAction SilentlyContinue)) {
    $t = $line.Trim()
    if ($t -eq '' -or $t.StartsWith('#')) { continue }
    $idx = $t.IndexOf('=')
    if ($idx -le 0) { continue }
    $k = $t.Substring(0, $idx).Trim().ToUpperInvariant()
    $v = $t.Substring($idx + 1).Trim()
    $cfg[$k] = $v
  }
}
# Defaults (match mcp-manager Settings)
$cfg['HOST']       = if ($cfg['HOST'])       { $cfg['HOST'] }       else { '127.0.0.1' }
$cfg['PORT']       = if ($cfg['PORT'])       { $cfg['PORT'] }       else { '8765' }
$cfg['PUBLIC_URL'] = if ($cfg['PUBLIC_URL']) { $cfg['PUBLIC_URL'] } else { 'http://127.0.0.1:8765' }
if ($cfg['DATA_DIR'])     { $env:MCP_MANAGER_DATA_DIR     = $cfg['DATA_DIR'] }
if ($cfg['DATABASE_URL']) { $env:MCP_MANAGER_DATABASE_URL = $cfg['DATABASE_URL'] }
if ($cfg['COOKIE_SECURE']) { $env:MCP_MANAGER_COOKIE_SECURE = $cfg['COOKIE_SECURE'] }

# --- Explicit PATH (service/scheduled-task env has sparse PATH) ---
$systemPaths = @(
  'C:\Windows\system32'
  'C:\Windows'
  'C:\Windows\System32\Wbem'
  'C:\Windows\System32\WindowsPowerShell\v1.0\'
  'C:\Windows\System32\OpenSSH\'
)
$userPaths = @(
  (Join-Path $userHome 'AppData\Local\nvm')
  (Join-Path $env:ProgramFiles 'nodejs')
  $mmgDir
  (Join-Path $userHome 'AppData\Local\Microsoft\WindowsApps')
  (Join-Path $env:ProgramFiles 'Git\cmd')
)
$env:PATH = ($systemPaths + $userPaths) -join ';'

$workDir = Join-Path $userHome '.mcp-manager'
if (-not (Test-Path $workDir)) { New-Item -ItemType Directory -Path $workDir -Force | Out-Null }
Set-Location -LiteralPath $workDir

# --- Run loop: start mmg serve with HOST/PORT, self-heal on non-zero exit ---
while ($true) {
  # Uvicorn writes to stderr; temporarily lower ErrorActionPreference so stderr
  # is not treated as a terminating error. Output discarded (no logging).
  $prevEAP = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = $mmg
  $psi.Arguments = "serve --host $($cfg['HOST']) --port $($cfg['PORT'])"
  $psi.UseShellExecute = $false
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError = $true
  $psi.CreateNoWindow = $true
  $psi.WorkingDirectory = $workDir
  $proc = [System.Diagnostics.Process]::Start($psi)
  $proc.StandardOutput.ReadToEnd() | Out-Null
  $proc.StandardError.ReadToEnd() | Out-Null
  $proc.WaitForExit()
  $exitCode = $proc.ExitCode
  $ErrorActionPreference = $prevEAP

  if ($exitCode -eq 0) { exit 0 }
  Start-Sleep -Seconds 10
}
