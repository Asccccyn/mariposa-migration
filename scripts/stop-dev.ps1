# mariposa stop-dev: 只停本项目监听 18780 的进程
# 不按进程名模糊匹配，不会杀掉其他 Python/Node/cloudflared
$conn = Get-NetTCPConnection -LocalPort 18780 -State Listen -ErrorAction SilentlyContinue
if (-not $conn) { Write-Host "18780 无监听，无需停止。"; exit 0 }
$pids = $conn | Select-Object -ExpandProperty OwningProcess -Unique
foreach ($p in $pids) {
    # 校验该进程命令行属于本项目，防止误杀占用同端口的他人服务
    $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$p").CommandLine
    if ($cmd -and $cmd -match "mariposa") {
        Stop-Process -Id $p -Force
        Write-Host "stopped PID $p ($cmd)"
    } else {
        Write-Host "PID $p 不是 mariposa（$cmd），跳过。"
    }
}
