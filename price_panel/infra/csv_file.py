"""Атомарная запись CSV: снимок записей с заданными колонками."""

from __future__ import annotations

import contextlib
import csv
import os
from collections.abc import Sequence
from pathlib import Path


def write_csv(rows: Sequence[dict], path: Path, fields: Sequence[str]) -> None:
    """Записывает строки в CSV (UTF-8 с BOM, чтобы Excel не ломал кириллицу).

    Пишем во временный файл и подменяем через os.replace, чтобы обрыв посреди
    записи не оставил обрезанный, но внешне валидный CSV.

    :raises OSError: файл не записан; прежний CSV на месте не тронут.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    try:
        with tmp_path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({field: row.get(field) for field in fields})
        os.replace(tmp_path, path)
    except OSError:
        # Неудача уборки временного файла - мелочь рядом с основной ошибкой.
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise
