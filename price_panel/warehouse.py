"""Хранилище конвейера в PostgreSQL: миграции, panel, запуски, история цен.

Одно соединение живёт весь прогон, а каждый SKU пишется своей короткой
транзакцией: упавшая запись одного товара не откатывает остальные, а
собранное к моменту сбоя уже лежит в базе. Если соединение оборвалось
(перезапуск PostgreSQL посреди ночного прогона), операция повторяется один
раз на новом соединении.

Схема задаётся файлами price_panel/migrations/NNNN_*.sql. Они применяются по
порядку номеров, каждая ровно один раз, - учёт ведётся в schema_migrations.
Руками ничего запускать не нужно: миграции проверяются при старте каждой
команды python -m price_panel.
"""

from __future__ import annotations

import contextlib
import datetime as dt
from collections.abc import Generator, Iterable
from pathlib import Path
from typing import Any, Callable, Optional

from . import config, db
from .logger import get_logger
from .sampling import PanelPick

log = get_logger("warehouse")

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# Ключи advisory lock: миграции не должны идти из двух процессов разом, а два
# прогона парсера - поднимать два браузера и писать в один день дважды.
MIGRATION_LOCK = 72_001
PARSE_LOCK = 72_002

MIGRATIONS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

PANEL_UPSERT = """
INSERT INTO sku_panel (
    sku, category, source, source_url, position, page, sampling_group, discovery_run_id
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (sku) DO UPDATE SET
    category = EXCLUDED.category,
    source = EXCLUDED.source,
    source_url = EXCLUDED.source_url,
    position = EXCLUDED.position,
    page = EXCLUDED.page,
    sampling_group = EXCLUDED.sampling_group,
    discovery_run_id = EXCLUDED.discovery_run_id,
    is_active = TRUE,
    deactivated_at = NULL,
    last_seen_at = now(),
    updated_at = now()
WHERE NOT sku_panel.is_active
"""

# COALESCE: карточка из запасного пути (JSON в HTML) не знает описания и
# полных характеристик - пустые значения не должны затирать известные.
PRODUCT_UPSERT = """
INSERT INTO products (
    sku, title, cover_image, color, material, art_set, has_rich_content,
    photos_seller, videos_seller, first_seen_at, last_seen_at, last_run_id
) VALUES (
    %(sku)s, %(title)s, %(cover_image)s, %(color)s, %(material)s, %(art_set)s,
    %(has_rich_content)s, %(photos_seller)s, %(videos_seller)s,
    %(collected_at)s, %(collected_at)s, %(run_id)s
)
ON CONFLICT (sku) DO UPDATE SET
    title = COALESCE(EXCLUDED.title, products.title),
    cover_image = COALESCE(EXCLUDED.cover_image, products.cover_image),
    color = COALESCE(EXCLUDED.color, products.color),
    material = COALESCE(EXCLUDED.material, products.material),
    art_set = COALESCE(EXCLUDED.art_set, products.art_set),
    has_rich_content = COALESCE(EXCLUDED.has_rich_content, products.has_rich_content),
    photos_seller = COALESCE(EXCLUDED.photos_seller, products.photos_seller),
    videos_seller = COALESCE(EXCLUDED.videos_seller, products.videos_seller),
    last_seen_at = EXCLUDED.last_seen_at,
    last_run_id = EXCLUDED.last_run_id
"""

# История не перезаписывается между прогонами: ключ - (run_id, sku). Повтор
# записи в рамках одного прогона обновляет ту же строку, а не плодит дубль.
PRICE_UPSERT = """
INSERT INTO price_history (
    run_id, sku, collected_at, price, card_price, old_price, discount_pct,
    is_available, rating, reviews_total, source
) VALUES (
    %(run_id)s, %(sku)s, %(collected_at)s, %(price)s, %(card_price)s, %(old_price)s,
    %(discount_pct)s, %(is_available)s, %(rating)s, %(reviews_total)s, %(source)s
)
ON CONFLICT (run_id, sku) DO UPDATE SET
    collected_at = EXCLUDED.collected_at,
    price = EXCLUDED.price,
    card_price = EXCLUDED.card_price,
    old_price = EXCLUDED.old_price,
    discount_pct = EXCLUDED.discount_pct,
    is_available = EXCLUDED.is_available,
    rating = EXCLUDED.rating,
    reviews_total = EXCLUDED.reviews_total,
    source = EXCLUDED.source
"""

