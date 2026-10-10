"""Структурная проверка Airflow DAG без установленного Airflow.

Airflow под Windows не ставится, поэтому подменяем его модули заглушками,
импортируем файл DAG и смотрим, что получилось: состав задач, их связи,
параметры расписания. Отдельно отрисовываем bash-команды настоящим Jinja2 -
именно там легче всего ошибиться.

Две вещи, важные для запуска в общем прогоне:
  * jinja2 импортируется внутри render(), а не на уровне модуля, чтобы её
    отсутствие не рушило сбор остальных тестов;
  * заглушки ставятся в sys.modules, поэтому после модуля они снимаются:
    фальшивый airflow не должен пережить этот файл и достаться соседям.

Что это НЕ проверяет: семантику Airflow (совместимость версий, работу
операторов, планировщик). Только то, что файл исполняется и собирает
ожидаемую структуру. Решение «входить ли заново» принимает get_cookies.py -
оно проверяется в tests/test_session.py.

    pytest tests/test_dag.py
"""

from __future__ import annotations

import datetime as dt
import sys
import types
from pathlib import Path

import pytest

DAG_FILE = Path(__file__).resolve().parents[1] / "dags" / "ozon_parser_dag.py"

# Модули, которые подменяются заглушками - чтобы потом убрать за собой.
STUBBED_MODULES = ("airflow", "airflow.operators", "airflow.operators.bash", "pendulum")

# Запуск 23-го в 05:30 закрывает интервал [22-е 05:30, 23-е 05:30).
# Срез должен датироваться днём ЗАПУСКА, то есть концом интервала: цены,
# снятые 23-го, - это данные за 23-е, а не за 22-е.
INTERVAL_START = dt.datetime(2026, 9, 22, 5, 30)
INTERVAL_END = dt.datetime(2026, 9, 23, 5, 30)
RUN_DAY = "2026-09-23"

EXPECTED_TASKS = ["ensure_session", "parse_products", "check_result"]

created_tasks = []
created_dags = []


# --------------------------------------------------------------- заглушки --
class FakeTask:
    def __init__(self, task_id, **kwargs):
        self.task_id = task_id
        self.kwargs = kwargs
        self.downstream = []
        created_tasks.append(self)

    def __rshift__(self, other):
        self.downstream.append(other)
        return other


class FakeDAG:
    def __init__(self, dag_id=None, **kwargs):
        self.dag_id = dag_id
        self.kwargs = kwargs
        created_dags.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def stub_module(name: str, **attributes) -> types.ModuleType:
    """Модуль-заглушка с заданными атрибутами."""
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    return module


def install_stubs() -> None:
    """Подменяет airflow и pendulum ровно тем, что импортирует файл DAG.

    Путь Airflow 3 (airflow.providers.standard) намеренно не подменяется:
    DAG должен откатиться на импорт Airflow 2.
    """
    sys.modules.update(
        {
            "airflow": stub_module("airflow", DAG=FakeDAG),
            "airflow.operators": stub_module("airflow.operators"),
            "airflow.operators.bash": stub_module("airflow.operators.bash", BashOperator=FakeTask),
            "pendulum": stub_module(
                "pendulum", datetime=lambda *args, **kwargs: dt.datetime(*args)
            ),
        }
    )


_loaded = None


def load_dag():
    """Импортирует файл DAG на заглушках. Повторный вызов отдаёт кеш.

    Кеш здесь не оптимизация, а корректность: импорт наполняет created_tasks
    и created_dags, и второй импорт задвоил бы их.
    """
    global _loaded
    if _loaded is None:
        install_stubs()
        sys.path.insert(0, str(DAG_FILE.parent))
        _loaded = __import__(DAG_FILE.stem)
    return _loaded


def remove_stubs():
    """Снимает заглушки, чтобы они не достались соседним тестовым модулям."""
    global _loaded
    for name in STUBBED_MODULES:
        sys.modules.pop(name, None)
    sys.modules.pop(DAG_FILE.stem, None)
    _loaded = None
    created_tasks.clear()
    created_dags.clear()


@pytest.fixture(autouse=True, scope="module")
def _airflow_stubs():
    """Заглушки живут ровно столько, сколько выполняется этот модуль."""
    load_dag()
    yield
    remove_stubs()


# ------------------------------------------------------------- рендер Jinja --
def render(template_text, variables):
    """Отрисовывает шаблон так же, как это сделал бы Airflow."""
    try:
        import jinja2
    except ImportError:  # pragma: no cover - зависит от окружения
        pytest.skip("jinja2 не установлен (pip install -r requirements.txt)")

    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    # ds - фильтр Airflow, форматирующий дату как ГГГГ-ММ-ДД
    env.filters["ds"] = lambda value: value.strftime("%Y-%m-%d")

    var_accessor = types.SimpleNamespace(value=variables)
    return (
        env.from_string(template_text)
        .render(
            var=var_accessor,
            data_interval_start=INTERVAL_START,
            data_interval_end=INTERVAL_END,
        )
        .strip()
    )


