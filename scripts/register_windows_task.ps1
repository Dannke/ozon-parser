# Регистрирует ежедневную задачу Планировщика заданий Windows для run_daily.ps1.
#
#   powershell -ExecutionPolicy Bypass -File scripts\register_windows_task.ps1
#
# Время берётся из config.yaml (schedule.daily_at в schedule.timezone, по
# умолчанию по Москве) и переводится в местное время Windows: Планировщик
# заданий работает в местном времени. После смены daily_at или часового пояса
# Windows запустите скрипт ещё раз - задача перезапишется.
#
# Задача:
#   * выполняется от текущего пользователя и только когда он вошёл в систему -
#     пароль Windows не нужен и не сохраняется;
#   * если в назначенное время компьютер был выключен, запускается при
#     первой возможности (StartWhenAvailable);
#   * не запускается второй раз, пока идёт предыдущий прогон; жёсткий
#     предел - schedule.parse_timeout_hours + 1 час. Прогон вместе с повтором
#     после блокировки сам укладывается в parse_timeout_hours, лимит Windows -
#     страховка на случай зависания.
#
# Удалить задачу:  Unregister-ScheduledTask -TaskName OzonParserDaily

param(
    [string]$TaskName = "OzonParserDaily"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$python = Join-Path $root ".venv\Scripts\python.exe"

$code = @"
import datetime as dt
from ozon_parser.settings import load_settings
from ozon_parser.scheduler import get_timezone, next_run_at
s = load_settings()
target = next_run_at(dt.datetime.now(dt.timezone.utc), s.schedule.daily_at,
                     get_timezone(s.schedule.timezone))
print(target.astimezone().strftime('%H:%M'), s.schedule.daily_at.strftime('%H:%M'),
      s.schedule.timezone, int(s.schedule.parse_timeout_hours * 60) + 60)
"@
$parts = (& $python -c $code).Trim().Split(" ")
$localTime, $configTime, $configZone = $parts[0], $parts[1], $parts[2]
$limitMinutes = [int]$parts[3]

$script = Join-Path $root "scripts\run_daily.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`"" `
    -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At $localTime
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes $limitMinutes) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force `
    -Description "Ozon price panel: daily parse ($configTime $configZone)" | Out-Null

$info = Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo
"Task '$TaskName': daily at $localTime local time = $configTime $configZone, limit $limitMinutes min"
"Next run: $($info.NextRunTime)"
