# mariposa start-dev: 种子 + 启动开发服务（127.0.0.1:18780）
# 不启动生产 provider 副作用；不触碰其他项目进程
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = "$root\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "venv 缺失：先 python -m venv .venv 并 pip install -r requirements.lock" }

$env:PYTHONPATH = "$root\backend"
# 语义检索：本地 ONNX bge-small-zh（模型已固化 runtime/models；置空回 degraded）
$env:MARIPOSA_SEMANTIC_PROVIDER = "local_bge_zh"
$env:FASTEMBED_CACHE_PATH = "$root\runtime\models"
& $py -m mariposa.devseed | Out-Host

$existing = Get-NetTCPConnection -LocalPort 18780 -State Listen -ErrorAction SilentlyContinue
if ($existing) { Write-Host "18780 已有监听（PID $($existing[0].OwningProcess)），不再重复启动。"; exit 0 }

$log = "$root\runtime\logs\dev-server.log"
$proc = Start-Process -FilePath $py `
    -ArgumentList "-m", "uvicorn", "mariposa.app:app", "--host", "127.0.0.1", "--port", "18780" `
    -WorkingDirectory $root -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $log -RedirectStandardError "$root\runtime\logs\dev-server.err.log"
Start-Sleep -Seconds 3
$ok = $false
try { $r = Invoke-RestMethod "http://127.0.0.1:18780/health" -TimeoutSec 5; $ok = ($r.ok -eq $true) } catch {}
if ($ok) {
    Write-Host "mariposa dev 已启动：http://127.0.0.1:18780  (PID $($proc.Id))"
    Write-Host "token 见 runtime/dev_tokens.json；日志 runtime/logs/dev-server.log"
} else {
    Write-Host "启动后 /health 未就绪，查看 $log"
}
