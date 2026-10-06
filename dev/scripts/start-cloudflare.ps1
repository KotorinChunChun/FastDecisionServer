param(
    [switch]$Restart,
    [string]$Model='jeff-qwen-2b',
    [ValidateSet('auto','cpu','cuda')][string]$Device='auto',
    [ValidateRange(1,65535)][int]$Port=8767
)
$ErrorActionPreference='Stop'
$PSNativeCommandUseErrorActionPreference=$true
$root=[IO.Path]::GetFullPath("$PSScriptRoot/../..")
$runner=Join-Path $PSScriptRoot 'fds.ps1'
if (-not (Test-Path -LiteralPath (Join-Path $root '.venv/Scripts/python.exe'))) {
    throw 'setup.ps1で専用環境を導入してください。'
}
try { $health=Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 2 } catch { $health=$null }
if ($health) {
    if ($health.service -ne 'FastDecisionServer') { throw 'このポートは別サービスが使用しています。' }
    if (-not $Restart) { throw '起動済みです。認証設定を反映するには -Restart を付けて正常再起動してください。' }
    if (-not $PSBoundParameters.ContainsKey('Model')) { $Model=$health.default_model }
    if (-not $PSBoundParameters.ContainsKey('Device')) { $Device=$health.default_device }
}
$account=(Read-Host 'Cloudflare アカウントID').Trim()
if ($account -notmatch '^[a-fA-F0-9]{32}$') { throw 'アカウントIDは32桁の16進数です。' }
$secure=Read-Host 'Workers AI APIトークン' -AsSecureString
$previousAccount=$env:CLOUDFLARE_ACCOUNT_ID
$previousToken=$env:CLOUDFLARE_AUTH_TOKEN
try {
    $env:CLOUDFLARE_ACCOUNT_ID=$account
    $env:CLOUDFLARE_AUTH_TOKEN=([Net.NetworkCredential]::new('', $secure)).Password.Trim()
    if (-not $env:CLOUDFLARE_AUTH_TOKEN -or $env:CLOUDFLARE_AUTH_TOKEN.Length -gt 4096 -or $env:CLOUDFLARE_AUTH_TOKEN -match '[^\x21-\x7e]') {
        throw 'APIトークンの形式が不正です。'
    }
    if ($health) {
        & $runner -Command stop -Port $Port
        $deadline=[DateTime]::UtcNow.AddSeconds(60)
        do {
            Start-Sleep -Milliseconds 250
            try { $running=Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 1 } catch { $running=$null }
            if (-not $running) { break }
            if ([DateTime]::UtcNow -ge $deadline) { throw '正常終了の待機が60秒を超えました。強制終了せず終了を待ち、再実行してください。' }
        } while ($true)
    }
    & $runner -Command start -Model $Model -Device $Device -Port $Port
    Write-Output '認証情報を起動プロセスへ渡しました。実接続は判定要求で確認してください。'
} finally {
    $env:CLOUDFLARE_ACCOUNT_ID=$previousAccount
    $env:CLOUDFLARE_AUTH_TOKEN=$previousToken
    $secure.Dispose()
}
