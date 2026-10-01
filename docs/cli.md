# Командная строка

- [Конвейер: python -m ozon_parser](#конвейер-python--m-ozon_parser)
- [Вход на Ozon: get_cookies.py](#вход-на-ozon-get_cookiespy)
- [Старый сценарий: parse_ozon.py и check_snapshot.py](#старый-сценарий-parse_ozonpy-и-check_snapshotpy)
- [Скрипты Windows](#скрипты-windows)
- [Коды возврата](#коды-возврата)

## Конвейер: python -m ozon_parser

```text
python -m ozon_parser [--config PATH] <команда> [флаги]
```

`--config` — путь к `config.yaml` (иначе `PIPELINE_CONFIG` или `config.yaml` в
корне проекта). Любая команда сначала применяет недостающие миграции схемы.

| Команда     | Флаги | Что делает |
| ----------- | ----- | ---------- |
| `migrate`   | — | Применить миграции схемы (делается и автоматически) |
| `discover`  | `--category NAME` (можно повторять), `--rebuild` | Найти SKU и дописать `sku_panel` до `panel_size` каждой категории. `--rebuild` пересобирает panel заново: старая выключается (`is_active = false`) |
| `panel`     | `--export PATH` | Состав panel по категориям и группам; `--export` выгружает активные SKU в CSV |
| `parse`     | `SKU ...`, `--file PATH`, `--category NAME`, `--limit N`, `--kind manual\|daily` | Прогон парсера с записью в PostgreSQL. По умолчанию — активная panel; SKU аргументами или `--file` — вместо неё. `--kind daily` ставит планировщик |
| `benchmark` | `--sample N`, `--seed S`, `--file PATH` | Замер скорости на случайных SKU panel (или из файла) и расчёт дневной ёмкости |
| `runs`      | `--limit N` (10) | Последние прогоны и объём истории |
| `schedule`  | `--once` | Ежедневный прогон: без флага — цикл до `daily_at` (контейнер), `--once` — выполнить прогон сейчас и выйти |

Примеры:

```bash
python -m ozon_parser discover --category coffee_machines
```

```bash
python -m ozon_parser parse --category phone_cases --limit 20
```

```bash
python -m ozon_parser panel --export data/panel.csv
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
| `scripts/register_windows_task.ps1`   | Регистрирует задачу `OzonParserDaily` в Планировщике заданий; `-TaskName` меняет имя |

```bash
powershell -ExecutionPolicy Bypass -File scripts\register_windows_task.ps1
```

## Коды возврата

`0` — успех. `1` — ошибка или неполный результат:

- `discover` — хотя бы одна категория не собрана до `panel_size`;
- `parse` — нет успешных SKU, доля успеха ниже `parser.min_success_rate` или
  сработал предохранитель;
- `benchmark` — ни одного успешного SKU;
- ошибки настроек, подключения к PostgreSQL, параллельный прогон.

`130` — прервано по Ctrl+C.
