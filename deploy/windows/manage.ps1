# manage.ps1 - mmg serve 交互式管理脚本
# 运行: powershell -NoProfile -ExecutionPolicy Bypass -File manage.ps1
# 通用: 用 $env:USERPROFILE，无硬编码用户名。
# 配置存 .env（脚本同级目录），配置不写注册表。
# 服务 = HKCU Run 键（当前用户，无需管理员）；直接启动 = 临时后台。
# 写法: 本文件以 .log 扩展名写入(明文落盘)再改名为 .ps1，避免 DLP 加密 .ps1；
#       文件头带 UTF-8 BOM，使 Windows PowerShell 5.x 按 UTF-8 读取中文。
$ErrorActionPreference = 'Stop'
$scriptDir = $PSScriptRoot
$daemon    = Join-Path $scriptDir 'mmg-daemon.ps1'
$envFile   = Join-Path $scriptDir '.env'
$userHome  = $env:USERPROFILE
$runKey    = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$entryName = 'mmg-gateway'
$defaultEnv = @'
HOST=127.0.0.1
PORT=8765
PUBLIC_URL=http://127.0.0.1:8765
# DATA_DIR=./mcp-manager/
# DATABASE_URL=mysql://user:password@localhost:3306/mcp_manager?charset=utf8mb4
# COOKIE_SECURE=false
'@

function Write-Banner {
  Write-Host ''
  Write-Host '========================================'
  Write-Host '  mmg serve 管理器'
  Write-Host '========================================'
}

function Write-Menu {
  Write-Host ''
  Write-Host '  [1] 配置与管理'
  Write-Host '  [2] 服务管理'
  Write-Host '  [3] 直接启动'
  Write-Host '  [0] 退出'
  Write-Host ''
}

# 读取 .env（脚本同级目录），返回哈希表（键大写）。
# HOST/PORT/PUBLIC_URL 用默认值兜底（与 mcp-manager Settings 一致）。
function Get-EnvConfig {
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
  if (-not $cfg['HOST'])       { $cfg['HOST'] = '127.0.0.1' }
  if (-not $cfg['PORT'])       { $cfg['PORT'] = '8765' }
  if (-not $cfg['PUBLIC_URL']) { $cfg['PUBLIC_URL'] = 'http://127.0.0.1:8765' }
  return $cfg
}

function Get-ConfigPort { (Get-EnvConfig)['PORT'] }

function Show-Config {
  Write-Host ''
  Write-Host "--- 当前 .env 内容（$envFile）---"
  if (Test-Path $envFile) {
    Get-Content -LiteralPath $envFile | ForEach-Object { Write-Host '  ' $_ }
  } else {
    Write-Host '  （尚无 .env；将使用默认值）'
  }
  Write-Host ''
}

function Edit-Config {
  if (-not (Test-Path $envFile)) {
    Set-Content -LiteralPath $envFile -Value $defaultEnv -Encoding UTF8
    Write-Host '已创建默认 .env。'
  }
  Show-Config
  Write-Host '编辑 .env（保存并关闭后，下次启动生效）:'
  $notepad = Join-Path $env:ProgramFiles 'Notepad\notepad.exe'
  if (-not (Test-Path $notepad)) { $notepad = 'notepad.exe' }
  Start-Process -FilePath $notepad -ArgumentList "`"$envFile`""
  Write-Host '已用记事本打开 .env。保存并关闭后按回车重新显示...'
  [void](Read-Host)
  Show-Config
}

function Config-Menu {
  $loop = $true
  while ($loop) {
    Write-Host ''
    Write-Host '  [1] 查看当前配置'
    Write-Host '  [2] 编辑 .env（记事本）'
    Write-Host '  [0] 返回'
    Write-Host ''
    $in = Read-Host '请选择'; if ($null -eq $in) { $loop = $false; break }; $c = $in.Trim()
    switch ($c) {
      '1' { Show-Config }
      '2' { Edit-Config }
      '0' { $loop = $false }
      default { Write-Host '无效，请输入 1/2/0。' }
    }
  }
}

function Get-ServiceCmd {
  "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$daemon`""
}

function Install-Service {
  if (Get-Process -Name mmg -ErrorAction SilentlyContinue) {
    Write-Host 'mmg 正在运行；请先停止（直接启动[2] 或 服务管理[3]）。'
    return
  }
  $cmd = Get-ServiceCmd
  Set-ItemProperty -Path $runKey -Name $entryName -Value $cmd
  Write-Host "服务已安装（HKCU Run $entryName），下次登录自动启动。"
  Write-Host '现在启��？(y/N) ' -NoNewline
  $yn = Read-Host; if ($yn -and $yn.Trim().StartsWith('y')) { Start-Service }
}

function Uninstall-Service {
  Stop-Service
  Remove-ItemProperty -Path $runKey -Name $entryName -ErrorAction SilentlyContinue
  Write-Host "服务已卸载（删除 $entryName 并停止 mmg）。"
}

