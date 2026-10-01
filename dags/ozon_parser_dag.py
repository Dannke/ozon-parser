"""Airflow DAG: ежедневный парсинг карточек Ozon.

    ensure_session  ->  parse_products  ->  check_result

Почему именно так:
  * ensure_session - get_cookies.py с лимитом возраста сессии. Если сессия
    действительна и моложе ozon_cookies_ttl_days, задача завершается сразу.
    Если файла нет, он повреждён, в нём нет токенов или он старше лимита,
    выполняется автоматический вход: почта -> код из Gmail -> новый
    cookies.json. Вход идёт без человека (--non-interactive): при
    невозможности - понятная ошибка, а не ожидание окна, которое некому закрыть;
  * все шаги запускаются отдельными процессами в собственном виртуальном
    окружении проекта: у воркера Airflow обычно нет ни Playwright, ни
    браузера, ни драйверов БД. DAG не импортирует пакет проекта и не тянет
    его зависимости;
  * после парсинга сверяем, что данные за эту дату действительно легли в
    хранилище - молчаливый пустой результат хуже явной ошибки.

Переменные Airflow подставляются шаблонами Jinja в момент выполнения, а не
разбора файла: планировщик разбирает DAG каждые 30 секунд, и Variable.get()
на верхнем уровне дёргал бы метабазу при каждом разборе.

Установка:
    1. Скопировать каталог проекта на сервер (например, в /opt/ozon_parser),
       создать там окружение и поставить зависимости (см. docs/installation.md).
    2. Положить на сервер credentials.json и token.json. token.json создаётся
       при первом запуске get_cookies.py на машине с браузером (окно согласия
       Google) - на сервере его открыть некому.
    3. Положить этот файл в AIRFLOW_HOME/dags/.
    4. Задать переменные Airflow из блока VARIABLES ниже.

Windows: Airflow нативно под Windows не работает - нужен Linux, WSL2 или
Docker. Сам парсер при этом кроссплатформенный.
"""

# Airflow и pendulum есть в окружении Airflow, а не в .venv проекта - так
# задумано (см. выше), поэтому их импорты проверка типов не разрешает.
# Запись "a >> b" задаёт порядок задач: значение выражения не нужно.
# pyright: reportMissingImports=false, reportUnusedExpression=false

from __future__ import annotations

import datetime as dt

import pendulum
from airflow import DAG

try:  # Airflow 3: операторы переехали в провайдер standard
    from airflow.providers.standard.operators.bash import BashOperator
except ImportError:  # Airflow 2.x
    from airflow.operators.bash import BashOperator

# --- VARIABLES: задаются в Airflow (Admin -> Variables) ----------------------
# ozon_project_dir       каталог проекта на сервере       (/opt/ozon_parser)
# ozon_python            интерпретатор окружения проекта  (.venv/bin/python)
# ozon_storage           csv | postgres | clickhouse      (postgres)
# ozon_cookies_ttl_days  через сколько дней входить заново (14)
# ozon_min_rows          сколько строк минимум ждём за день (1)
# ozon_min_success_rate  доля успешных SKU, ниже которой прогон неудачен (0.8)

# Команды выполняются уже внутри каталога проекта, поэтому путь к интерпретатору
# по умолчанию относительный - так не нужно склеивать его с каталогом в шаблоне.
PROJECT_PREFIX = """
cd {{ var.value.get('ozon_project_dir', '/opt/ozon_parser') }} && \
{{ var.value.get('ozon_python', '.venv/bin/python') }}"""

SESSION_COMMAND = PROJECT_PREFIX + """ get_cookies.py \
--non-interactive \
--max-age-days {{ var.value.get('ozon_cookies_ttl_days', '14') }}
"""

PARSE_COMMAND = PROJECT_PREFIX + """ parse_ozon.py \
--file skus.txt \
--storage {{ var.value.get('ozon_storage', 'postgres') }} \
--min-success-rate {{ var.value.get('ozon_min_success_rate', '0.8') }} \
--date {{ data_interval_end | ds }}
"""

CHECK_COMMAND = PROJECT_PREFIX + """ check_snapshot.py \
--storage {{ var.value.get('ozon_storage', 'postgres') }} \
--min-rows {{ var.value.get('ozon_min_rows', '1') }} \
--date {{ data_interval_end | ds }}
"""

# На сервере окну браузера взяться неоткуда.
HEADLESS_ENV = {"HEADLESS": "1"}

DEFAULT_ARGS = {
    "owner": "data-team",
    "retries": 2,
    "retry_delay": dt.timedelta(minutes=10),
    # ~10-15 с на товар: 90 минут хватает примерно на 400 SKU. Собранное
    # сохраняется батчами, так что таймаут не обнуляет прогон. Списки в
    # тысячи SKU стоит делить на части через --offset/--limit.
    "execution_timeout": dt.timedelta(minutes=90),
}


with DAG(
    dag_id="ozon_products_daily",
    description="Ежедневный парсинг карточек товаров Ozon",
    default_args=DEFAULT_ARGS,
    # 05:30 по Москве: ночные пересчёты цен уже прошли, до дневной нагрузки далеко.
    schedule="30 5 * * *",
    start_date=pendulum.datetime(2026, 9, 1, tz="Europe/Moscow"),
    catchup=False,        # цены задним числом всё равно не собрать
    max_active_runs=1,    # параллельные запуски подняли бы два браузера разом
    dagrun_timeout=dt.timedelta(hours=3),
    tags=["ozon", "parser", "datalens"],
) as dag:

    # Вход занимает 1-3 минуты; каждый повтор запрашивает новый код на почту.
    session_task = BashOperator(
        task_id="ensure_session",
        bash_command=SESSION_COMMAND,
        env=HEADLESS_ENV,
        append_env=True,
        retries=1,
        execution_timeout=dt.timedelta(minutes=15),
    )

    # --date фиксирует дату среза: перезапуск задачи за прошлые сутки запишет
    # данные тем же числом, а не сегодняшним.
    #
    # Берём data_interval_end, а не start. Мы собираем СНИМОК на момент
    # запуска, а не агрегат за интервал: запуск 23-го в 05:30 закрывает
    # интервал [22-е 05:30, 23-е 05:30), и по start цены, снятые 23-го,
    # легли бы в срез за 22-е - вся временная ось витрины съехала бы на сутки.
    parse_task = BashOperator(
        task_id="parse_products",
        bash_command=PARSE_COMMAND,
        env=HEADLESS_ENV,
        append_env=True,
    )

    result_task = BashOperator(
        task_id="check_result",
        bash_command=CHECK_COMMAND,
    )

    session_task >> parse_task >> result_task
