"""Общая настройка pytest: корень проекта в sys.path и отдельный каталог логов.

Тесты лежат в tests/ и импортируют пакет ozon_parser из корня. pytest
добавляет в sys.path каталог этого файла, поэтому пакет находится и без
установки - `pip install -r requirements.txt` достаточно, `pip install -e .`
не обязателен.
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Логи тестов - во временный каталог, а не в боевой logs/: иначе фикстуры
# вроде "SKU 0000000000" перемешиваются с записями настоящих прогонов.
# Задаётся до импорта пакета: логгеры создаются при импорте модулей.
os.environ.setdefault("OZON_LOG_DIR", tempfile.mkdtemp(prefix="ozon-parser-test-logs-"))
