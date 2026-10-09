"""Авторизация на data.ozon.ru и сохранение cookies.

Точка входа: вся логика живёт в price_panel.login. Файл оставлен в корне намеренно -
на него ссылаются README и команды Airflow DAG, и привычка запускать
`python get_cookies.py` ничего не должна стоить.

Запуск:
    python get_cookies.py                  # войти, если сессии нет
    python get_cookies.py --manual         # вход руками, скрипт сохранит cookies
    python get_cookies.py --force          # войти заново, даже если сессия есть
    python get_cookies.py --max-age-days 14 --non-interactive   # так вызывает Airflow
"""

import sys

from price_panel.login import main

if __name__ == "__main__":
    sys.exit(main())
