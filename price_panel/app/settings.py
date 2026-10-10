"""Настройки конвейера discovery -> panel -> parse из config.yaml.

Разделение с config.py намеренное:
  * .env (config.py) - секреты и всё, что зависит от машины: DSN базы, путь к
    cookies, режим браузера, а также паузы и повторы парсера карточек. Эти
    параметры уже были в проекте, и parse_ozon.py продолжает читать их оттуда;
  * config.yaml (этот модуль) - поведение конвейера: какие категории
    наблюдаем, размер panel, доли top / tail, расписание, параметры замера
    скорости. Секретов здесь нет, файл лежит в репозитории.

Путь к файлу можно переопределить переменной PIPELINE_CONFIG.
"""

from __future__ import annotations

import datetime as dt
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from price_panel.infra import config
from price_panel.marketplaces.ozon import constants

DEFAULT_PATH = config.BASE_DIR / "config.yaml"
DEFAULT_BACKUP_DIR = config.BASE_DIR / "backups"

SOURCE_OZON_LISTING = "ozon_listing"
SOURCE_DATA_OZON = "data_ozon"
SOURCES = (SOURCE_OZON_LISTING, SOURCE_DATA_OZON)

# Имя категории попадает в логи, CSV и колонку sku_panel.category.
NAME_RE = re.compile(r"[a-z0-9_]+")
LISTING_URL_RE = re.compile(r"https://www\.ozon\.ru/category/[a-z0-9\-]+-\d+/?")
DAILY_AT_RE = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")


class SettingsError(ValueError):
    """config.yaml не найден или заполнен неверно."""


@dataclass(frozen=True)
class CategoryConfig:
    """Одна наблюдаемая категория."""

    name: str
    panel_size: int
    top_ratio: float
    source: str = SOURCE_OZON_LISTING
    title: str = ""
    # ozon_listing: адрес категории и диапазон страниц для случайного хвоста.
    url: str = ""
    tail_max_page: int = 300
    tail_items_per_page: int = 2
    # data_ozon: id узлов дерева категорий data.ozon.ru и метрика сортировки.
    category_ids: tuple = ()
    sort_attribute: str = "sum_gmv"

    @property
    def top_size(self) -> int:
        return round(self.panel_size * self.top_ratio)

    @property
    def tail_size(self) -> int:
        return self.panel_size - self.top_size

    @property
    def listing_path(self) -> str:
        """Путь категории без домена: /category/smartfony-15502/."""
        path = re.sub(r"^https://www\.ozon\.ru", "", self.url.split("?")[0])
        return path if path.endswith("/") else path + "/"


@dataclass(frozen=True)
class DiscoverySettings:
    categories: tuple
    seed: str = "ozon-panel"
    # Пауза между запросами к листингу, от конца предыдущего запроса, и её
    # случайный разброс (доля): 0.3 -> от 0.7 до 1.3 паузы.
    request_delay: float = 5.0
    request_jitter: float = 0.3
    # Сколько ждать, когда Ozon ограничил запросы (403/429/антибот-заглушка),
    # перед каждой следующей попыткой. Когда список кончился - discovery
    # останавливается, сохранив найденное.
    block_backoff: tuple = (10.0, 60.0, 180.0, 300.0)
    # Пауза между категориями, секунды.
    category_pause: float = 60.0
    export_csv: Path | None = None


@dataclass(frozen=True)
class ParserSettings:
    csv_export: Path | None = None
    min_success_rate: float = 0.0
    # Откуда брать цену: api - внутренний API, как старый сценарий; html -
    # JSON, встроенный в HTML карточки (API остаётся запасным путём).
    price_source: str = constants.PRICE_SOURCE_API
    # Раз в сколько дней запрашивать описание и полные характеристики
    # (вторую часть карточки): 1 - каждый день, 0 - только у SKU без них.
    details_refresh_days: int = 1


@dataclass(frozen=True)
class BackupSettings:
    """Резервная копия базы после ежедневного прогона (backup.py)."""

    enabled: bool = False
    directory: Path = DEFAULT_BACKUP_DIR
    keep: int = 14


@dataclass(frozen=True)
class ScheduleSettings:
    daily_at: dt.time = dt.time(5, 30)
    timezone: str = "Europe/Moscow"
    run_on_start: bool = False
    ensure_session: bool = True
    session_max_age_days: int = 14
    parse_timeout_hours: float = 10.0
    block_retries: int = 1
    block_retry_delay_hours: float = 3.0


@dataclass(frozen=True)
class BenchmarkSettings:
    sample_size: int = 50
    window_hours: float = 8.0
    safety_factor: float = 0.8


