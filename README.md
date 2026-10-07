# Ozon price panel

Конвейер данных, который каждый день собирает цены одного и того же набора
товаров Ozon и копит их историю в PostgreSQL для анализа.

```text
Discovery  ->  SKU Panel  ->  Daily Parsing  ->  PostgreSQL  ->  Historical Data
```

## Возможности

- **Discovery** — находит товары в категориях из `config.yaml` и формирует
  выборку «первые позиции выдачи + случайный хвост», воспроизводимо и с
  щадящим темпом запросов.
- **Стабильная panel** — набор SKU не перетасовывается от запуска к запуску:
  история строится по одним и тем же товарам.
- **Ежедневный сбор** — цена, цена с Ozon Картой, зачёркнутая цена, скидка,
  наличие, рейтинг и отзывы каждого SKU на момент сбора.
- **Наблюдаемость** — каждый прогон и каждая ошибка по SKU записываются в
  базу; предохранитель останавливает прогон, если Ozon начинает блокировать.
- **Аналитика** — готовые представления: цена вчера и сегодня, изменение,
  волатильность, динамика по категориям.
- **Совместимость** — исходный сценарий «список SKU → CSV / таблица для
  DataLens» (`parse_ozon.py`, Airflow DAG) работает как раньше.

## Как это работает

```text
config.yaml ─> discover ─> sku_panel ─> parse (ежедневно) ─> price_history
                 │                         │                      │
          листинг ozon.ru           Playwright + Chrome:     v_price_daily
          top + tail_random         JSON API карточки,       v_price_volatility
                                    запасной путь — HTML     v_category_daily
```

Этапы связаны только данными: discovery пишет `sku_panel`, парсер её читает
(или обычный файл со списком SKU). Подробнее — в
[docs/architecture.md](docs/architecture.md).

## Быстрый старт

Рабочая схема: **PostgreSQL — в Docker, парсинг — на хосте в Google Chrome**
(в контейнере Ozon блокирует браузер на карточках товаров).

Нужны Python 3.9+, Docker Desktop и Google Chrome. Вход в аккаунт Ozon для
сбора не нужен: вход через Gmail (`get_cookies.py`) оставлен на будущее.
Подробная инструкция — [docs/installation.md](docs/installation.md).

1. Окружение:

   ```bash
   python -m venv .venv
   ```

   ```bash
   .venv\Scripts\Activate.ps1
   ```

   ```bash
   pip install -r requirements.txt
   ```

   ```bash
   pip install -e ".[postgres]"
   ```

2. Настройки: скопируйте `.env.example` в `.env` и задайте
   `BROWSER_CHANNEL=chrome`, `POSTGRES_PASSWORD` и
   `PG_DSN=postgresql://ozon:<пароль>@127.0.0.1:5433/ozon`.

3. База:

   ```bash
   docker compose up -d
   ```

4. Panel и замер скорости:

   ```bash
   python -m ozon_parser discover
   ```

   ```bash
   python -m ozon_parser benchmark
   ```

5. Ежедневный прогон через Планировщик заданий Windows (время — из
   `config.yaml`). После прогона задача снимает резервную копию базы в
   `backups/`:

   ```bash
   powershell -ExecutionPolicy Bypass -File scripts\register_windows_task.ps1
   ```

Схема базы создаётся автоматически при первой команде.

## Основные команды

| Команда                              | Что делает |
| ------------------------------------ | ---------- |
| `python -m ozon_parser discover`     | Найти SKU и дописать panel до `panel_size` |
| `python -m ozon_parser parse`        | Прогон по panel с записью в PostgreSQL |
| `python -m ozon_parser runs`         | Последние прогоны и их статус |
| `python -m ozon_parser panel`        | Состав panel по категориям |
| `python -m ozon_parser benchmark`    | Замер скорости и расчёт дневной ёмкости |
| `python -m ozon_parser backup`       | Резервная копия базы в `backups/` |
| `python parse_ozon.py --file skus.txt` | Старый сценарий: список SKU → CSV / БД |

Все команды и флаги — [docs/cli.md](docs/cli.md).

## Текущее состояние

- Panel: 4 категории × 300 SKU (чехлы, смартфоны, кофемашины, аксессуары для
  струнных инструментов).
- Скорость: 8,6–9,2 SKU/мин, полная panel — около 2,3 ч
  ([замеры](docs/performance.md)). Быстрее не нужно: 07.10.2026 при ~14
  карточках в минуту Ozon показал капчу на 14-й минуте, поэтому карточки
  открываются не чаще раза в 6,5 с (`PAGE_INTERVAL`).
- Вход в аккаунт Ozon не нужен: с пустым `cookies.json` 07.10.2026 собрано
  989 из 989 SKU без капчи.
- Ежедневный прогон — на хосте в 08:45 МСК; компьютер должен быть включён и не
  уходить в сон.
- Полные прогоны 02–04.10.2026: 1200 из 1200 SKU за 2,2–2,8 ч. 01.10 и 05.10
  Ozon остановил парсер капчей (на 186-м и 349-м SKU); на такие дни есть
  предохранитель, повтор по недостающим SKU через 3 часа и очередь «дольше
  всех не обновлявшиеся первыми».
- Первая аналитика «день к дню»: за сутки цена изменилась у 68 из 177 SKU,
  наблюдавшихся оба дня.
- Парсинг карточек внутри контейнера пока блокируется Ozon; discovery в
  контейнере работает.

Все ограничения — [docs/limitations.md](docs/limitations.md).

## Документация

| Тема | Документ |
| ---- | -------- |
| Установка и настройка | [installation.md](docs/installation.md), [configuration.md](docs/configuration.md) |
| Ежедневная работа, мониторинг, проблемы | [operations.md](docs/operations.md) |
| Как устроен конвейер | [architecture.md](docs/architecture.md), [discovery.md](docs/discovery.md) |
| Данные и аналитика | [data-model.md](docs/data-model.md) |
| Производительность | [performance.md](docs/performance.md) |
| Старый сценарий и Airflow | [legacy.md](docs/legacy.md) |
| Разработка и тесты | [development.md](docs/development.md) |
| Что проверено вживую | [verification-log.md](docs/verification-log.md) |

Полное оглавление — [docs/README.md](docs/README.md).

## Разработка

```bash
pip install -e ".[dev,postgres]"
```

```bash
pytest
```

```bash
ruff check .
```

```bash
pyright
```

Тесты не ходят в сеть; проверки с настоящим PostgreSQL включаются переменной
`TEST_PG_DSN`. Подробнее — [docs/development.md](docs/development.md).

## Безопасность

`.env`, `credentials.json`, `token.json` (доступ к почте) и `cookies.json`
(сессия Ozon) содержат секреты, не попадают в git и в Docker-образ. Не
передавайте их вместе с проектом. Подробнее —
[docs/operations.md](docs/operations.md#секреты).

Автоматизированный сбор данных с ozon.ru — зона риска по пользовательскому
соглашению сайта; для продакшена этот вопрос стоит проговорить заранее.