def _templates(module):
    return (("parse_products", module.PARSE_COMMAND), ("check_result", module.CHECK_COMMAND))


# ------------------------------------------------------------------ тесты ---
def test_dag_parameters():
    """DAG создан с ожидаемым идентификатором и параметрами запуска."""
    load_dag()
    assert len(created_dags) == 1, created_dags
    dag = created_dags[0]

    assert dag.dag_id == "ozon_products_daily"
    assert dag.kwargs.get("catchup") is False, "catchup должен быть False"
    assert dag.kwargs.get("max_active_runs") == 1, "параллельные запуски поднимут два браузера"
    assert dag.kwargs.get("schedule") == "30 5 * * *"


def test_task_order():
    """Состав задач и порядок их связывания."""
    load_dag()
    assert [task.task_id for task in created_tasks] == EXPECTED_TASKS

    by_id = {task.task_id: task for task in created_tasks}
    chain = []
    node = by_id[EXPECTED_TASKS[0]]
    while node is not None:
        chain.append(node.task_id)
        node = node.downstream[0] if node.downstream else None
    assert chain == EXPECTED_TASKS


def test_parse_task_runs_headless():
    """На сервере окну браузера взяться неоткуда - HEADLESS обязан быть включён."""
    load_dag()
    parse_task = {task.task_id: task for task in created_tasks}["parse_products"]
    assert parse_task.kwargs.get("env", {}).get("HEADLESS") == "1"
    assert parse_task.kwargs.get("append_env") is True


def test_commands_render_with_defaults():
    """Команды отрисовываются без заданных переменных Airflow."""
    module = load_dag()
    for name, template in _templates(module) + (("ensure_session", module.SESSION_COMMAND),):
        text = render(template, {})
        assert "{{" not in text and "}}" not in text, "{}: остались шаблоны: {}".format(name, text)
        assert "/opt/ozon_parser" in text, name
        assert ".venv/bin/python" in text, name


def test_commands_render_with_variables():
    """Переменные Airflow подставляются в команды."""
    module = load_dag()
    custom = {
        "ozon_project_dir": "/srv/ozon",
        "ozon_python": "/srv/ozon/.venv/bin/python",
        "ozon_storage": "clickhouse",
        "ozon_min_rows": "2",
    }
    for name, template in _templates(module):
        text = render(template, custom)
        assert "/srv/ozon" in text, name
        assert "clickhouse" in text, name
    assert "--min-rows 2" in render(module.CHECK_COMMAND, custom)


def test_snapshot_date_is_run_day():
    """Срез датируется днём ЗАПУСКА, а не началом интервала.

    Регрессия: раньше в команды подставлялся data_interval_start, и запуск
    23-го в 05:30 писал цены в срез за 22-е - вся временная ось витрины
    v_ozon_products_daily была сдвинута на сутки.
    """
    module = load_dag()
    for name, template in _templates(module):
        text = render(template, {})
        assert "--date {}".format(RUN_DAY) in text, "{}: дата среза {!r}".format(name, text)


def test_min_success_rate_is_passed_to_parser():
    """Один разобранный товар из пятисот не должен считаться успехом."""
    module = load_dag()
    assert "--min-success-rate 0.8" in render(module.PARSE_COMMAND, {})
    assert "--min-success-rate 0.5" in render(
        module.PARSE_COMMAND, {"ozon_min_success_rate": "0.5"}
    )


def test_session_task_refreshes_stale_session_without_human():
    """ensure_session перевыпускает протухшую сессию сам и в фоне.

    --non-interactive запрещает ручной вход и окно согласия Google, которые
    на сервере некому пройти; лимит возраста берётся из переменной Airflow.
    """
    module = load_dag()
    text = render(module.SESSION_COMMAND, {})
    assert "get_cookies.py" in text
    assert "--non-interactive" in text
    assert "--max-age-days 14" in text
    assert "--force" not in text, "свежую сессию перевыпускать незачем"
    assert "--max-age-days 7" in render(module.SESSION_COMMAND, {"ozon_cookies_ttl_days": "7"})


def test_session_task_runs_headless_with_short_timeout():
    """Вход идёт в фоне и не занимает слот воркера на полтора часа."""
    load_dag()
    task = {task.task_id: task for task in created_tasks}["ensure_session"]
    assert task.kwargs.get("env", {}).get("HEADLESS") == "1"
    assert task.kwargs.get("append_env") is True
    assert task.kwargs.get("execution_timeout") <= dt.timedelta(minutes=30)
