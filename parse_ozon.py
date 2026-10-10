"""Парсинг карточек товаров ozon.ru по списку SKU.

Точка входа: вся логика живёт в price_panel.marketplaces.ozon.parse. Файл
оставлен в корне намеренно - на него ссылаются README и команды Airflow DAG,
и привычка запускать
`python parse_ozon.py` ничего не должна стоить.

Запуск:
    python parse_ozon.py                               # SKU из config.DEFAULT_SKUS
    python parse_ozon.py 2359066702 2829800382         # SKU аргументами
    python parse_ozon.py --file skus.txt --storage csv # SKU из файла
    python parse_ozon.py --file skus.txt --offset 200 --limit 200
"""

import sys

from price_panel.marketplaces.ozon.parse import main

if __name__ == "__main__":
    sys.exit(main())
