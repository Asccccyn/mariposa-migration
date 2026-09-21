# mariposa doctor: 运行时、端口、配置、数据路径检查（只读）
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Check($name, $ok, $detail) {
    $mark = if ($ok) { "[OK]  " } else { "[FAIL]" }
    Write-Host "$mark $name  $detail"
}

# Python
$py = "$root\.venv\Scripts\python.exe"
Check "venv python" (Test-Path $py) $py
if (Test-Path $py) {
    & $py --version
    $fts = & $py -c "import sqlite3;c=sqlite3.connect(':memory:');c.execute('CREATE VIRTUAL TABLE t USING fts5(x)');print('ok')"
    Check "sqlite fts5" ($fts -eq "ok") $fts
    foreach ($mod in "fastapi", "uvicorn", "pytest") {
        $v = & $py -c "import $mod; print($mod.__version__)" 2>$null
        Check "dependency $mod" ($null -ne $v) $v
    }
}

# 端口
$portBusy = (Get-NetTCPConnection -LocalPort 18780 -State Listen -ErrorAction SilentlyContinue) -ne $null
Check "port 18780 free" (-not $portBusy) $(if ($portBusy) { "被占用（若为本项目服务则正常）" } else { "空闲" })

# 数据目录
Check "runtime dir" (Test-Path "$root\runtime") "$root\runtime"
Check "formal db" (Test-Path "$root\runtime\formal") "$root\runtime\formal"
Check "workspace db" (Test-Path "$root\runtime\workspace") "$root\runtime\workspace"

# 旧生产边界（只确认存在，不读写）
foreach ($legacy in "D:\Ombre-Brain-dev", "D:\Ombre-Brain-main2.5") {
    Check "legacy readonly-present $legacy" (Test-Path $legacy) "存在；mariposa 不写入"
}

# CC 前置（仅探测，不启动）
$cc = Get-Command claude -ErrorAction SilentlyContinue
Check "claude CLI" ($null -ne $cc) $(if ($cc) { $cc.Source } else { "未找到 -> CC 接入 blocked: 需订阅核验" })

Write-Host "`ndoctor 完成。"
