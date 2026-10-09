# Командная строка

- [Конвейер: python -m price_panel](#конвейер-python--m-price_panel)
- [Вход на Ozon: get_cookies.py](#вход-на-ozon-get_cookiespy)
- [Старый сценарий: parse_ozon.py и check_snapshot.py](#старый-сценарий-parse_ozonpy-и-check_snapshotpy)
- [Скрипты Windows](#скрипты-windows)
- [Коды возврата](#коды-возврата)

## Конвейер: python -m price_panel

```text
python -m price_panel [--config PATH] <команда> [флаги]
```

`--config` — путь к `config.yaml` (иначе `PIPELINE_CONFIG` или `config.yaml` в
корне проекта). Любая команда, кроме `backup` и `notify`, сначала применяет
недостающие миграции схемы.

| Команда     | Флаги | Что делает |
| ----------- | ----- | ---------- |
| `migrate`   | — | Применить миграции схемы (делается и автоматически) |
| `discover`  | `--category NAME` (можно повторять), `--rebuild` | Найти SKU и дописать `sku_panel` до `panel_size` каждой категории. `--rebuild` пересобирает panel заново: старая выключается (`is_active = false`) |
| `panel`     | `--export PATH` | Состав panel по категориям и группам; `--export` выгружает активные SKU в CSV |
| `parse`     | `SKU ...`, `--file PATH`, `--category NAME`, `--limit N`, `--kind manual\|daily`, `--missing-today` | Прогон парсера с записью в PostgreSQL. По умолчанию — активная panel; SKU аргументами или `--file` — вместо неё. `--kind daily` ставит планировщик. `--missing-today` — только SKU panel, у которых за сегодня (по `schedule.timezone`) ещё нет успешного наблюдения: догон после блокировки; если таких нет — код 0 без прогона |
| `benchmark` | `--sample N`, `--seed S`, `--file PATH` | Замер скорости на случайных SKU panel (или из файла) и расчёт дневной ёмкости |
| `runs`      | `--limit N` (10) | Последние прогоны и объём истории |
| `backup`    | — | Резервная копия базы: `pg_dump` внутри контейнера `postgres` в `backup.dir` (`backups/ozon-ГГГГ-ММ-ДД.dump`), проверка архива через `pg_restore --list`, хранится `backup.keep` последних копий. Нужен запущенный Docker Desktop |
| `schedule`  | `--once` | Ежедневный прогон: без флага — цикл до `daily_at` (контейнер), `--once` — выполнить прогон сейчас и выйти. Оповещения о начале и о повторе после блокировки, после прогона и повторов — `backup` (если `backup.enabled`) и оповещение об итоге. С `--once` оповещает и о том, что прогон не начался (база недоступна, ошибка настроек) |
| `notify`    | — | Проверка Telegram-бота: пока `TELEGRAM_CHAT_ID` пуст — список чатов, которые писали боту (откуда взять id); иначе — проверочное сообщение, а перед ним — задержанные из очереди (если Telegram был недоступен). Базу и Ozon не трогает. Код 1 — не настроено или не отправлено (причина в `logs/notify.log`) |

Примеры:

```bash
python -m price_panel discover --category coffee_machines
```

```bash
python -m price_panel parse --category phone_cases --limit 20
```

```bash
python -m price_panel parse --kind daily --missing-today
```

```bash
python -m price_panel panel --export data/panel.csv
```

Пример вывода `discover` (живой запуск 29.09.2026, пилотная panel по 20 SKU):

```text
Discovery started

Category: phone_cases (Чехлы для телефонов)
  source:     ozon_listing
  requests:   7
  discovered: 56 уникальных SKU (0 уже в panel)
  selected:   20 (top 8 + tail_random 12)

Category: smartphones (Смартфоны)
  source:     ozon_listing
  requests:   8
  discovered: 55 уникальных SKU (0 уже в panel)
  selected:   20 (top 8 + tail_random 12)
...
Total selected SKU: 80
Panel saved successfully
```

Отметки категорий в выводе `discover`:

- `skipped` — panel категории уже собрана, запросов не было;
- `STOPPED: Ozon ограничил запросы, найденное сохранено` — категория собрана
  частично; `STOPPED: не запускалась …` — следующие категории после такой
  остановки;
- `FAILED` — категорию обойти не удалось (причина в выводе и в
  `discovery_runs`).

Недособранное доберёт повторный `discover` (готовые категории он пропускает).

## Вход на Ozon: get_cookies.py

```bash
python get_cookies.py [--method email|phone] [--manual] [--force] [--max-age-days N] [--non-interactive]
```

| Флаг                | Что делает |
| ------------------- | ---------- |
| `--method`          | Способ входа (по умолчанию `LOGIN_METHOD` из `.env`) |
| `--manual`          | Открыть браузер и дождаться, пока человек войдёт сам |
| `--force`           | Войти заново, даже если сессия есть |
| `--max-age-days N`  | Считать сессию устаревшей, если файл старше N дней |
| `--non-interactive` | Запретить шаги, которым нужен человек (ручной вход, окно согласия Google): при необходимости — понятная ошибка, а не ожидание |

Как устроен вход — в [installation.md](installation.md#сессия-ozon).

## Старый сценарий: parse_ozon.py и check_snapshot.py

```bash
python parse_ozon.py [SKU ...] [--file PATH] [--storage csv|postgres|clickhouse] [--date YYYY-MM-DD] [--output PATH] [--offset N] [--limit N] [--batch-size N] [--min-success-rate X]
```

```bash
python check_snapshot.py [--storage csv|postgres|clickhouse] [--date YYYY-MM-DD] [--min-rows N]
```

Описание и примеры — в [legacy.md](legacy.md).

## Скрипты Windows

| Скрипт                                | Что делает |
| ------------------------------------- | ---------- |
| `scripts/run_daily.ps1`               | Ежедневный прогон (`schedule --once`) в Chrome без окна |
| `scripts/register_windows_task.ps1`   | Регистрирует задачу `PricePanelDaily` в Планировщике заданий; `-TaskName` меняет имя |

```bash
powershell -ExecutionPolicy Bypass -File scripts\register_windows_task.ps1
```

## Коды возврата

`0` — успех. `1` — ошибка или неполный результат:

- `discover` — хотя бы одна категория не собрана до `panel_size`;
- `parse` — нет успешных SKU или доля успеха ниже `parser.min_success_rate`;
- `benchmark` — ни одного успешного SKU;
- `backup` — копия не создана (нет Docker, `pg_dump` не сработал, архив не
  прошёл проверку); прежние копии при этом не трогаются;
- ошибки настроек, подключения к PostgreSQL, параллельный прогон.

`2` — `parse --missing-today` вместе со списком SKU или `--file`.

`3` — `parse`: сработал предохранитель, прогон в статусе `blocked`. По этому
коду ежедневная задача повторяет прогон через `schedule.block_retry_delay_hours`.

`130` — прервано по Ctrl+C.
