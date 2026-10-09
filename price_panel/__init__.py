"""Парсер карточек товаров ozon.ru.

Модули собраны в пакет, а не лежат в корне: общие имена вроде ``config``,
``storage`` и ``logger`` иначе конфликтуют с чужими модулями - например, под
Airflow, где каталог проекта и dags/ оказываются на sys.path одновременно.

Точки входа лежат в корне проекта: ``get_cookies.py``, ``parse_ozon.py``,
``check_snapshot.py``. Конвейер discovery -> panel -> parse с хранением в
PostgreSQL запускается как ``python -m price_panel <команда>``.
"""

__version__ = "1.2.0"
