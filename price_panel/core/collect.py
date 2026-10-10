"""Цикл сбора, общий для всех маркетплейсов: адаптер -> наблюдатель -> предохранитель.

Адаптер маркетплейса (MarketplaceAdapter) отдаёт итог каждого SKU сразу
после разбора и сам отвечает за транспорт: браузер, перезапуски, темп. Ядро
передаёт итог наблюдателю (запись в базу, CSV), следит за серией отказов и
останавливает сбор, закрывая генератор адаптера. SKU, до которых очередь не
дошла, тоже отдаются наблюдателю - с причиной, а не пропадают молча.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Generator, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from price_panel.core.models import SkuOutcome


class MarketplaceAdapter(Protocol):
    def collect(self, skus: list) -> Generator[SkuOutcome, None, str | None]:
        """Итоги SKU по порядку списка, по одному сразу после разбора.

        Если адаптер не может продолжать (браузер падает раз за разом, не
        открылась сессия), он завершается раньше и возвращает причину - она
        достанется SKU, до которых не дошла очередь.
        """
        ...


class RunObserver:
    """Хуки на итог каждого SKU. Базовая версия ничего не делает."""

    def sku_done(self, sku: str, product: dict, seconds: float) -> None:
        """SKU разобран."""

    def sku_failed(
        self, sku: str, error_type: str, message: str, seconds: float, attempts: int = 0
    ) -> None:
        """SKU не разобран: товара нет, исчерпаны попытки или до него не дошли."""


class Observers(RunObserver):
    """Несколько наблюдателей как один: каждый получает каждый итог."""

    def __init__(self, *observers: RunObserver):
        self.observers = observers

    def sku_done(self, sku: str, product: dict, seconds: float) -> None:
        for observer in self.observers:
            observer.sku_done(sku, product, seconds)

    def sku_failed(
        self, sku: str, error_type: str, message: str, seconds: float, attempts: int = 0
    ) -> None:
        for observer in self.observers:
            observer.sku_failed(sku, error_type, message, seconds, attempts)


class RowBatches(RunObserver):
    """Копит записи и отдаёт их save(rows) каждые batch_size новых.

    save возвращает False, если сохранить не удалось: тогда эти записи уйдут
    со следующей партией. flush() - финальное сохранение, оно делается всегда,
    даже на пустом результате (иначе остался бы снимок прошлого запуска).
    0 в batch_size - только в конце.
    """

    def __init__(self, save: Callable[[list], bool], batch_size: int):
        self.save = save
        self.batch_size = batch_size
        self.rows: list = []
        self.saved = 0

    def sku_done(self, sku: str, product: dict, seconds: float) -> None:
        self.rows.append(product)
        if self.batch_size > 0 and len(self.rows) - self.saved >= self.batch_size:
            if self.save(self.rows):
                self.saved = len(self.rows)

    def flush(self) -> bool:
        return self.save(self.rows)


@dataclass
class Breaker:
    """Предохранитель: серия SKU подряд без данных означает блокировку.

    «Товара нет» (not_found) серию не продолжает: это ответ сайта, а не отказ.
    Непройденная антибот-проверка - самый явный признак блокировки, для неё
    порог меньше. 0 выключает порог.
    """

    max_failures: int
    max_challenges: int
    failures: int = 0
    challenges: int = 0

    def record(self, outcome: SkuOutcome) -> bool:
        """Учитывает итог SKU. True - пора остановиться."""
        if outcome.product is not None or outcome.error_type == "not_found":
            self.failures = self.challenges = 0
            return False
        self.failures += 1
        self.challenges = self.challenges + 1 if outcome.error_type == "antibot" else 0
        limits = ((self.max_failures, self.failures), (self.max_challenges, self.challenges))
        return any(0 < limit <= count for limit, count in limits)


@dataclass
class CollectResult:
    total: int
    success: int = 0
    failed: list = field(default_factory=list)
    blocked: bool = False

    @property
    def success_rate(self) -> float:
        return self.success / self.total if self.total else 0.0


def collect(
    adapter: MarketplaceAdapter,
    skus: Sequence[str],
    observer: RunObserver,
    breaker: Breaker,
    log: logging.Logger,
) -> CollectResult:
    """Обходит skus адаптером и отдаёт итог каждого SKU наблюдателю.

    :param log: лог маркетплейса - строки цикла идут туда же, что и разбор карточек.
    """
    result = CollectResult(total=len(skus))
    pending = list(skus)
    log.info("К обработке SKU: %s", len(pending))

    def report(outcome: SkuOutcome) -> None:
        # Сбой записи (база, CSV) не повод бросать очередь.
        try:
            if outcome.product is not None:
                observer.sku_done(outcome.sku, outcome.product, outcome.seconds)
            else:
                observer.sku_failed(
                    outcome.sku,
                    outcome.error_type or "unknown",
                    outcome.error_message,
                    outcome.seconds,
                    outcome.attempts,
                )
        except Exception:  # noqa: BLE001 - любой сбой наблюдателя - только в лог
            log.exception("SKU %s: не удалось записать результат", outcome.sku)

    unprocessed = ("not_processed", "сбор остановился до этого SKU")
    outcomes = adapter.collect(list(pending))
    try:
        while pending:
            try:
                outcome = next(outcomes)
            except StopIteration as stop:
                if stop.value:
                    unprocessed = ("not_processed", stop.value)
                break
            pending.remove(outcome.sku)
            if outcome.product is None:
                result.failed.append(outcome.sku)
            else:
                result.success += 1
            report(outcome)
            if breaker.record(outcome):
                result.blocked = True
                log.error(
                    "Сайт не отдаёт данные %s SKU подряд - похоже на блокировку. "
                    "Прогон остановлен, чтобы не нагружать сайт; осталось SKU: %s",
                    breaker.failures,
                    len(pending),
                )
                unprocessed = (
                    "blocked",
                    "прогон остановлен: сайт не отдавал данные {} SKU подряд".format(
                        breaker.failures
                    ),
                )
                break
    except KeyboardInterrupt:
        log.warning("Прервано пользователем - сохраняю собранное")
        unprocessed = ("interrupted", "прогон прерван до обработки SKU")
    finally:
        # Закрытие генератора закрывает и транспорт адаптера (браузер).
        outcomes.close()

    for sku in pending:
        report(SkuOutcome(sku=sku, error_type=unprocessed[0], error_message=unprocessed[1]))
    result.failed.extend(pending)

    log.info(
        "Итог: успешно %s из %s (%.0f%%), с ошибкой %s",
        result.success,
        result.total,
        result.success_rate * 100,
        len(result.failed),
    )
    if result.failed:
        log.warning("Не удалось обработать SKU: %s", ", ".join(result.failed))
    return result
