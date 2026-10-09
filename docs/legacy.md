# Старый сценарий: список SKU → CSV / БД

Исходный режим проекта, с которого начинался конвейер. Он работает как раньше
и не требует ни `config.yaml`, ни таблиц конвейера. Для ежедневного
наблюдения за panel используйте `python -m price_panel parse`
(см. [operations.md](operations.md)).

- [Парсинг по списку](#парсинг-по-списку)
- [Хранилища](#хранилища)
- [Контроль среза](#контроль-среза)
- [Airflow](#airflow)

## Парсинг по списку

```bash
python parse_ozon.py 2359066702 2829800382
```

Без аргументов берётся список `DEFAULT_SKUS` из `price_panel/config.py`.
Список можно читать из файла:

```bash
python parse_ozon.py --file skus.txt --storage postgres
```

Длинный список можно разложить на части, по одной на задачу планировщика:

```bash
python parse_ozon.py --file skus.txt --offset 200 --limit 200
```

Файл со списком может быть и CSV со столбцом `sku` — например, выгрузка panel
после `discover`:

```bash
python parse_ozon.py --file data/panel_skus.csv
```

Остальные флаги: `--output` (путь к CSV), `--date` (дата среза),
`--batch-size`, `--min-success-rate` — см. [cli.md](cli.md#старый-сценарий-parse_ozonpy-и-check_snapshotpy).

## Хранилища

Результат пишется в `data/products.csv`. CSV пишется всегда, даже при выгрузке
в БД. На длинном списке результат сбрасывается в хранилище батчами
(`BATCH_SIZE`, по умолчанию 50).

Бэкенд выбирается `STORAGE` в `.env` или `--storage`:

- `csv` — только `data/products.csv`: ровно 12 полей задания
  (см. [data-model.md](data-model.md#поля-карточки));
- `postgres` — таблица `PG_TABLE` (`ozon_products`);
- `clickhouse` — таблица `CH_TABLE`, движок `ReplacingMergeTree`.

В таблицах БД есть ещё две служебные колонки:

- `parsed_date` — дата среза;
- `source` — `api` или `html`, то есть откуда взяты данные.

Ключ таблицы — `(sku, parsed_date)`: повторный запуск за тот же день не плодит
дубли (PostgreSQL обновляет строку через `ON CONFLICT`, ClickHouse схлопывает
движком). Витрины для DataLens поверх этой таблицы — в
`sql/datalens_views.sql`.

## Контроль среза

```bash
python check_snapshot.py --storage postgres --min-rows 1 --date 2026-09-23
```

Проверяет, что за дату в хранилище появилось не меньше `--min-rows` строк, и
возвращает 1, если нет. Пустой прогон парсера не считается успешным: он
возвращает 1, а CSV перезаписывается одним заголовком, чтобы не выдать снимок
прошлого запуска за сегодняшний.

## Airflow

DAG лежит в [`dags/ozon_parser_dag.py`](../dags/ozon_parser_dag.py) и состоит
из трёх задач:

```text
ensure_session  ->  parse_products  ->  check_result
```

- **ensure_session** — `get_cookies.py --non-interactive --max-age-days N`.
  Если сессия действительна и моложе `ozon_cookies_ttl_days`, задача сразу
  завершается. Если файла нет, он повреждён, в нём нет токенов или он старше
  лимита, выполняется автоматический вход по почте с кодом из Gmail, и
  `cookies.json` перевыпускается. Человек при этом не нужен.
  `--non-interactive` запрещает шаги, которые на сервере некому пройти (ручной
  вход и окно согласия Google): если они понадобились, задача падает с
  понятной причиной, а не зависает.
- **parse_products** запускает парсер в фоновом режиме (`HEADLESS=1`).
- **check_result** вызывает `check_snapshot.py` и падает, если за эту дату в
  хранилище не появилось строк.

Все шаги запускаются отдельными процессами в окружении проекта. У воркера
Airflow обычно нет ни Playwright, ни драйверов БД, поэтому DAG не импортирует
пакет проекта и не тянет его зависимости.

Перед первым запуском на сервер нужно положить `credentials.json` и
`token.json`. `token.json` создаётся при первом запуске `get_cookies.py` на
машине с браузером: там откроется окно согласия Google, а на сервере его
открыть некому. Дальше токен обновляется сам (кроме OAuth-приложения в
статусе Testing, см. [installation.md](installation.md#доступ-к-gmail)).

Переменные Airflow (Admin → Variables):

| Переменная              | По умолчанию        | Назначение                                |
| ----------------------- | ------------------- | ----------------------------------------- |
| `ozon_project_dir`      | `/opt/price_panel`  | каталог проекта на сервере                |
| `ozon_python`           | `.venv/bin/python`  | интерпретатор окружения проекта           |
| `ozon_storage`          | `postgres`          | `csv` / `postgres` / `clickhouse`         |
| `ozon_cookies_ttl_days` | `14`                | через сколько дней входить заново         |
| `ozon_min_rows`         | `1`                 | сколько строк минимум ждём за день        |
| `ozon_min_success_rate` | `0.8`               | доля успешных SKU, ниже которой — провал  |

Расписание — `30 5 * * *` по Москве, `catchup=False`, `max_active_runs=1`.
Дата среза передаётся парсеру через `--date {{ data_interval_end | ds }}`:
это день запуска, а не начало интервала, поэтому перезапуск задачи за прошлые
сутки запишет данные тем же числом.

DAG совместим с Airflow 2.x и 3.x: операторы импортируются из провайдера
`standard`, а при его отсутствии — по старым путям. Под Windows Airflow не
работает, нужен Linux, WSL2 или Docker.

Чтобы DAG обслуживал panel конвейера, замените команду задачи
`parse_products` на `python -m price_panel parse --kind daily`.
