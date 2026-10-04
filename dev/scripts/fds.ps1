param([ValidateSet('start','stop','status','download')][string]$Command='status', [ValidateSet('auto','cpu','cuda')][string]$Device='auto', [ValidateRange(1,65535)][int]$Port=8767, [string]$Model='jeff-qwen-2b')
$ErrorActionPreference='Stop'
$PSNativeCommandUseErrorActionPreference=$true
$root=([IO.Path]::GetFullPath("$PSScriptRoot/../.."))
$python=Join-Path $root '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'setup.ps1で専用Python環境を導入してください。' }
if ($Command -eq 'start') {
    try { $health=Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 2 } catch { $health=$null }
    if ($health) {
        if ($health.service -ne 'FastDecisionServer') { throw 'このポートは別サービスが使用しています。' }
        Write-Output 'fdsは起動済みです。現在の設定を表示します。'
        $health
        return
    }
    $runtime=Join-Path $root 'runtime'
    New-Item -ItemType Directory -Force -Path $runtime | Out-Null
    $arguments=@('-m','fds.cli','serve','--root',('"'+$root+'"'),'--device',$Device,'--port',"$Port",'--model',$Model)
    $process=Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $root -WindowStyle Hidden -PassThru -RedirectStandardOutput "$runtime/server-$Port.out.log" -RedirectStandardError "$runtime/server-$Port.err.log"
    $process.Id | Set-Content -LiteralPath "$runtime/server-$Port.pid"
    $deadline=[DateTime]::UtcNow.AddSeconds(20)
    do {
        if ($process.HasExited) { throw "fds起動に失敗しました。終了コード=$($process.ExitCode)。runtime/server-$Port.err.logを確認してください。" }
        try { $health=Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 1 } catch { $health=$null }
        if ($health -and $health.pid -eq $process.Id) { $health; return }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "fdsの起動確認が期限を超えました。PID=$($process.Id) を確認してください。自動再起動は行いません。"
} else {
    & $python -m fds.cli $Command --root $root --port $Port --model $Model
    if ($LASTEXITCODE -ne 0) { throw "fds $Command が失敗しました。" }
}