@dataclass(frozen=True)
class Settings:
    discovery: DiscoverySettings
    parser: ParserSettings = field(default_factory=ParserSettings)
    schedule: ScheduleSettings = field(default_factory=ScheduleSettings)
    benchmark: BenchmarkSettings = field(default_factory=BenchmarkSettings)
    backup: BackupSettings = field(default_factory=BackupSettings)
    path: Path | None = None

    def category(self, name: str) -> CategoryConfig:
        for category in self.discovery.categories:
            if category.name == name:
                return category
        known = ", ".join(c.name for c in self.discovery.categories)
        raise SettingsError("Категории {!r} нет в конфигурации (есть: {})".format(name, known))


# ------------------------------------------------------------------ разбор --
def _section(data: dict, key: str) -> dict:
    value = data.get(key) or {}
    if not isinstance(value, dict):
        raise SettingsError("Раздел {!r} должен быть словарём".format(key))
    return value


def _number(section: dict, key: str, default, kind, minimum, maximum, where: str):
    value = section.get(key, default)
    try:
        value = kind(value)
    except (TypeError, ValueError):
        raise SettingsError(
            "{}{}: ожидается число, получено {!r}".format(where, key, value)
        ) from None
    if minimum is not None and value < minimum or maximum is not None and value > maximum:
        raise SettingsError(
            "{}{}={} вне диапазона [{}, {}]".format(where, key, value, minimum, maximum)
        )
    return value


def _float(
    section: dict,
    key: str,
    default: float,
    minimum: float | None = None,
    maximum: float | None = None,
    where: str = "",
) -> float:
    return float(_number(section, key, default, float, minimum, maximum, where))


def _int(
    section: dict,
    key: str,
    default: int,
    minimum: int | None = None,
    maximum: int | None = None,
    where: str = "",
) -> int:
    return int(_number(section, key, default, int, minimum, maximum, where))


def _path(value: Any) -> Path | None:
    if not value:
        return None
    path = Path(str(value))
    return path if path.is_absolute() else config.BASE_DIR / path


def _backoff(section: dict) -> tuple:
    raw = section.get("block_backoff", [10, 60, 180, 300])
    if not isinstance(raw, list):
        raise SettingsError(
            "discovery.block_backoff: ожидается список секунд, например [10, 60, 180, 300]"
        )
    return tuple(
        _float({"value": item}, "value", 0, minimum=1.0, where="discovery.block_backoff.")
        for item in raw
    )


def _top_ratio(section: dict, default: float, where: str) -> float:
    top = _float(section, "top_ratio", default, minimum=0.0, maximum=1.0, where=where)
    if "tail_ratio" in section:
        tail = _float(section, "tail_ratio", 1 - top, minimum=0.0, maximum=1.0, where=where)
        if abs(top + tail - 1.0) > 1e-6:
            raise SettingsError(
                "{}top_ratio + tail_ratio должны давать 1 (сейчас {} + {})".format(where, top, tail)
            )
    return top


def _category(raw: Any, defaults: dict) -> CategoryConfig:
    if not isinstance(raw, dict):
        raise SettingsError("Категория должна быть словарём: {!r}".format(raw))
    name = str(raw.get("name") or "")
    if not NAME_RE.fullmatch(name):
        raise SettingsError(
            "Имя категории {!r}: только латиница в нижнем регистре, цифры и '_'".format(name)
        )
    where = "discovery.categories[{}].".format(name)

    source = str(raw.get("source") or SOURCE_OZON_LISTING)
    if source not in SOURCES:
        raise SettingsError(
            "{}source={!r}, допустимо: {}".format(where, source, ", ".join(SOURCES))
        )

    url = str(raw.get("url") or "")
    category_ids = tuple(str(item) for item in (raw.get("category_ids") or ()))
    if source == SOURCE_OZON_LISTING and not LISTING_URL_RE.fullmatch(url.split("?")[0]):
        raise SettingsError(
            "{}url={!r}: нужен адрес вида https://www.ozon.ru/category/<slug>-<id>/".format(
                where, url
            )
        )
    if source == SOURCE_DATA_OZON and not category_ids:
        raise SettingsError("{}category_ids: для data_ozon нужен хотя бы один id".format(where))

    return CategoryConfig(
        name=name,
        title=str(raw.get("title") or name),
        source=source,
        url=url,
        panel_size=_int(raw, "panel_size", 0, minimum=1, where=where),
        top_ratio=_top_ratio(raw, defaults["top_ratio"], where),
        tail_max_page=_int(raw, "tail_max_page", defaults["tail_max_page"], minimum=2, where=where),
        tail_items_per_page=_int(
            raw, "tail_items_per_page", defaults["tail_items_per_page"], minimum=1, where=where
        ),
        category_ids=category_ids,
        sort_attribute=str(raw.get("sort_attribute") or "sum_gmv"),
    )


