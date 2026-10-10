"""Проверка, что срез за указанную дату попал в хранилище.

Точка входа: вся логика живёт в price_panel.legacy.check. Файл оставлен в корне намеренно -
на него ссылаются README и команды Airflow DAG, и привычка запускать
`python check_snapshot.py` ничего не должна стоить.

Запуск:
    python check_snapshot.py                     # за сегодня, бэкенд из .env
    python check_snapshot.py --date 2026-09-23
    python check_snapshot.py --storage postgres --min-rows 2

Код возврата: 0 - данные на месте, 1 - данных нет либо проверка не удалась.
"""

import sys

from price_panel.legacy.check import main

if __name__ == "__main__":
    sys.exit(main())
