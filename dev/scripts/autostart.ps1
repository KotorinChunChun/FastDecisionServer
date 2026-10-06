param(
    [ValidateSet('enable','disable','status')][string]$Command = 'status'
)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath("$PSScriptRoot/../..")
$key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$name = 'FastDecisionServer'
if ($Command -eq 'enable') {
    $pwshPath = (Get-Command pwsh -ErrorAction Stop).Source
    $launcher = Join-Path $root 'dev/scripts/fds.ps1'
    $python = Join-Path $root '.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $launcher) -or -not (Test-Path -LiteralPath $python)) {
        throw 'fdsの起動スクリプトまたは専用Python環境がありません。'
    }
    $value = '"{0}" -NoProfile -WindowStyle Hidden -File "{1}" -Command start -Device auto' -f $pwshPath, $launcher
    New-Item -Path $key -Force | Out-Null
    New-ItemProperty -LiteralPath $key -Name $name -PropertyType String -Value $value -Force | Out-Null
} elseif ($Command -eq 'disable') {
    if (Get-ItemProperty -LiteralPath $key -Name $name -ErrorAction SilentlyContinue) {
        Remove-ItemProperty -LiteralPath $key -Name $name
    }
}
$value = (Get-ItemProperty -LiteralPath $key -Name $name -ErrorAction SilentlyContinue).$name
[pscustomobject]@{ Enabled = [bool]$value; Command = $value }
