# Ежедневный прогон конвейера на Windows (для Планировщика заданий):
#   ensure_session -> parse по активной panel  (python -m ozon_parser schedule --once)
#
# Почему не в Docker: 30.09.2026 Chromium в контейнере получал от Ozon HTTP 403
# на каждую карточку, а Google Chrome на этой машине с того же IP разбирает
# те же SKU через API. База при этом остаётся в docker-compose (PG_DSN в .env).
#
# Браузер берётся из .env (BROWSER_CHANNEL=chrome), окна не показывает.
# Логи - как обычно, в logs\scheduler.log, logs\pipeline.log, logs\parse_ozon.log.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$env:HEADLESS = "1"
$env:PYTHONUTF8 = "1"

& (Join-Path $root ".venv\Scripts\python.exe") -m ozon_parser schedule --once
exit $LASTEXITCODE
