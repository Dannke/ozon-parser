"""Командная строка конвейера: python -m ozon_parser <команда>.

    migrate     применить миграции схемы PostgreSQL
    discover    найти SKU в категориях из config.yaml и дописать sku_panel
    panel       состав panel по категориям; --export выгружает CSV со SKU
    parse       прогон парсера по panel (или --file / SKU аргументами)
    benchmark   замер скорости на случайных SKU из panel и расчёт ёмкости
    runs        последние прогоны парсера и объём истории
    schedule    ежедневный запуск (для docker-compose); --once - один прогон

Старые точки входа не меняются: parse_ozon.py, get_cookies.py и
check_snapshot.py работают как раньше и не требуют ни PostgreSQL, ни config.yaml.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

from . import discovery, pipeline, scheduler
from .db import DatabaseError
from .logger import get_logger
from .settings import Settings, SettingsError, load_settings
from .warehouse import Warehouse

log = get_logger("pipeline")


def _warehouse() -> Warehouse:
    wh = Warehouse()
    wh.migrate()
    return wh


# ---------------------------------------------------------------- команды --
def cmd_migrate(args, settings: Settings) -> int:
    with Warehouse() as wh:
        applied = wh.migrate()
    print("Миграции применены: {}".format(", ".join(applied)) if applied
          else "Схема актуальна, новых миграций нет")
    return 0


def print_panel(wh: Warehouse) -> None:
    rows = wh.panel_summary()
    if not rows:
        print("Panel пуста: запустите python -m ozon_parser discover")
        return
    print("{:<22} {:<12} {:>8} {:>10}".format("category", "group", "active", "inactive"))
    totals: dict = {}
    for category, group, active, inactive in rows:
        print("{:<22} {:<12} {:>8} {:>10}".format(category, group, active, inactive))
        totals[category] = totals.get(category, 0) + active
    print("-" * 55)
    for category, active in totals.items():
        print("{:<35} {:>8}".format(category + " (active)", active))
    print("{:<35} {:>8}".format("TOTAL active SKU", sum(totals.values())))


def cmd_discover(args, settings: Settings) -> int:
    print("Discovery started")
    with _warehouse() as wh:
        results = discovery.run_discovery(wh, settings, args.category, rebuild=args.rebuild)
        for result in results:
            category = result.category
            print("\nCategory: {} ({})".format(category.name, category.title))
            print("  source:     {}".format(category.source))
            if result.status == "skipped":
                print("  skipped:    {}".format(result.note))
                continue
            if result.status in ("failed", "stopped"):
                print("  {:<11} {}".format(result.status.upper() + ":", result.note))
                continue
            if result.status == "blocked":
                print("  STOPPED:    Ozon ограничил запросы, найденное сохранено")
            depth = (" (выдача кончается на странице {})".format(result.listing_depth)
                     if result.listing_depth else "")
            print("  requests:   {}{}".format(result.pages_requested, depth))
            print("  discovered: {} уникальных SKU ({} уже в panel)".format(
                result.discovered, result.already_in_panel))
            print("  selected:   {} (top {} + tail_random {})".format(
                result.selected, result.selected_top, result.selected_tail))
            if result.note:
                print("  note:       {}".format(result.note))

        unfinished = [r for r in results if r.status in ("failed", "blocked", "stopped")]
        print("\nTotal selected SKU: {}".format(sum(r.selected for r in results)))
        exported = discovery.export_panel(wh, settings)
        if unfinished:
            print("Panel saved, но не собраны до panel_size: {}. Запустите discover "
                  "повторно - готовые категории он пропустит.".format(
                      ", ".join(r.category.name for r in unfinished)))
        else:
            print("Panel saved successfully")
        if exported is not None:
            print("CSV: {} ({} SKU)".format(settings.discovery.export_csv, exported))
        print()
        print_panel(wh)
    return 1 if unfinished else 0


def cmd_panel(args, settings: Settings) -> int:
    with _warehouse() as wh:
        print_panel(wh)
        if args.export:
            count = wh.export_panel_csv(args.export)
            print("\nВыгружено SKU: {} -> {}".format(count, args.export))
    return 0


def _today_start(settings: Settings) -> dt.datetime:
    tz = scheduler.get_timezone(settings.schedule.timezone)
    return scheduler.day_start(dt.datetime.now(dt.timezone.utc), tz)


def _skus_for_parse(args, wh: Warehouse, settings: Settings) -> tuple:
    """(список SKU, откуда он взят)."""
    from .parse import read_skus_file, select_range

    if args.skus:
        skus, source = list(dict.fromkeys(args.skus)), "args"
    elif args.file:
        skus, source = read_skus_file(args.file), "file"
    else:
        since = _today_start(settings) if args.missing_today else None
        skus, source = wh.panel_skus(args.category, missing_since=since), "panel"
    return select_range(skus, 0, args.limit), source


def cmd_parse(args, settings: Settings) -> int:
    if args.missing_today and (args.skus or args.file):
        log.error("--missing-today работает только с panel, без списка SKU и --file")
        return 2
    with _warehouse() as wh:
        skus, source = _skus_for_parse(args, wh, settings)
        if not skus and args.missing_today and wh.panel_skus(args.category, limit=1):
            log.info("Все SKU panel за сегодня уже собраны - парсить нечего")
            print("Все SKU panel за сегодня уже собраны")
            return 0
        if not skus:
            log.error("Нет SKU для парсинга (%s). Сначала: python -m ozon_parser discover",
                      source)
            return 1
        report = pipeline.run_parse(wh, skus, settings, kind=args.kind, sku_source=source)
        print(pipeline.format_report(report))
        return report.exit_code


def cmd_benchmark(args, settings: Settings) -> int:
    size = args.sample or settings.benchmark.sample_size
    with _warehouse() as wh:
        if args.file:
            from .parse import read_skus_file
            skus, source = read_skus_file(args.file)[:size], "file"
        else:
            skus, source = pipeline.sample_panel(wh, size, args.seed), "panel"
        if not skus:
            log.error("Panel пуста - замерять не на чем. Сначала: python -m ozon_parser discover")
            return 1
        report = pipeline.run_parse(wh, skus, settings, kind="benchmark", sku_source=source)
        print(pipeline.format_report(report, settings, title="Benchmark"))
        return 0 if report.success else 1


def cmd_runs(args, settings: Settings) -> int:
    # База в контейнере живёт в UTC - время показываем в поясе расписания.
    tz = scheduler.get_timezone(settings.schedule.timezone)
    with _warehouse() as wh:
        rows = wh.recent_runs(args.limit)
        print("Время - {}".format(settings.schedule.timezone))
        print("{:>6} {:<9} {:<11} {:<16} {:>6} {:>6} {:>6} {:>9} {:>8} {:>7}".format(
            "run", "kind", "status", "started", "total", "ok", "err", "duration", "SKU/min",
            "s/SKU"))
        for (run_id, kind, status, started, total, ok, err, duration, speed, avg,
             _delay) in rows:
            print("{:>6} {:<9} {:<11} {:<16} {:>6} {:>6} {:>6} {:>9} {:>8} {:>7}".format(
                run_id, kind, status, started.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
                total, ok, err,
                pipeline.format_duration(float(duration)) if duration is not None else "-",
                speed if speed is not None else "-", avg if avg is not None else "-"))
        observations, skus, runs = wh.history_counts()
        print("\nprice_history: {} наблюдений, {} SKU, {} прогонов".format(
            observations, skus, runs))
    return 0


def cmd_schedule(args, settings: Settings) -> int:
    # Схему готовим сразу при старте контейнера, а не в момент первого прогона.
    with _warehouse():
        pass
    return scheduler.serve(settings, once=args.once)


# ------------------------------------------------------------------ разбор --
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ozon_parser",
                                     description="Конвейер discovery -> panel -> parse")
    parser.add_argument("--config", type=Path, help="путь к config.yaml (или PIPELINE_CONFIG)")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("migrate", help="применить миграции схемы").set_defaults(func=cmd_migrate)

    discover = commands.add_parser("discover", help="найти SKU и сформировать panel")
    discover.add_argument("--category", action="append",
                          help="только эта категория из config.yaml (можно несколько раз)")
    discover.add_argument("--rebuild", action="store_true",
                          help="пересобрать panel категории заново (старая выключается)")
    discover.set_defaults(func=cmd_discover)

    panel = commands.add_parser("panel", help="состав panel")
    panel.add_argument("--export", type=Path, help="выгрузить активные SKU в CSV")
    panel.set_defaults(func=cmd_panel)

    parse = commands.add_parser("parse", help="прогон парсера с записью в PostgreSQL")
    parse.add_argument("skus", nargs="*", help="SKU через пробел (по умолчанию - panel)")
    parse.add_argument("--file", type=Path, help="файл со списком SKU вместо panel")
    parse.add_argument("--category", action="append", help="только SKU этой категории panel")
    parse.add_argument("--limit", type=int, help="обработать не больше N SKU")
    parse.add_argument("--kind", choices=["manual", "daily"], default="manual",
                       help="тип прогона в parse_runs (планировщик передаёт daily)")
    parse.add_argument("--missing-today", action="store_true",
                       help="только SKU panel, у которых за сегодня (schedule.timezone) ещё "
                            "нет успешного наблюдения - догон после блокировки")
    parse.set_defaults(func=cmd_parse)

    bench = commands.add_parser("benchmark", help="замер скорости парсера")
    bench.add_argument("--sample", type=int, help="сколько SKU (по умолчанию из config.yaml)")
    bench.add_argument("--seed", help="зерно выборки SKU (по умолчанию случайно)")
    bench.add_argument("--file", type=Path, help="мерить на SKU из файла, а не из panel")
    bench.set_defaults(func=cmd_benchmark)

    runs = commands.add_parser("runs", help="последние прогоны парсера")
    runs.add_argument("--limit", type=int, default=10)
    runs.set_defaults(func=cmd_runs)

    schedule = commands.add_parser("schedule", help="ежедневный запуск")
    schedule.add_argument("--once", action="store_true", help="выполнить прогон сейчас и выйти")
    schedule.set_defaults(func=cmd_schedule)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(args.config)
        return args.func(args, settings)
    except SettingsError as exc:
        log.error("Настройки: %s", exc)
    except DatabaseError as exc:
        log.error("PostgreSQL: %s (проверьте PG_DSN в .env и что база запущена)", exc)
    except (discovery.DiscoveryError, pipeline.ParseLockBusy, FileNotFoundError) as exc:
        log.error("%s", exc)
    except KeyboardInterrupt:
        log.warning("Прервано пользователем")
        return 130
    return 1


if __name__ == "__main__":
    sys.exit(main())
