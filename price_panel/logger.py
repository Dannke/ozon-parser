"""Единая настройка логирования для всех скриптов проекта.

Логи пишутся одновременно в консоль и в файл ``logs/<имя>.log``,
чтобы после ночного прогона можно было разобрать, что именно упало.
Каталог можно переопределить переменной окружения PRICE_PANEL_LOG_DIR (так делают
тесты, чтобы не засорять боевые логи).
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path(os.getenv("PRICE_PANEL_LOG_DIR")
               or Path(__file__).resolve().parent.parent / "logs")
LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-12s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Ротация: при ежедневном запуске обычный FileHandler растил бы файлы бесконечно.
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 5


def get_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """Возвращает сконфигурированный логгер (повторные вызовы безопасны)."""
    logger = logging.getLogger(name)
    if logger.handlers:  # уже настроен — не плодим дубли обработчиков
        return logger

    logger.setLevel(level)
    logger.propagate = False
    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    console = logging.StreamHandler(stream=sys.stdout)
    console.setFormatter(formatter)
    logger.addHandler(console)

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            LOG_DIR / f"{name}.log",
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError as exc:  # нет прав на запись — работаем только в консоль
        logger.warning("Не удалось создать файл лога: %s", exc)

    return logger
