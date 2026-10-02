"""Разбор config.yaml: рабочий файл репозитория и ошибки заполнения."""

from __future__ import annotations

import copy
import datetime as dt

import pytest

from ozon_parser import settings as settings_module
from ozon_parser.settings import SettingsError, load_settings, parse_settings

VALID = {
    "discovery": {
        "seed": "s",
        "top_ratio": 0.4,
        "categories": [
            {"name": "phones", "url": "https://www.ozon.ru/category/smartfony-15502/",
             "panel_size": 10},
            {"name": "cases", "url": "https://www.ozon.ru/category/chehly-15892/",
             "panel_size": 7, "top_ratio": 0.5, "tail_max_page": 50},
            {"name": "best", "source": "data_ozon", "category_ids": ["95139"],
             "panel_size": 5},
        ],
    },
    "schedule": {"daily_at": "06:15", "timezone": "Europe/Moscow"},
}


def test_repository_config_is_valid():
    """config.yaml из репозитория читается без ошибок: 3-4 категории разной природы."""
    settings = load_settings(settings_module.DEFAULT_PATH)
    assert 3 <= len(settings.discovery.categories) <= 4
    assert all(c.source == "ozon_listing" for c in settings.discovery.categories)
    assert settings.discovery.export_csv is not None


def test_sizes_and_overrides():
    settings = parse_settings(VALID)
    phones, cases, best = settings.discovery.categories
    assert (phones.top_size, phones.tail_size) == (4, 6)
    assert (cases.top_size, cases.tail_size) == (4, 3)   # round(3.5) == 4
    assert cases.tail_max_page == 50
    assert phones.tail_max_page == 300                   # значение по умолчанию
    assert phones.listing_path == "/category/smartfony-15502/"
    assert best.category_ids == ("95139",)
    assert settings.schedule.daily_at == dt.time(6, 15)
    assert settings.category("cases") is cases


def mutate(**changes):
    data = copy.deepcopy(VALID)
    data["discovery"]["categories"][0].update(changes)
    return data


@pytest.mark.parametrize("data, message", [
    (mutate(url="https://www.ozon.ru/search/?text=phone"), "url"),
    (mutate(name="Smart Phones"), "Имя категории"),
    (mutate(panel_size=0), "panel_size"),
    (mutate(source="wildberries"), "source"),
    (mutate(top_ratio=1.5), "top_ratio"),
    (mutate(top_ratio=0.4, tail_ratio=0.5), "должны давать 1"),
    (mutate(name="cases"), "повторяются"),
])
def test_invalid_category(data, message):
    with pytest.raises(SettingsError, match=message):
        parse_settings(data)


def test_data_ozon_needs_category_ids():
    data = copy.deepcopy(VALID)
    data["discovery"]["categories"][2]["category_ids"] = []
    with pytest.raises(SettingsError, match="category_ids"):
        parse_settings(data)


def test_invalid_schedule_and_empty_categories():
    data = copy.deepcopy(VALID)
    data["schedule"]["daily_at"] = "25:00"
    with pytest.raises(SettingsError, match="daily_at"):
        parse_settings(data)
    with pytest.raises(SettingsError, match="не задано ни одной категории"):
        parse_settings({"discovery": {"categories": []}})


def test_discovery_pacing_defaults_and_overrides():
    """Темп discovery и выжидание при отказе Ozon задаются в config.yaml."""
    defaults = parse_settings(VALID).discovery
    assert defaults.request_delay == 5.0
    assert defaults.block_backoff == (10.0, 60.0, 180.0, 300.0)
    assert defaults.category_pause == 60.0

    data = copy.deepcopy(VALID)
    data["discovery"].update(request_delay=7, request_jitter=0, block_backoff=[30, 90],
                             category_pause=0)
    custom = parse_settings(data).discovery
    assert (custom.request_delay, custom.request_jitter) == (7.0, 0.0)
    assert custom.block_backoff == (30.0, 90.0)
    assert custom.category_pause == 0.0


@pytest.mark.parametrize("backoff", [60, [10, 0], ["минута"]])
def test_invalid_block_backoff(backoff):
    data = copy.deepcopy(VALID)
    data["discovery"]["block_backoff"] = backoff
    with pytest.raises(SettingsError, match="block_backoff"):
        parse_settings(data)


def test_unknown_category_name():
    with pytest.raises(SettingsError, match="нет в конфигурации"):
        parse_settings(VALID).category("tv")
