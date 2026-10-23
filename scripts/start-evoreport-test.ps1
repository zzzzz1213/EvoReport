[CmdletBinding()]
param(
    # 覆盖默认测试根目录；默认在系统临时目录下创建，含秒级时间戳与随机后缀，
    # 避免短时间内重复启动时复用同一个测试库。
    [string]$TestRoot = (Join-Path $env:TEMP ("evoreport-test-" + (Get-Date -Format "yyyyMMdd-HHmmss") + "-" + [guid]::NewGuid().ToString("N").Substring(0, 8))),
    # 覆盖默认端口；默认 8091，避免与正式服务的 8090 冲突。
    [int]$Port = 8091
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot

# 优先复用正式启动脚本的 python 路径，其次回退到 PATH 中的 python。
$pythonPath = 'E:\python\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $cmd) {
        throw "未找到可用的 Python，请修改脚本中的 `$pythonPath 或将其加入 PATH。"
    }
    $pythonPath = $cmd.Source
}

# 本脚本会读取或覆盖的环境变量；启动前保存原值，结束时恢复，避免污染调用方会话。
$managedEnvNames = @(
    'EVOREPORT_DATABASE_PATH',
    'EVOREPORT_ARTIFACT_PATH',
    'EVOREPORT_PORT',
    'EVOREPORT_HOST',
    'ARK_API_KEY'
)
$savedEnv = @{}
foreach ($name in $managedEnvNames) {
    if (Test-Path -LiteralPath ("Env:" + $name)) {
        $savedEnv[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
    }
}

# 独立测试目录：数据库与 artifact（导出/备份）分开，都放在项目目录之外。
$dbDir    = Join-Path $TestRoot 'data'
$artifactDir = Join-Path $TestRoot 'artifacts'
New-Item -ItemType Directory -Force -Path $dbDir, $artifactDir | Out-Null
$databasePath = Join-Path $dbDir 'test.db'

Write-Host "=============================================="
Write-Host " EvoReport 隔离测试环境"
Write-Host "=============================================="
Write-Host (" 端口        : {0}" -f $Port)
Write-Host (" 数据库      : {0}" -f $databasePath)
Write-Host (" 导出/备份   : {0}" -f $artifactDir)
Write-Host (" 测试根目录  : {0}" -f $TestRoot)
Write-Host " 说明: 不读写项目目录下的 exports/ 与 backups/；"
Write-Host "       强制本机地址与本地 Writer（不继承外部 ARK_API_KEY）。"
Write-Host " 停止后如需清理，可删除上述测试根目录。"
Write-Host "=============================================="

# 仅在此进程内设置，不影响系统或正式环境变量；结束时恢复调用方原值。
$env:EVOREPORT_DATABASE_PATH = $databasePath
$env:EVOREPORT_ARTIFACT_PATH = $artifactDir
$env:EVOREPORT_PORT = [string]$Port
# 强制监听本机地址，避免继承外部 EVOREPORT_HOST。
$env:EVOREPORT_HOST = '127.0.0.1'
# 强制使用本地 Writer：确保测试进程不继承外部 ARK_API_KEY，不发起外部模型调用。
Remove-Item Env:ARK_API_KEY -ErrorAction SilentlyContinue

Set-Location -LiteralPath $projectRoot
try {
    & $pythonPath -m evoreport
    exit $LASTEXITCODE
}
finally {
    # 不删除任何目录；测试产物保留在 TestRoot，由使用者决定是否清理。
    foreach ($name in $managedEnvNames) {
        if ($savedEnv.ContainsKey($name)) {
            [Environment]::SetEnvironmentVariable($name, $savedEnv[$name], 'Process')
        }
        else {
            [Environment]::SetEnvironmentVariable($name, $null, 'Process')
        }
    }
}