# Очередь парсинга: никогда не пробованные SKU, затем самые давние попытки.
# Ошибки not_processed / interrupted / blocked - не попытка: до SKU не дошли.
PANEL_QUEUE = """
SELECT sp.sku
FROM sku_panel sp
LEFT JOIN (
    SELECT sku, max(collected_at) AS at FROM price_history GROUP BY sku
) ok ON ok.sku = sp.sku
LEFT JOIN (
    SELECT sku, max(occurred_at) AS at FROM parse_errors
    WHERE error_type NOT IN ('not_processed', 'interrupted', 'blocked')
    GROUP BY sku
) err ON err.sku = sp.sku
{where}
ORDER BY greatest(ok.at, err.at) ASC NULLS FIRST, md5(sp.sku)
"""

# Брошенные прогоны: итог восстанавливается по записям каждого SKU, окончание -
# по последней из них (если записей нет - момент обнаружения).
STALE_RUNS_UPDATE = """
UPDATE parse_runs r SET
    status = 'interrupted',
    success_count = s.ok,
    error_count = s.err,
    processed_count = s.ok + s.err,
    finished_at = coalesce(r.finished_at, s.last_activity, now()),
    duration_seconds = round(extract(epoch FROM coalesce(s.last_activity, now())
                                     - r.started_at)::numeric, 1)
FROM (
    SELECT p.run_id,
           (SELECT count(*) FROM price_history ph WHERE ph.run_id = p.run_id) AS ok,
           (SELECT count(*) FROM parse_errors pe WHERE pe.run_id = p.run_id) AS err,
           greatest((SELECT max(collected_at) FROM price_history ph WHERE ph.run_id = p.run_id),
                    (SELECT max(occurred_at) FROM parse_errors pe WHERE pe.run_id = p.run_id))
               AS last_activity
    FROM parse_runs p
    WHERE p.status = 'running'
) s
WHERE r.run_id = s.run_id
"""

PRODUCT_FIELDS = ("sku", "title", "cover_image", "color", "material", "art_set",
                  "has_rich_content", "photos_seller", "videos_seller")
PRICE_FIELDS = ("sku", "price", "card_price", "old_price", "discount_pct", "is_available",
                "rating", "reviews_total", "source")
# Поля карточки, которые берутся из второй части (описание, полные характеристики).
DETAIL_FIELDS = ("has_rich_content", "art_set", "color", "material")


def migration_files(directory: Path = MIGRATIONS_DIR) -> list:
    """Файлы миграций в порядке применения: 0001_*.sql, 0002_*.sql, ..."""
    return sorted(directory.glob("[0-9][0-9][0-9][0-9]_*.sql"))


def product_params(run_id: int, product: dict, collected_at: dt.datetime) -> dict:
    """Параметры для PRODUCT_UPSERT и PRICE_UPSERT из записи парсера."""
    params = {name: product.get(name) for name in PRODUCT_FIELDS + PRICE_FIELDS}
    params["sku"] = str(product["sku"])
    params["run_id"] = run_id
    params["collected_at"] = collected_at
    # Описание и полные характеристики известны, только если пришла вторая
    # часть карточки (details). Без неё значения неполны: False значил бы «нет
    # rich-контента», хотя на деле «не знаем». None не затирает в products
    # известное (COALESCE) - медленные атрибуты обновляются, когда приходит
    # описание (pipeline.details_schedule).
    if not product.get("details"):
        for name in DETAIL_FIELDS:
            params[name] = None
    return params


