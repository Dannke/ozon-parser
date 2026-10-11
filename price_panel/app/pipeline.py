"""Прогон парсера по sku_panel (или файлу) с записью в PostgreSQL.

Карточки собирает адаптер маркетплейса, к которому привязано хранилище
(Ozon или WB), цикл - ядро (core.collect), как и у parse_ozon.py. Здесь к
нему добавляется учёт запуска:

    parse_runs     - запуск: когда, сколько SKU, сколько успешно, скорость;
    products       - последние атрибуты карточки;
    price_history  - наблюдение цены, рейтинга, отзывов на момент сбора;
    parse_errors   - причина неудачи по каждому проблемному SKU;
    raw_products   - сырой ответ сайта (у WB), хранится RAW_KEEP_DAYS дней.

Товар пишется в базу сразу после разбора, до перехода к следующему SKU
(RunObserver), поэтому сбой посреди ночного прогона не теряет уже собранное.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import random
import statistics
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from price_panel.app.settings import Settings, SettingsError
from price_panel.core.collect import (
    Breaker,
    MarketplaceAdapter,
    Observers,
    RowBatches,
    RunObserver,
    collect,
)
from price_panel.core.models import EXIT_BLOCKED, WB
from price_panel.infra import config
from price_panel.infra.csv_file import write_csv
from price_panel.infra.db import DatabaseError
from price_panel.infra.logger import get_logger
from price_panel.infra.warehouse import Warehouse
from price_panel.marketplaces.ozon import parse
from price_panel.marketplaces.ozon.extract import FIELDS
from price_panel.marketplaces.wb import adapter as wb

log = get_logger("pipeline")

# Ошибки, при которых SKU вообще не обрабатывался: в скорость не входят.
# blocked - прогон остановлен предохранителем (Ozon отказывал SKU подряд).
NOT_ATTEMPTED = ("not_processed", "interrupted", "blocked")


class ParseLockBusy(RuntimeError):
    """Уже идёт другой прогон парсера."""


class WarehouseObserver(RunObserver):
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
        log.info(
            "SKU SUCCESS run_id=%s sku=%s price=%s old_price=%s available=%s seconds=%.1f",
            self.run_id,
            sku,
            product.get("price"),
            product.get("old_price"),
            product.get("is_available"),
            seconds,
        )

    def sku_failed(
        self, sku: str, error_type: str, message: str, seconds: float, attempts: int = 0
    ) -> None:
        self.reported.add(sku)
        if seconds > 0:
            self.durations.append(seconds)
        self.errors[error_type] += 1
        log.warning(
            "SKU ERROR run_id=%s sku=%s type=%s attempts=%s message=%s",
            self.run_id,
            sku,
            error_type,
            attempts,
            (message or "")[:300],
        )
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
    avg_sku_seconds: float | None
    status: str
    exit_code: int
    request_delay: float = field(default_factory=lambda: config.REQUEST_DELAY)

    @property
    def error_count(self) -> int:
        return sum(self.errors.values())

    @property
    def sku_per_minute(self) -> float | None:
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


def details_schedule(
    wh: Warehouse, skus: list, refresh_days: int, today: dt.date | None = None
) -> frozenset | None:
    """Каким SKU в этом прогоне запрашивать вторую часть карточки; None - всем.

    Описание и полные характеристики (art_set, has_rich_content, цвет,
    материал) почти не меняются, а это лишний запрос к Ozon на каждый товар:
      * SKU без описания (новые в panel или оно ни разу не пришло) - в каждом
        прогоне, пока не придёт;
      * остальные - раз в refresh_days дней, причём каждый день своя доля
        panel (по md5 от SKU), а не вся panel разом раз в неделю.
    refresh_days = 1 - каждый день, как раньше; 0 - только SKU без описания.
    """
    if refresh_days == 1:
        return None
    missing = wh.skus_without_details(skus)
    if refresh_days <= 0:
        return frozenset(missing)
    day = (today or dt.date.today()).toordinal()
    return frozenset(
        sku
        for sku in skus
        if sku in missing
        or (day + int(hashlib.md5(sku.encode("utf-8")).hexdigest()[:8], 16)) % refresh_days == 0
    )


def csv_export(path: Path) -> RowBatches:
    """Снимок прогона panel в CSV (parser.csv_export) - артефакт для глазной проверки.

    Сбой записи не останавливает прогон: основное хранилище - PostgreSQL.
    """

    def save(rows: list) -> bool:
        try:
            write_csv(rows, path, FIELDS)
        except OSError as exc:
            log.error("CSV %s не записан: %s", path, exc)
            return False
        log.info("Сохранено строк в CSV: %s -> %s", len(rows), path)
        return True

    return RowBatches(save, config.BATCH_SIZE)


@dataclass(frozen=True)
class Collector:
    """Чем и как собирать прогон: адаптер маркетплейса и его параметры."""

    adapter: MarketplaceAdapter
    log: logging.Logger
    request_delay: float
    region: str | None = None
    csv_path: Path | None = None
    # Параметры маркетплейса для строки PARSER START.
    describe: str = ""


def collector(wh: Warehouse, skus: list, settings: Settings) -> Collector:
    """Адаптер маркетплейса, к которому привязано хранилище.

    :raises SettingsError: сбор WB не включён в config.yaml.
    """
    if wh.marketplace == WB:
        # Пока ozon-market-analytics не различает маркетплейсы, строки WB в
        # боевой базе смешались бы с Ozon - поэтому сбор WB включается явно.
        if not settings.wb.enabled:
            raise SettingsError("сбор WB выключен: marketplaces.wb.enabled в config.yaml")
        options = settings.wb
        return Collector(
            adapter=wb.WbAdapter(options.dest, options.batch_size, options.request_delay),
            log=wb.log,
            request_delay=options.request_delay,
            region=options.dest,
            describe="dest={} batch_size={}".format(options.dest, options.batch_size),
        )
    details_due = details_schedule(wh, skus, settings.parser.details_refresh_days)
    options = parse.ParseOptions(price_source=settings.parser.price_source, details_for=details_due)
    return Collector(
        adapter=parse.OzonAdapter(options),
        log=parse.log,
        request_delay=config.REQUEST_DELAY,
        csv_path=settings.parser.csv_export,
        describe="page_interval={:.1f} price_source={} details={}".format(
            config.PAGE_INTERVAL,
            options.price_source,
            len(skus) if details_due is None else len(details_due),
        ),
    )


def run_parse(
    wh: Warehouse, skus: list, settings: Settings, kind: str = "manual", sku_source: str = "panel"
) -> ParseReport:
    """Парсит SKU и ведёт учёт прогона в базе.

    :raises ParseLockBusy: параллельно идёт другой прогон.
    :raises SettingsError: маркетплейс не настроен (collector).
    """
    source = collector(wh, skus, settings)
    if not wh.try_parse_lock():
        raise ParseLockBusy("Уже идёт другой прогон парсера (блокировка в PostgreSQL занята)")
    stale = wh.close_stale_runs()
    if stale:
        log.warning("Прогонов, брошенных упавшим процессом, помечено interrupted: %s", stale)

    run_id = wh.start_parse_run(kind, sku_source, len(skus), source.request_delay, source.region)
    observer = WarehouseObserver(wh, run_id)
    csv_path = source.csv_path
    log.info(
        "PARSER START run_id=%s marketplace=%s kind=%s source=%s total=%s request_delay=%.1f "
        "%s csv=%s",
        run_id,
        wh.marketplace,
        kind,
        sku_source,
        len(skus),
        source.request_delay,
        source.describe,
        csv_path or "-",
    )

    started = time.monotonic()
    export = csv_export(csv_path) if csv_path else None
    try:
        collect(
            source.adapter,
            skus,
            Observers(observer, export) if export else observer,
            # Порог один для всех: у WB отказ батча - antibot у каждого его SKU.
            Breaker(config.MAX_CONSECUTIVE_FAILURES, config.MAX_CONSECUTIVE_CHALLENGES),
            source.log,
        )
        if export:
            export.flush()
    finally:
        # SKU, о которых парсер не отчитался (не открылась сессия, программная
        # ошибка), тоже должны остаться в учёте, а не пропасть молча.
        for sku in skus:
            if sku not in observer.reported:
                observer.sku_failed(
                    sku,
                    "not_processed",
                    "прогон завершился до обработки SKU (см. logs/{}.log)".format(source.log.name),
                    0.0,
                )
        duration = time.monotonic() - started
        report = ParseReport(
            run_id=run_id,
            kind=kind,
            total=len(skus),
            success=observer.success,
            errors=observer.errors,
            processed=observer.processed,
            duration=duration,
            avg_sku_seconds=statistics.mean(observer.durations) if observer.durations else None,
            status=run_status(observer.success, observer.errors),
            exit_code=1,
            request_delay=source.request_delay,
        )
        speed = report.sku_per_minute
        try:
            wh.finish_parse_run(
                run_id,
                report.status,
                report.processed,
                report.success,
                report.error_count,
                duration,
                round(speed, 2) if speed else None,
                round(report.avg_sku_seconds, 2) if report.avg_sku_seconds else None,
            )
        except DatabaseError as exc:
            log.error("Итог прогона %s не записан: %s", run_id, exc)
        try:
            pruned = wh.prune_raw()
        except DatabaseError as exc:
            log.error("Старые сырые ответы не удалены: %s", exc)
        else:
            if pruned:
                log.info("Удалено сырых ответов старше срока хранения: %s", pruned)

    ok = (
        report.success > 0
        and report.status != "blocked"
        and report.success_rate >= settings.parser.min_success_rate
    )
    report.exit_code = 0 if ok else EXIT_BLOCKED if report.status == "blocked" else 1
    log.info(
        "PARSER FINISHED run_id=%s status=%s success=%s errors=%s duration=%.0fs speed=%s SKU/min",
        run_id,
        report.status,
        report.success,
        report.error_count,
        duration,
        "{:.2f}".format(speed) if speed else "-",
    )
    return report


def sample_panel(wh: Warehouse, size: int, seed: str | None = None) -> list:
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


def format_report(
    report: ParseReport, settings: Settings | None = None, title: str = "Parse run"
) -> str:
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
        lines.append(
            "  Avg parse time:   {:.1f} s/SKU (+ пауза {:.1f} с между SKU)".format(
                report.avg_sku_seconds, report.request_delay
            )
        )
    lines += [
        "  Success:          {} ({:.1f}%)".format(report.success, 100 * report.success / total),
        "  Errors:           {} ({:.1f}%) {}".format(
            report.error_count, 100 * report.error_count / total, errors
        ),
    ]
    if settings is not None and speed:
        window = settings.benchmark.window_hours
        factor = settings.benchmark.safety_factor
        per_window, recommended = capacity(speed, window, factor)
        lines += [
            "  Daily capacity:   {:g} h x {:.2f} SKU/min = {} SKU".format(
                window, speed, per_window
            ),
            "  Recommended panel (x{:g} запас): ~{} SKU".format(factor, recommended),
        ]
    return "\n".join(lines)
