"""Командная строка конвейера: python -m price_panel <команда>.

    migrate     применить миграции схемы PostgreSQL
    discover    найти SKU в категориях из config.yaml и дописать sku_panel
    panel       состав panel по категориям; --export выгружает CSV со SKU
    parse       прогон парсера по panel (или --file / SKU аргументами)
    benchmark   замер скорости на случайных SKU из panel и расчёт ёмкости
    runs        последние прогоны парсера и объём истории
    backup      резервная копия базы (pg_dump в контейнере postgres)
    schedule    ежедневный запуск (для docker-compose); --once - один прогон
    notify      проверить связку с Telegram-ботом или узнать id чата

Старые точки входа не меняются: parse_ozon.py, get_cookies.py и
check_snapshot.py работают как раньше и не требуют ни PostgreSQL, ни config.yaml.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

from price_panel.app import discovery, pipeline, scheduler
from price_panel.app.settings import Settings, SettingsError, load_settings
from price_panel.infra import backup, config, notify
from price_panel.infra.db import DatabaseError
from price_panel.infra.logger import get_logger
from price_panel.infra.warehouse import Warehouse

log = get_logger("pipeline")


def _warehouse() -> Warehouse:
    wh = Warehouse()
    wh.migrate()
    return wh


# ---------------------------------------------------------------- команды --
def cmd_migrate(args, settings: Settings) -> int:
    with Warehouse() as wh:
        applied = wh.migrate()
    print(
        "Миграции применены: {}".format(", ".join(applied))
        if applied
        else "Схема актуальна, новых миграций нет"
    )
    return 0


def print_panel(wh: Warehouse) -> None:
    rows = wh.panel_summary()
    if not rows:
        print("Panel пуста: запустите python -m price_panel discover")
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
            depth = (
                " (выдача кончается на странице {})".format(result.listing_depth)
                if result.listing_depth
                else ""
            )
            print("  requests:   {}{}".format(result.pages_requested, depth))
            print(
                "  discovered: {} уникальных SKU ({} уже в panel)".format(
                    result.discovered, result.already_in_panel
                )
            )
            print(
                "  selected:   {} (top {} + tail_random {})".format(
                    result.selected, result.selected_top, result.selected_tail
                )
            )
            if result.note:
                print("  note:       {}".format(result.note))

        unfinished = [r for r in results if r.status in ("failed", "blocked", "stopped")]
        print("\nTotal selected SKU: {}".format(sum(r.selected for r in results)))
        exported = discovery.export_panel(wh, settings)
        if unfinished:
            print(
                "Panel saved, но не собраны до panel_size: {}. Запустите discover "
                "повторно - готовые категории он пропустит.".format(
                    ", ".join(r.category.name for r in unfinished)
                )
            )
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
    return scheduler.day_start(dt.datetime.now(dt.UTC), tz)


def _skus_for_parse(args, wh: Warehouse, settings: Settings) -> tuple:
    """(список SKU, откуда он взят)."""
    from price_panel.marketplaces.ozon.parse import read_skus_file, select_range

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
            log.error("Нет SKU для парсинга (%s). Сначала: python -m price_panel discover", source)
            return 1
        report = pipeline.run_parse(wh, skus, settings, kind=args.kind, sku_source=source)
        print(pipeline.format_report(report))
        return report.exit_code


def cmd_benchmark(args, settings: Settings) -> int:
    size = args.sample or settings.benchmark.sample_size
    with _warehouse() as wh:
        if args.file:
            from price_panel.marketplaces.ozon.parse import read_skus_file

            skus, source = read_skus_file(args.file)[:size], "file"
        else:
            skus, source = pipeline.sample_panel(wh, size, args.seed), "panel"
        if not skus:
            log.error("Panel пуста - замерять не на чем. Сначала: python -m price_panel discover")
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
        print(
            "{:>6} {:<9} {:<11} {:<16} {:>6} {:>6} {:>6} {:>9} {:>8} {:>7}".format(
                "run",
                "kind",
                "status",
                "started",
                "total",
                "ok",
                "err",
                "duration",
                "SKU/min",
                "s/SKU",
            )
        )
        for run_id, kind, status, started, total, ok, err, duration, speed, avg, _delay in rows:
            print(
                "{:>6} {:<9} {:<11} {:<16} {:>6} {:>6} {:>6} {:>9} {:>8} {:>7}".format(
                    run_id,
                    kind,
                    status,
                    started.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
                    total,
                    ok,
                    err,
                    pipeline.format_duration(float(duration)) if duration is not None else "-",
                    speed if speed is not None else "-",
                    avg if avg is not None else "-",
                )
            )
        observations, skus, runs = wh.history_counts()
        print(
            "\nprice_history: {} наблюдений, {} SKU, {} прогонов".format(observations, skus, runs)
        )
    return 0


def cmd_backup(args, settings: Settings) -> int:
    # Базу не трогаем через PG_DSN: pg_dump работает внутри контейнера.
    try:
        path = backup.create_backup(settings.backup.directory, settings.backup.keep)
    except backup.BackupError as exc:
        log.error("Резервная копия не создана: %s", exc)
        return 1
    print("Резервная копия: {} ({:.1f} МБ)".format(path, path.stat().st_size / 2**20))
    return 0


def cmd_schedule(args, settings: Settings) -> int:
    # Схему готовим сразу при старте контейнера, а не в момент первого прогона.
    with _warehouse():
        pass
    return scheduler.serve(settings, once=args.once)


def cmd_notify(args, settings: Settings) -> int:
    # База не нужна: команда проверяет только настройки Telegram в .env.
    if not config.TELEGRAM_BOT_TOKEN:
        print(
            "TELEGRAM_BOT_TOKEN не задан в .env: токен выдаёт @BotFather "
            "(docs/operations.md, «Оповещения»)"
        )
        return 1
    if not config.TELEGRAM_CHAT_ID:
        chats = notify.find_chats()
        if chats is None:
            print("Telegram не ответил - причина в logs/notify.log")
            return 1
        if not chats:
            print("Боту ещё никто не писал: отправьте ему любое сообщение и повторите команду")
            return 1
        print("Чаты, которые писали боту, - нужный id впишите в TELEGRAM_CHAT_ID в .env:")
        for chat_id, name in chats:
            print("  {:<16} {}".format(chat_id, name))
        return 0
    # Задержанные из-за сети сообщения уходят перед проверочным; само оно в
    # очередь не встаёт - результат виден сразу.
    queued = len(notify.load_outbox())
    if not notify.send_telegram("✅ Price panel: оповещения настроены, бот на связи", queue=False):
        print("Сообщение не отправлено - причина в logs/notify.log")
        left = len(notify.load_outbox())
        if left:
            print("Задержанных сообщений в очереди: {}".format(left))
        return 1
    print("Проверочное сообщение отправлено в Telegram")
    if queued:
        print("Перед ним отправлены задержанные сообщения из очереди: {}".format(queued))
    return 0


# ------------------------------------------------------------------ разбор --
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m price_panel", description="Конвейер discovery -> panel -> parse"
    )
    parser.add_argument("--config", type=Path, help="путь к config.yaml (или PIPELINE_CONFIG)")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("migrate", help="применить миграции схемы").set_defaults(func=cmd_migrate)

    discover = commands.add_parser("discover", help="найти SKU и сформировать panel")
    discover.add_argument(
        "--category",
        action="append",
        help="только эта категория из config.yaml (можно несколько раз)",
    )
    discover.add_argument(
        "--rebuild",
        action="store_true",
        help="пересобрать panel категории заново (старая выключается)",
    )
    discover.set_defaults(func=cmd_discover)

    panel = commands.add_parser("panel", help="состав panel")
    panel.add_argument("--export", type=Path, help="выгрузить активные SKU в CSV")
    panel.set_defaults(func=cmd_panel)

    parse = commands.add_parser("parse", help="прогон парсера с записью в PostgreSQL")
    parse.add_argument("skus", nargs="*", help="SKU через пробел (по умолчанию - panel)")
    parse.add_argument("--file", type=Path, help="файл со списком SKU вместо panel")
    parse.add_argument("--category", action="append", help="только SKU этой категории panel")
    parse.add_argument("--limit", type=int, help="обработать не больше N SKU")
    parse.add_argument(
        "--kind",
        choices=["manual", "daily"],
        default="manual",
        help="тип прогона в parse_runs (планировщик передаёт daily)",
    )
    parse.add_argument(
        "--missing-today",
        action="store_true",
        help="только SKU panel, у которых за сегодня (schedule.timezone) ещё "
        "нет успешного наблюдения - догон после блокировки",
    )
    parse.set_defaults(func=cmd_parse)

    bench = commands.add_parser("benchmark", help="замер скорости парсера")
    bench.add_argument("--sample", type=int, help="сколько SKU (по умолчанию из config.yaml)")
    bench.add_argument("--seed", help="зерно выборки SKU (по умолчанию случайно)")
    bench.add_argument("--file", type=Path, help="мерить на SKU из файла, а не из panel")
    bench.set_defaults(func=cmd_benchmark)

    runs = commands.add_parser("runs", help="последние прогоны парсера")
    runs.add_argument("--limit", type=int, default=10)
    runs.set_defaults(func=cmd_runs)

    commands.add_parser("backup", help="резервная копия базы (backup в config.yaml)").set_defaults(
        func=cmd_backup
    )

    schedule = commands.add_parser("schedule", help="ежедневный запуск")
    schedule.add_argument("--once", action="store_true", help="выполнить прогон сейчас и выйти")
    schedule.set_defaults(func=cmd_schedule)

    commands.add_parser(
        "notify", help="проверить Telegram-бота: тестовое сообщение или id чата"
    ).set_defaults(func=cmd_notify)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(args.config)
        return args.func(args, settings)
    except SettingsError as exc:
        problem = "Настройки: {}".format(exc)
    except DatabaseError as exc:
        problem = "PostgreSQL: {} (проверьте PG_DSN в .env и что база запущена)".format(exc)
    except (discovery.DiscoveryError, pipeline.ParseLockBusy, FileNotFoundError) as exc:
        problem = str(exc)
    except KeyboardInterrupt:
        log.warning("Прервано пользователем")
        return 130
    log.error("%s", problem)
    if args.command == "schedule" and args.once:
        # Ежедневная задача Windows упала до прогона (не запущен Docker, сломан
        # config.yaml): иначе об этом узнали бы только из лога. Цикл контейнера
        # (без --once) не пишет: Docker перезапускает его, и сообщение уходило
        # бы на каждом перезапуске.
        notify.report_failure(problem)
    return 1


if __name__ == "__main__":
    sys.exit(main())