class Warehouse:
    """Соединение с PostgreSQL и операции конвейера."""

    def __init__(self, dsn: str = ""):
        self.dsn = dsn or config.PG_DSN
        if not self.dsn:
            raise db.DatabaseError("Не задан PG_DSN (см. .env)")
        self._psycopg2 = db._import_psycopg2()
        self._connection = None

    # ------------------------------------------------------------ соединение --
    def _connect(self):
        if self._connection is None or self._connection.closed:
            try:
                self._connection = self._psycopg2.connect(self.dsn)
            except self._psycopg2.Error as exc:
                raise db.DatabaseError("Ошибка PostgreSQL: {}".format(exc)) from exc
        return self._connection

    def close(self) -> None:
        if self._connection is not None and not self._connection.closed:
            with contextlib.suppress(Exception):
                self._connection.close()
        self._connection = None

    def __enter__(self) -> Warehouse:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    @contextlib.contextmanager
    def cursor(self) -> Generator:
        """Курсор в транзакции: commit при успехе, rollback при ошибке."""
        connection = self._connect()
        try:
            with connection:
                with connection.cursor() as cursor:
                    yield cursor
        except self._psycopg2.Error as exc:
            raise db.DatabaseError("Ошибка PostgreSQL: {}".format(exc)) from exc

    def run(self, operation: Callable[[Any], Any]) -> Any:
        """Выполняет operation(cursor) в транзакции; при обрыве связи - ещё раз."""
        lost = (self._psycopg2.OperationalError, self._psycopg2.InterfaceError)
        was_connected = self._connection is not None and not self._connection.closed
        try:
            with self.cursor() as cursor:
                return operation(cursor)
        except db.DatabaseError as exc:
            # Повтор - только если соединение было и оборвалось. Ошибку первого
            # подключения (неверный пароль, база не запущена) повтор не лечит.
            if not was_connected or not isinstance(exc.__cause__, lost):
                raise
            log.warning("Соединение с PostgreSQL потеряно (%s) - переподключаюсь", exc)
            self.close()
            with self.cursor() as cursor:
                return operation(cursor)

    # --------------------------------------------------------------- миграции --
    def migrate(self) -> list:
        """Применяет недостающие миграции. Возвращает список применённых версий."""
        files = migration_files()

        def apply(cursor) -> list:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK,))
            cursor.execute(MIGRATIONS_TABLE_DDL)
            cursor.execute("SELECT version FROM schema_migrations")
            done = {row[0] for row in cursor.fetchall()}
            applied = []
            for path in files:
                if path.stem in done:
                    continue
                log.info("Применяю миграцию %s", path.name)
                cursor.execute(path.read_text(encoding="utf-8"))
                cursor.execute("INSERT INTO schema_migrations (version) VALUES (%s)",
                               (path.stem,))
                applied.append(path.stem)
            return applied

        # Все недостающие миграции - одной транзакцией: схема не остаётся
        # применённой наполовину.
        applied = self.run(apply)
        if applied:
            log.info("Схема обновлена: %s", ", ".join(applied))
        return applied

    # ------------------------------------------------------------------ panel --
    def panel_counts(self, category: str) -> dict:
        """Сколько активных SKU категории в каждой группе выборки."""
        def query(cursor) -> dict:
            cursor.execute(
                "SELECT sampling_group, count(*) FROM sku_panel "
                "WHERE is_active AND category = %s GROUP BY sampling_group", (category,))
            return {group: count for group, count in cursor.fetchall()}
        return self.run(query)

    def active_panel_skus(self) -> set:
        def query(cursor) -> set:
            cursor.execute("SELECT sku FROM sku_panel WHERE is_active")
            return {row[0] for row in cursor.fetchall()}
        return self.run(query)

    def panel_skus(self, categories: Optional[Iterable[str]] = None,
                   limit: Optional[int] = None,
                   missing_since: Optional[dt.datetime] = None) -> list:
        """Активные SKU panel для парсинга: дольше всех не обновлявшиеся - первыми.

        Сначала SKU, которые ещё ни разу не пытались разобрать, затем - по
        давности последней попытки (успешной или с ошибкой разбора; SKU, до
        которых прогон просто не дошёл, попыткой не считаются). Если прогоны
        обрываются на середине - 01.10.2026 Ozon заблокировал парсер на 186-м
        SKU из 1200, - за несколько дней всё равно обходится вся panel, а не
        одни и те же первые сотни товаров.

        При равенстве порядок - md5(sku): стабильный и перемешанный между
        категориями, так что недобор распределяется по ним равномерно.

        missing_since - только SKU без успешного наблюдения с этого момента:
        повтор после блокировки добирает недостающее, а не обходит panel заново.
        """
        where, params = "WHERE sp.is_active", []
        names = list(categories or [])
        if names:
            where += " AND sp.category = ANY(%s)"
            params.append(names)
        if missing_since is not None:
            where += " AND (ok.at IS NULL OR ok.at < %s)"
            params.append(missing_since)
        sql = PANEL_QUEUE.format(where=where)
        if limit is not None:
            sql += " LIMIT %s"
            params.append(limit)

        def query(cursor) -> list:
            cursor.execute(sql, params)
            return [row[0] for row in cursor.fetchall()]
        return self.run(query)

    def add_to_panel(self, picks: Iterable[PanelPick], category: str, source: str,
                     discovery_run_id: Optional[int]) -> int:
        """Добавляет отобранные SKU. Возвращает число новых или возвращённых в panel.

        Активные строки не трогаются (WHERE NOT is_active в upsert): повторный
        discovery не перетасовывает panel.
        """
        rows = [(p.candidate.sku, category, source, p.candidate.url or None,
                 p.candidate.position, p.candidate.page, p.group, discovery_run_id)
                for p in picks]

        def insert(cursor) -> int:
            added = 0
            for row in rows:
                cursor.execute(PANEL_UPSERT, row)
                added += cursor.rowcount
            return added
        return self.run(insert)

    def touch_seen(self, skus: Iterable[str]) -> int:
        """Отмечает, что SKU из panel снова встретились в выдаче."""
        values = list(skus)
        if not values:
            return 0

        def update(cursor) -> int:
            cursor.execute("UPDATE sku_panel SET last_seen_at = now() WHERE sku = ANY(%s)",
                           (values,))
            return cursor.rowcount
        return self.run(update)

    def deactivate_category(self, category: str) -> int:
        """Выключает всю активную panel категории (для discover --rebuild)."""
        def update(cursor) -> int:
            cursor.execute(
                "UPDATE sku_panel SET is_active = FALSE, deactivated_at = now(), "
                "updated_at = now() WHERE is_active AND category = %s", (category,))
            return cursor.rowcount
        return self.run(update)

    def panel_summary(self) -> list:
        """Строки (category, sampling_group, active, inactive) для отчёта."""
        def query(cursor) -> list:
            cursor.execute(
                "SELECT category, sampling_group, active_sku, inactive_sku "
                "FROM v_panel_summary ORDER BY category, sampling_group")
            return cursor.fetchall()
        return self.run(query)

    def export_panel_csv(self, path: Path) -> int:
        """Выгружает активную panel одним столбцом sku - формат входа parse_ozon.py."""
        skus = self.panel_skus()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(path.name + ".tmp")
        tmp_path.write_text("sku\n" + "".join(sku + "\n" for sku in skus), encoding="utf-8")
        tmp_path.replace(path)
        return len(skus)

    # -------------------------------------------------------------- discovery --
    def start_discovery_run(self, category: str, source: str, seed: str) -> int:
        def insert(cursor) -> int:
            cursor.execute(
                "INSERT INTO discovery_runs (category, source, seed) VALUES (%s, %s, %s) "
                "RETURNING id", (category, source, seed))
            return cursor.fetchone()[0]
        return self.run(insert)

    def finish_discovery_run(self, run_id: int, status: str, pages_requested: int = 0,
                             discovered: int = 0, selected_top: int = 0,
                             selected_tail: int = 0, error_message: Optional[str] = None) -> None:
        def update(cursor) -> None:
            cursor.execute(
                "UPDATE discovery_runs SET finished_at = now(), status = %s, "
                "pages_requested = %s, discovered = %s, selected_top = %s, "
                "selected_tail = %s, error_message = %s WHERE id = %s",
                (status, pages_requested, discovered, selected_top, selected_tail,
                 error_message, run_id))
        self.run(update)

    # ------------------------------------------------------------ парсинг ------
    def try_parse_lock(self) -> bool:
        """Блокировка «идёт прогон парсера» на время жизни соединения."""
        def query(cursor) -> bool:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", (PARSE_LOCK,))
            return bool(cursor.fetchone()[0])
        return self.run(query)

    def close_stale_runs(self) -> int:
        """Помечает прогоны, брошенные упавшим процессом, как прерванные.

        Вызывается только под PARSE_LOCK: раз блокировка наша, живых прогонов
        кроме текущего нет, и всё, что осталось в статусе running, - брошено.

        Брошенный прогон не успел записать итог (так бывает, когда компьютер
        выключили посреди прогона), поэтому счётчики и время окончания
        восстанавливаются по строкам, которые он успел записать по каждому SKU.
        """
        def update(cursor) -> int:
            cursor.execute(STALE_RUNS_UPDATE)
            return cursor.rowcount
        return self.run(update)

    def start_parse_run(self, kind: str, sku_source: str, total_sku: int,
                        request_delay: float) -> int:
        def insert(cursor) -> int:
            cursor.execute(
                "INSERT INTO parse_runs (kind, sku_source, total_sku, request_delay) "
                "VALUES (%s, %s, %s, %s) RETURNING run_id",
                (kind, sku_source, total_sku, request_delay))
            return cursor.fetchone()[0]
        return self.run(insert)

    def finish_parse_run(self, run_id: int, status: str, processed: int, success: int,
                         errors: int, duration_seconds: float,
                         sku_per_minute: Optional[float],
                         avg_sku_seconds: Optional[float]) -> None:
        def update(cursor) -> None:
            cursor.execute(
                "UPDATE parse_runs SET status = %s, finished_at = now(), "
                "processed_count = %s, success_count = %s, error_count = %s, "
                "duration_seconds = %s, sku_per_minute = %s, avg_sku_seconds = %s "
                "WHERE run_id = %s",
                (status, processed, success, errors, round(duration_seconds, 1),
                 sku_per_minute, avg_sku_seconds, run_id))
        self.run(update)

    def record_product(self, run_id: int, product: dict,
                       collected_at: Optional[dt.datetime] = None) -> None:
        """Сохраняет карточку и наблюдение цены - одной транзакцией."""
        params = product_params(run_id, product,
                                collected_at or dt.datetime.now(dt.timezone.utc))

        def write(cursor) -> None:
            cursor.execute(PRODUCT_UPSERT, params)
            cursor.execute(PRICE_UPSERT, params)
        self.run(write)

    def skus_without_details(self, skus: Iterable[str]) -> set:
        """SKU, у которых ещё нет описания: карточки нет в products или вторая
        часть (has_rich_content) ни разу не приходила."""
        values = list(skus)
        if not values:
            return set()

        def query(cursor) -> set:
            cursor.execute(
                "SELECT s.sku FROM unnest(%s::text[]) AS s(sku) "
                "LEFT JOIN products p ON p.sku = s.sku "
                "WHERE p.sku IS NULL OR p.has_rich_content IS NULL", (values,))
            return {row[0] for row in cursor.fetchall()}
        return self.run(query)

    def record_error(self, run_id: int, sku: str, error_type: str, message: str,
                     attempts: Optional[int] = None) -> None:
        def insert(cursor) -> None:
            cursor.execute(
                "INSERT INTO parse_errors (run_id, sku, error_type, error_message, attempts) "
                "VALUES (%s, %s, %s, %s, %s)",
                (run_id, sku, error_type, (message or "")[:2000], attempts))
        self.run(insert)

    def error_counts(self, run_id: int) -> list:
        """Ошибки прогона по типам: [(error_type, число)], самые частые первыми."""
        def query(cursor) -> list:
            cursor.execute(
                "SELECT error_type, count(*) FROM parse_errors WHERE run_id = %s "
                "GROUP BY error_type ORDER BY count(*) DESC, error_type", (run_id,))
            return [tuple(row) for row in cursor.fetchall()]
        return self.run(query)

    def recent_runs(self, limit: int = 10) -> list:
        def query(cursor) -> list:
            cursor.execute(
                "SELECT run_id, kind, status, started_at, total_sku, success_count, "
                "error_count, duration_seconds, sku_per_minute, avg_sku_seconds, request_delay "
                "FROM parse_runs ORDER BY run_id DESC LIMIT %s", (limit,))
            return cursor.fetchall()
        return self.run(query)

    def history_counts(self) -> tuple:
        """(строк в price_history, различных SKU, прогонов с данными)."""
        def query(cursor) -> tuple:
            cursor.execute(
                "SELECT count(*), count(DISTINCT sku), count(DISTINCT run_id) FROM price_history")
            return tuple(cursor.fetchone())
        return self.run(query)
