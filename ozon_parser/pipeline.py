"""Прогон парсера по sku_panel (или файлу) с записью в PostgreSQL.

Парсер карточек не меняется: это тот же parse.run(), что и у parse_ozon.py.
Здесь к нему добавляется учёт запуска:

    parse_runs     - запуск: когда, сколько SKU, сколько успешно, скорость;
    products       - последние атрибуты карточки;
    price_history  - наблюдение цены, рейтинга, отзывов на момент сбора;
    parse_errors   - причина неудачи по каждому проблемному SKU.

Товар пишется в базу сразу после разбора, до перехода к следующему SKU
(RunObserver), поэтому сбой посреди ночного прогона не теряет уже собранное.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import random
import statistics
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import config, parse
from .db import DatabaseError
from .logger import get_logger
from .settings import Settings
from .warehouse import Warehouse

log = get_logger("pipeline")

# Ошибки, при которых SKU вообще не обрабатывался: в скорость не входят.
# blocked - прогон остановлен предохранителем (Ozon отказывал SKU подряд).
NOT_ATTEMPTED = ("not_processed", "interrupted", "blocked")

# Код выхода parse, если прогон остановил предохранитель. Планировщик по нему
# отличает блокировку Ozon (имеет смысл повторить через несколько часов) от
# прочих неудач (повтор не поможет).
EXIT_BLOCKED = 3


class ParseLockBusy(RuntimeError):
    """Уже идёт другой прогон парсера."""


class WarehouseObserver(parse.RunObserver):
    """Пишет итог каждого SKU в PostgreSQL и ведёт счётчики прогона."""

    def __init__(self, wh: Warehouse, run_id: int):
        self.wh = wh
        self.run_id = run_id
        self.success = 0
        self.errors: Counter = Counter()
        self.durations: list = []
        self.reported: set = set()

    def sku_done(self, sku: str, product: dict, seconds: float) -> None:
        self.reported.add(sku)
        self.durations.append(seconds)
        try:
            self.wh.record_product(self.run_id, product)
        except DatabaseError as exc:
            self.sku_failed(sku, "storage_error", str(exc), 0.0)
            return
        self.success += 1
        log.info("SKU SUCCESS run_id=%s sku=%s price=%s old_price=%s available=%s seconds=%.1f",
                 self.run_id, sku, product.get("price"), product.get("old_price"),
                 product.get("is_available"), seconds)

    def sku_failed(self, sku: str, error_type: str, message: str, seconds: float,
                   attempts: int = 0) -> None:
        self.reported.add(sku)
        if seconds > 0:
            self.durations.append(seconds)
        self.errors[error_type] += 1
        log.warning("SKU ERROR run_id=%s sku=%s type=%s attempts=%s message=%s",
                    self.run_id, sku, error_type, attempts, (message or "")[:300])
        try:
            self.wh.record_error(self.run_id, sku, error_type, message, attempts or None)
        except DatabaseError as exc:
            log.error("SKU %s: ошибку не удалось записать в parse_errors: %s", sku, exc)

    @property
    def error_count(self) -> int:
        return sum(self.errors.values())

    @property
    def processed(self) -> int:
        """Сколько SKU реально прошли через парсер (успех или ошибка разбора)."""
        attempted_errors = sum(n for kind, n in self.errors.items() if kind not in NOT_ATTEMPTED)
        return self.success + attempted_errors


@dataclass
class ParseReport:
    run_id: int
    kind: str
    total: int
    success: int
    errors: Counter
    processed: int
    duration: float
    avg_sku_seconds: Optional[float]
    status: str
    exit_code: int
    request_delay: float = field(default_factory=lambda: config.REQUEST_DELAY)

    @property
    def error_count(self) -> int:
        return sum(self.errors.values())

    @property
    def sku_per_minute(self) -> Optional[float]:
        if self.duration <= 0 or self.processed == 0:
            return None
        return self.processed / (self.duration / 60)

    @property
    def success_rate(self) -> float:
        return self.success / self.total if self.total else 0.0


def run_status(success: int, errors: Counter) -> str:
    if errors.get("interrupted"):
        return "interrupted"
    if errors.get("blocked"):
        return "blocked"
    if not errors:
        return "success"
    return "partial" if success else "failed"


def details_schedule(wh: Warehouse, skus: list, refresh_days: int,
                     today: Optional[dt.date] = None) -> Callable[[str], bool]:
    """Кому в этом прогоне запрашивать вторую часть карточки.

    Описание и полные характеристики (art_set, has_rich_content, цвет,
    материал) почти не меняются, а это лишний запрос к Ozon на каждый товар:
      * SKU без описания (новые в panel или оно ни разу не пришло) - в каждом
        прогоне, пока не придёт;
      * остальные - раз в refresh_days дней, причём каждый день своя доля
        panel (по md5 от SKU), а не вся panel разом раз в неделю.
    refresh_days = 1 - каждый день, как раньше; 0 - только SKU без описания.
    """
    missing = wh.skus_without_details(skus)
    day = (today or dt.date.today()).toordinal()

    def due(sku: str) -> bool:
        if sku in missing:
            return True
        if refresh_days <= 0:
            return False
        slot = int(hashlib.md5(sku.encode("utf-8")).hexdigest()[:8], 16)
        return (day + slot) % refresh_days == 0

    return due


def run_parse(wh: Warehouse, skus: list, settings: Settings, kind: str = "manual",
              sku_source: str = "panel") -> ParseReport:
    """Парсит SKU и ведёт учёт прогона в базе.

    :raises ParseLockBusy: параллельно идёт другой прогон.
    """
    if not wh.try_parse_lock():
        raise ParseLockBusy("Уже идёт другой прогон парсера (блокировка в PostgreSQL занята)")
    stale = wh.close_stale_runs()
    if stale:
        log.warning("Прогонов, брошенных упавшим процессом, помечено interrupted: %s", stale)

    details_due = details_schedule(wh, skus, settings.parser.details_refresh_days)
    options = parse.ParseOptions(price_source=settings.parser.price_source,
                                 details_for=details_due)
    run_id = wh.start_parse_run(kind, sku_source, len(skus), config.REQUEST_DELAY)
    observer = WarehouseObserver(wh, run_id)
    csv_path = settings.parser.csv_export
    log.info("PARSER START run_id=%s kind=%s source=%s total=%s request_delay=%.1f "
             "price_source=%s details=%s csv=%s",
             run_id, kind, sku_source, len(skus), config.REQUEST_DELAY,
             options.price_source, sum(1 for sku in skus if details_due(sku)), csv_path or "-")

    started = time.monotonic()
    try:
        parse.run(skus, storage_backend="csv" if csv_path else "none", output=csv_path,
                  observer=observer, options=options)
    finally:
        # SKU, о которых парсер не отчитался (не открылась сессия, программная
        # ошибка), тоже должны остаться в учёте, а не пропасть молча.
        for sku in skus:
            if sku not in observer.reported:
                observer.sku_failed(sku, "not_processed",
                                    "прогон завершился до обработки SKU (см. logs/parse_ozon.log)",
                                    0.0)
        duration = time.monotonic() - started
        report = ParseReport(
            run_id=run_id, kind=kind, total=len(skus), success=observer.success,
            errors=observer.errors, processed=observer.processed, duration=duration,
            avg_sku_seconds=statistics.mean(observer.durations) if observer.durations else None,
            status=run_status(observer.success, observer.errors), exit_code=1,
        )
        speed = report.sku_per_minute
        try:
            wh.finish_parse_run(
                run_id, report.status, report.processed, report.success, report.error_count,
                duration, round(speed, 2) if speed else None,
                round(report.avg_sku_seconds, 2) if report.avg_sku_seconds else None)
        except DatabaseError as exc:
            log.error("Итог прогона %s не записан: %s", run_id, exc)

    ok = (report.success > 0 and report.status != "blocked"
          and report.success_rate >= settings.parser.min_success_rate)
    report.exit_code = 0 if ok else EXIT_BLOCKED if report.status == "blocked" else 1
    log.info("PARSER FINISHED run_id=%s status=%s success=%s errors=%s duration=%.0fs "
             "speed=%s SKU/min", run_id, report.status, report.success, report.error_count,
             duration, "{:.2f}".format(speed) if speed else "-")
    return report


def sample_panel(wh: Warehouse, size: int, seed: Optional[str] = None) -> list:
    """Случайные SKU из активной panel для замера скорости."""
    skus = wh.panel_skus()
    rng = random.Random(seed)
    return rng.sample(skus, min(size, len(skus)))


# ---------------------------------------------------------------- отчёты ---
def format_duration(seconds: float) -> str:
    minutes, secs = divmod(int(round(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return "{}h {:02d}m {:02d}s".format(hours, minutes, secs)
    return "{}m {:02d}s".format(minutes, secs)


def capacity(sku_per_minute: float, window_hours: float, safety_factor: float) -> tuple:
    """(SKU за окно, рекомендуемый размер panel с запасом)."""
    per_window = sku_per_minute * 60 * window_hours
    return int(per_window), int(per_window * safety_factor)


def format_report(report: ParseReport, settings: Optional[Settings] = None,
                  title: str = "Parse run") -> str:
    errors = ", ".join("{}: {}".format(k, v) for k, v in report.errors.most_common()) or "-"
    total = report.total or 1
    lines = [
        "{} (run_id={}, status={})".format(title, report.run_id, report.status),
        "  Total SKU:        {}".format(report.total),
        "  Duration:         {}".format(format_duration(report.duration)),
    ]
    speed = report.sku_per_minute
    if speed:
        lines.append("  Speed:            {:.2f} SKU/min".format(speed))
    if report.avg_sku_seconds:
        lines.append("  Avg parse time:   {:.1f} s/SKU (+ пауза {:.1f} с между SKU)".format(
            report.avg_sku_seconds, report.request_delay))
    lines += [
        "  Success:          {} ({:.1f}%)".format(report.success, 100 * report.success / total),
        "  Errors:           {} ({:.1f}%) {}".format(report.error_count,
                                                     100 * report.error_count / total, errors),
    ]
    if settings is not None and speed:
        window = settings.benchmark.window_hours
        factor = settings.benchmark.safety_factor
        per_window, recommended = capacity(speed, window, factor)
        lines += [
            "  Daily capacity:   {:g} h x {:.2f} SKU/min = {} SKU".format(
                window, speed, per_window),
            "  Recommended panel (x{:g} запас): ~{} SKU".format(factor, recommended),
        ]
    return "\n".join(lines)
