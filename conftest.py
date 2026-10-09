"""Общая настройка pytest: корень проекта в sys.path и отдельный каталог логов.

Тесты лежат в tests/ и импортируют пакет price_panel из корня. pytest
добавляет в sys.path каталог этого файла, поэтому пакет находится и без
установки - `pip install -r requirements.txt` достаточно, `pip install -e .`
не обязателен.

Здесь же общие фикстуры: тестовая база PostgreSQL (wh, select) и сверка с
эталонами tests/golden/*.json (golden).
"""

import datetime as dt
import decimal
import json
import os
import sys
import tempfile
import urllib.parse
from pathlib import Path

import pytest

ROOT = str(Path(__file__).resolve().parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Логи тестов - во временный каталог, а не в боевой logs/: иначе фикстуры
# вроде "SKU 0000000000" перемешиваются с записями настоящих прогонов.
# Задаётся до импорта пакета: логгеры создаются при импорте модулей.
os.environ.setdefault("PRICE_PANEL_LOG_DIR", tempfile.mkdtemp(prefix="price-panel-test-logs-"))

# Оповещения в тестах выключены, даже если они настроены в .env: пустые
# значения в окружении сильнее файла (load_dotenv не трогает заданные
# переменные), и ни один тест не напишет в настоящий Telegram.
for _name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "HEALTHCHECK_URL"):
    os.environ[_name] = ""

# Предел частоты открытия страниц (PAGE_INTERVAL) в тестах выключен: иначе
# каждая проверка с браузером ждала бы по 6,5 с. Сам предел проверяется
# отдельно (tests/test_parse_run.py).
os.environ["PAGE_INTERVAL"] = "0"

TEST_DSN = os.getenv("TEST_PG_DSN", "")
GOLDEN_DIR = Path(ROOT) / "tests" / "golden"


@pytest.fixture
def wh():
    """Пустая схема на тестовой базе: TEST_PG_DSN, в имени базы - "test".

    Фикстура каждый раз пересоздаёт схему public, поэтому на базу без "test"
    в имени она не пойдёт.
    """
    if not TEST_DSN:
        pytest.skip("TEST_PG_DSN не задан")
    if "test" not in urllib.parse.urlsplit(TEST_DSN).path:
        pytest.skip("TEST_PG_DSN должен указывать на тестовую базу (имя содержит 'test')")
    from price_panel.warehouse import Warehouse

    store = Warehouse(TEST_DSN)
    store.run(lambda cursor: cursor.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    assert store.migrate()
    yield store
    store.close()


@pytest.fixture
def select(wh):
    """select(sql) - строки запроса к тестовой базе словарями {колонка: значение}."""
    def run(sql: str) -> list:
        def query(cursor) -> list:
            cursor.execute(sql)
            names = [column[0] for column in cursor.description]
            return [dict(zip(names, row)) for row in cursor.fetchall()]
        return wh.run(query)
    return run


def _golden_value(value):
    # Время - в UTC: иначе эталон зависел бы от часового пояса сессии базы.
    if isinstance(value, dt.datetime):
        return value.astimezone(dt.timezone.utc).isoformat()
    if isinstance(value, (dt.date, decimal.Decimal)):
        return str(value)
    raise TypeError("{!r} не сериализуется в эталон".format(value))


@pytest.fixture
def golden():
    """golden(name, data) - сверка data с эталоном tests/golden/<name>.json.

    Эталон фиксирует текущее поведение, чтобы перестройка кода его не меняла.
    Поведение изменилось намеренно - перезапишите эталон и проверьте дифф:
    UPDATE_GOLDEN=1 pytest <тест>.
    """
    def check(name: str, data) -> None:
        path = GOLDEN_DIR / "{}.json".format(name)
        text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True,
                          default=_golden_value) + "\n"
        if os.getenv("UPDATE_GOLDEN") == "1":
            path.parent.mkdir(exist_ok=True)
            path.write_text(text, encoding="utf-8")
        assert path.exists(), "Нет эталона {} - создайте: UPDATE_GOLDEN=1 pytest".format(path)
        assert json.loads(text) == json.loads(path.read_text(encoding="utf-8"))
    return check
