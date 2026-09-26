$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$studioCli = 'E:\conda_envs\langchain\Scripts\langgraph.exe'
$taskTemp = 'E:\codex\tmp'

if (-not (Test-Path -LiteralPath $studioCli)) {
    throw 'LangGraph CLI is not installed in E:\conda_envs\langchain. Run the README Studio install command first.'
}

New-Item -ItemType Directory -Force -Path $taskTemp | Out-Null
$env:TEMP = $taskTemp
$env:TMP = $taskTemp
$env:PIP_CACHE_DIR = Join-Path $repoRoot '.pip-cache'
$env:UV_CACHE_DIR = Join-Path $repoRoot '.uv-cache'
$env:PYTHONPYCACHEPREFIX = Join-Path $repoRoot '.pycache'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

Set-Location -LiteralPath $repoRoot
& $studioCli dev --config langgraph.json --host 127.0.0.1 --port 2024 --no-browser --no-reload