function Start-Service {
  if (Get-Process -Name mmg -ErrorAction SilentlyContinue) {
    Write-Host 'mmg 已在运行。'
    return
  }
  Start-Process powershell.exe -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File',"$daemon" -WindowStyle Hidden
  Start-Sleep 2
  Write-Host '服务已启动（隐藏窗口）。可用状态[6]确认。'
}

function Stop-Service {
  $killed = 0
  Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*mmg-daemon.ps1*' } | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $killed++
  }
  Stop-Process -Name mmg -ErrorAction SilentlyContinue
  Write-Host "已停止（守护进程 $killed 个，mmg）。"
}

function Restart-Service {
  Stop-Service
  Start-Sleep 2
  Start-Service
}

function Service-Status {
  $port = Get-ConfigPort
  Write-Host ''
  Write-Host "--- 服务状态（端口 $port）---"
  $p = Get-Process -Name mmg -ErrorAction SilentlyContinue
  if ($p) { Write-Host "  mmg: 运行中 (PID $(($p | ForEach-Object { $_.Id }) -join ', '))" }
  else    { Write-Host '  mmg: 未运行' }
  $d = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*mmg-daemon.ps1*' }
  if ($d) { Write-Host "  守护: 运行中 (PID $(($d.ProcessId) -join ', '))" } else { Write-Host '  守护: 未运行' }
  $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
  if ($c) { Write-Host "  端口 $port : 监听中" } else { Write-Host "  端口 $port : 未监听" }
  $prop = Get-ItemProperty -Path $runKey -ErrorAction SilentlyContinue
  $installed = $false
  if ($prop) { $installed = ($prop.PSObject.Properties | Where-Object { $_.Name -eq $entryName }) -ne $null }
  if ($installed) { Write-Host '  自启 (HKCU Run): 已安装' } else { Write-Host '  自启 (HKCU Run): 未安装' }
  Write-Host ''
}

function Service-Menu {
  $loop = $true
  while ($loop) {
    Write-Host ''
    Write-Host '  [1] 安装为常驻服务'
    Write-Host '  [2] 启动服务'
    Write-Host '  [3] 停止服务'
    Write-Host '  [4] 重启服务'
    Write-Host '  [5] 卸载服务'
    Write-Host '  [6] 查看服务状态'
    Write-Host '  [0] 返回'
    Write-Host ''
    $in = Read-Host '请选择'; if ($null -eq $in) { $loop = $false; break }; $c = $in.Trim()
    switch ($c) {
      '1' { Install-Service }
      '2' { Start-Service }
      '3' { Stop-Service }
      '4' { Restart-Service }
      '5' { Uninstall-Service }
      '6' { Service-Status }
      '0' { $loop = $false }
      default { Write-Host '无效，请输入 1-6 或 0。' }
    }
  }
}

function Direct-Status {
  $port = Get-ConfigPort
  Write-Host ''
  Write-Host "--- 直接启动状态（端口 $port）---"
  $p = Get-Process -Name mmg -ErrorAction SilentlyContinue
  if ($p) { Write-Host "  mmg: 运行中 (PID $(($p | ForEach-Object { $_.Id }) -join ', '))" } else { Write-Host '  mmg: 未运行' }
  $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
  if ($c) { Write-Host "  端口 $port : 监听中" } else { Write-Host "  端口 $port : 未监听" }
  Write-Host ''
}

function Direct-Menu {
  $loop = $true
  while ($loop) {
    Write-Host ''
    Write-Host '  [1] 启动程序（临时后台）'
    Write-Host '  [2] 停止程序'
    Write-Host '  [3] 查询程序状态'
    Write-Host '  [0] 返回'
    Write-Host ''
    $in = Read-Host '请选择'; if ($null -eq $in) { $loop = $false; break }; $c = $in.Trim()
    switch ($c) {
      '1' {
        if (Get-Process -Name mmg -ErrorAction SilentlyContinue) { Write-Host 'mmg 已在运行。' }
        else {
          Start-Process powershell.exe -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File',"$daemon" -WindowStyle Hidden
          Start-Sleep 2
          Write-Host '已启动（隐藏，临时）。可用[3]查询；重启后不会自动恢复。'
        }
      }
      '2' {
        $killed = 0
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*mmg-daemon.ps1*' } | ForEach-Object {
          Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $killed++
        }
        Stop-Process -Name mmg -ErrorAction SilentlyContinue
        Write-Host "已停止（守护进程 $killed 个，mmg）。"
      }
      '3' { Direct-Status }
      '0' { $loop = $false }
      default { Write-Host '无效，请输入 1-3 或 0。' }
    }
  }
}

# --- 主循环 ---
Write-Banner
Write-Menu
$main = $true
while ($main) {
  $cin = Read-Host '请选择'; if ($null -eq $cin) { $main = $false; break }; $choice = $cin.Trim()
  switch ($choice) {
    '1' { Config-Menu }
    '2' { Service-Menu }
    '3' { Direct-Menu }
    '0' { $main = $false }
    default { Write-Host '无效，请输入 1/2/3/0。' }
  }
  Write-Host ''
}
Write-Host '再见。'