def parse_settings(data: Any, path: Path | None = None) -> Settings:
    """Проверяет и собирает настройки из разобранного YAML."""
    if not isinstance(data, dict):
        raise SettingsError("config.yaml: ожидается словарь верхнего уровня")

    discovery = _section(data, "discovery")
    defaults = {
        "top_ratio": _top_ratio(discovery, 0.4, "discovery."),
        "tail_max_page": _int(discovery, "tail_max_page", 300, minimum=2, where="discovery."),
        "tail_items_per_page": _int(
            discovery, "tail_items_per_page", 2, minimum=1, where="discovery."
        ),
    }
    categories = tuple(_category(raw, defaults) for raw in discovery.get("categories") or ())
    if not categories:
        raise SettingsError("discovery.categories: не задано ни одной категории")
    names = [category.name for category in categories]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise SettingsError("discovery.categories: повторяются имена {}".format(duplicates))

    parser = _section(data, "parser")
    schedule = _section(data, "schedule")
    benchmark = _section(data, "benchmark")
    backup = _section(data, "backup")

    daily_at = str(schedule.get("daily_at") or "05:30")
    match = DAILY_AT_RE.fullmatch(daily_at)
    if not match:
        raise SettingsError("schedule.daily_at={!r}: ожидается ЧЧ:ММ".format(daily_at))

    price_source = str(parser.get("price_source") or constants.PRICE_SOURCE_API)
    if price_source not in constants.PRICE_SOURCES:
        raise SettingsError(
            "parser.price_source={!r}, допустимо: {}".format(
                price_source, ", ".join(constants.PRICE_SOURCES)
            )
        )

    return Settings(
        path=path,
        discovery=DiscoverySettings(
            categories=categories,
            seed=str(discovery.get("seed") or "ozon-panel"),
            request_delay=_float(discovery, "request_delay", 5.0, minimum=0.5, where="discovery."),
            request_jitter=_float(
                discovery, "request_jitter", 0.3, minimum=0.0, maximum=0.9, where="discovery."
            ),
            block_backoff=_backoff(discovery),
            category_pause=_float(
                discovery, "category_pause", 60.0, minimum=0.0, where="discovery."
            ),
            export_csv=_path(discovery.get("export_csv")),
        ),
        parser=ParserSettings(
            csv_export=_path(parser.get("csv_export")),
            min_success_rate=_float(
                parser, "min_success_rate", 0.0, minimum=0.0, maximum=1.0, where="parser."
            ),
            price_source=price_source,
            details_refresh_days=_int(
                parser, "details_refresh_days", 1, minimum=0, maximum=365, where="parser."
            ),
        ),
        backup=BackupSettings(
            enabled=bool(backup.get("enabled", False)),
            directory=_path(backup.get("dir")) or DEFAULT_BACKUP_DIR,
            keep=_int(backup, "keep", 14, minimum=1, where="backup."),
        ),
        schedule=ScheduleSettings(
            daily_at=dt.time(int(match.group(1)), int(match.group(2))),
            timezone=str(schedule.get("timezone") or "Europe/Moscow"),
            run_on_start=bool(schedule.get("run_on_start", False)),
            ensure_session=bool(schedule.get("ensure_session", True)),
            session_max_age_days=_int(
                schedule, "session_max_age_days", 14, minimum=0, where="schedule."
            ),
            parse_timeout_hours=_float(
                schedule, "parse_timeout_hours", 10.0, minimum=0.1, where="schedule."
            ),
            block_retries=_int(
                schedule, "block_retries", 1, minimum=0, maximum=3, where="schedule."
            ),
            # Меньше получаса - уже не «переждать блокировку», а долбить Ozon.
            block_retry_delay_hours=_float(
                schedule, "block_retry_delay_hours", 3.0, minimum=0.5, maximum=12, where="schedule."
            ),
        ),
        benchmark=BenchmarkSettings(
            sample_size=_int(benchmark, "sample_size", 50, minimum=1, where="benchmark."),
            window_hours=_float(
                benchmark, "window_hours", 8.0, minimum=0.1, maximum=24, where="benchmark."
            ),
            safety_factor=_float(
                benchmark, "safety_factor", 0.8, minimum=0.1, maximum=1.0, where="benchmark."
            ),
        ),
    )


def load_settings(path: Path | None = None) -> Settings:
    """Читает config.yaml (или PIPELINE_CONFIG)."""
    import yaml  # локальный импорт: parse_ozon.py без конвейера YAML не нужен

    path = path or Path(os.getenv("PIPELINE_CONFIG") or DEFAULT_PATH)
    if not path.is_absolute():
        path = config.BASE_DIR / path
    if not path.exists():
        raise SettingsError("Файл настроек не найден: {}".format(path))
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SettingsError("{} не разобрался как YAML: {}".format(path, exc)) from exc
    return parse_settings(data, path)
