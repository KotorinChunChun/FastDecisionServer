param([ValidateSet('cpu','cuda')][string]$Device='cpu')
$ErrorActionPreference='Stop'
$PSNativeCommandUseErrorActionPreference=$true
Set-Location ([IO.Path]::GetFullPath("$PSScriptRoot/../.."))
if (-not (Test-Path .venv/Scripts/python.exe)) { uv venv --python 3.12 .venv }
git submodule update --init --recursive
uv pip install --python .venv/Scripts/python.exe -e . -e vendor/jeff
if ($Device -eq 'cuda') {
    uv pip install --python .venv/Scripts/python.exe --no-deps torch==2.14.0+cu130 torchvision==0.29.0+cu130 --index-url https://download.pytorch.org/whl/cu130
} else {
    uv pip install --python .venv/Scripts/python.exe --no-deps torch==2.14.0 torchvision==0.29.0 --index-url https://download.pytorch.org/whl/cpu
}
uv pip check --python .venv/Scripts/python.exe
& .venv/Scripts/python.exe -c "import fds,torch; print('FastDecisionServer / fds',fds.__version__,'PyTorch',torch.__version__,'CUDA',torch.cuda.is_available())"