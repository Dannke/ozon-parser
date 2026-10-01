# Модель данных

Что лежит в PostgreSQL, как схема обновляется и как отвечать на аналитические
вопросы. Таблица `ozon_products` старого сценария описана в
[legacy.md](legacy.md).

- [Миграции](#миграции)
- [Таблицы](#таблицы)
- [Решения по схеме](#решения-по-схеме)
- [Аналитические представления](#аналитические-представления)
- [Примеры запросов](#примеры-запросов)
- [Поля карточки](#поля-карточки)

## Миграции

Схема задаётся файлами `ozon_parser/migrations/NNNN_*.sql`. Они применяются по
порядку номеров, каждый ровно один раз (учёт — в `schema_migrations`), под
advisory lock и одной транзакцией. Любая команда `python -m ozon_parser`
сначала проверяет миграции, поэтому свежая база готова после первого запуска.
Явно: `python -m ozon_parser migrate`.

| Миграция                          | Что делает                                            |
| --------------------------------- | ----------------------------------------------------- |
| `0001_pipeline_schema`            | Таблицы конвейера                                     |
| `0002_analytics_views`            | Аналитические представления                           |
| `0003_parse_run_blocked_status`   | Статус прогона `blocked` (сработал предохранитель)    |

Как добавить миграцию — в [development.md](development.md#миграции).

## Таблицы

| Таблица          | Что хранит                                                              |
| ---------------- | ----------------------------------------------------------------------- |
| `sku_panel`      | Panel: `sku` (уникален), `category`, `source`, `source_url`, `position`, `page`, `sampling_group` (`top` / `tail_random`), `is_active`, `first_seen_at` / `last_seen_at` (когда discovery видел товар в выдаче), `created_at`, `updated_at`, `deactivated_at` |
| `discovery_runs` | Запуски discovery: категория, источник, seed, запросов, найдено, отобрано, статус и причина неудачи |
| `parse_runs`     | Запуски парсера: `kind` (`daily` / `manual` / `benchmark`), `status` (`running` / `success` / `partial` / `failed` / `interrupted` / `blocked`), `started_at`, `finished_at`, `total_sku`, `processed_count`, `success_count`, `error_count`, `duration_seconds`, `sku_per_minute`, `avg_sku_seconds`, `request_delay` |
| `products`       | Последние атрибуты карточки: название, картинка, цвет, материал, комплектация, фото/видео, rich-контент |
| `price_history`  | Наблюдение на момент сбора: `collected_at`, `price`, `card_price` (с Ozon Картой), `old_price` (зачёркнутая), `discount_pct`, `is_available`, `rating`, `reviews_total`, `source`, `run_id` |
| `parse_errors`   | Ошибка по SKU: `run_id`, `sku`, `occurred_at`, `error_type`, `error_message`, `attempts` |

Типы ошибок в `parse_errors.error_type`:

| Тип             | Значение                                                          |
| --------------- | ----------------------------------------------------------------- |
| `not_found`     | Карточки нет (HTTP 404), повторов не было                          |
| `fetch_error`   | Данные не получены ни из API, ни из HTML после всех попыток        |
| `timeout`       | Таймаут загрузки страницы                                         |
| `browser_error` | Прочая ошибка браузера                                            |
| `storage_error` | Карточка разобрана, но не записалась в PostgreSQL                  |
| `not_processed` | До SKU не дошла очередь: браузер падал, не открылась сессия        |
| `interrupted`   | Прогон прерван (Ctrl+C) до этого SKU                               |
| `blocked`       | Прогон остановлен предохранителем до этого SKU                     |

## Решения по схеме

- «медленные» атрибуты карточки (название, характеристики) лежат в
  `products` один раз и обновляются. Меняющиеся каждый день (цена, наличие,
  рейтинг, отзывы) — в `price_history`, строка на каждый успешный сбор.
  Данные не дублируются;
- `collected_at` — момент разбора карточки, а не дата запуска и не имя файла;
- история не перезаписывается: ключ `(run_id, sku)`. Новый прогон — новые
  строки. Повтор записи внутри того же прогона обновляет ту же строку;
- карточка, собранная запасным путём (JSON в HTML), не затирает известные
  характеристики пустыми (`COALESCE`);
- два прогона парсера одновременно невозможны (`pg_try_advisory_lock`), а
  прогон, брошенный упавшим процессом (например, компьютер выключили),
  помечается `interrupted` при старте следующего. Его счётчики и время
  окончания восстанавливаются по уже записанным строкам `price_history` и
  `parse_errors`;
- таблица `ozon_products` старого сценария не затрагивается.

## Аналитические представления

| Представление        | Вопрос, на который отвечает                                        |
| -------------------- | ------------------------------------------------------------------ |
| `v_price_daily`      | Цена за день (последнее наблюдение по Москве), вчерашняя цена, изменение в ₽ и % |
| `v_price_latest`     | Текущая цена, скидка и наличие по каждому SKU                       |
| `v_price_volatility` | Какие товары часто меняют цену: число смен, размах цены            |
| `v_category_daily`   | Как меняются цены внутри категории: средняя и медианная цена, доля SKU с изменением цены, средняя скидка, доля в наличии |
| `v_panel_summary`    | Сколько SKU в каждой категории и группе выборки                     |
| `v_parse_runs`       | Запуски парсера с долей успеха и скоростью                          |

День наблюдения считается по Москве. Если прогон перейдёт за полночь, его
наблюдения разделятся на два дня — поэтому время запуска выбрано с запасом до
полуночи (см. [operations.md](operations.md#ежедневный-запуск)).

## Примеры запросов

Какая цена была вчера и какая сегодня, насколько изменилась:

```sql
SELECT sku, category, title, prev_date, prev_price, observed_date, price,
       price_change, price_change_pct
FROM v_price_daily
WHERE observed_date = current_date AND price_change <> 0
ORDER BY abs(price_change_pct) DESC;
```

Товары, которые чаще всего меняют цену:

```sql
SELECT sku, category, title, days_observed, price_changes, min_price, max_price
FROM v_price_volatility
ORDER BY price_changes DESC, price_range_pct DESC
LIMIT 20;
```

Динамика цен внутри категорий:

```sql
SELECT category, observed_date, sku_observed, median_price,
       sku_price_changed, avg_price_change_pct, avg_discount_pct
FROM v_category_daily
ORDER BY category, observed_date;
```

## Поля карточки

Двенадцать полей задания — общие для старого сценария (CSV, `ozon_products`)
и конвейера (`products`, `price_history`):

| Поле               | Описание                                     | Источник                                             |
| ------------------ | -------------------------------------------- | ---------------------------------------------------- |
| `sku`              | Артикул                                      | входной список                                       |
| `title`            | Название                                     | виджет `webProductHeading`, запасной — SEO / JSON-LD |
| `price`            | Цена, число                                  | `webPrice` / `webSale`, запасной — JSON-LD           |
| `rating`           | Рейтинг, число                               | `webReviewProductScore`                              |
| `reviews_total`    | Количество отзывов                           | `webReviewProductScore`                              |
| `cover_image`      | Главное изображение                          | `webGallery.coverImage`                              |
| `photos_seller`    | Количество фото в галерее                    | `webGallery.images` (уникальные)                     |
| `videos_seller`    | Количество видео                             | `webGallery.videos`                                  |
| `color`            | Цвет                                         | характеристики, запасной — `webAspects`              |
| `material`         | Материал                                     | характеристики                                       |
| `art_set`          | Артикул производителя / комплектация         | характеристики                                       |
| `has_rich_content` | Есть ли в описании картинки, таблицы, списки | `webDescription`                                     |

Поля, которых у товара нет, остаются пустыми, и это не ошибка парсинга.
Характеристики ищутся по названию с приоритетом: полное совпадение, затем
совпадение начала, затем вхождение. Известные ловушки исключены: «Количество
цветов» не считается цветом, «Состав комплекта» — материалом, а характеристика
«Артикул» у Ozon равна самому SKU.

Для истории цен из того же виджета `webPrice` берутся ещё четыре поля. Их
структура проверена на живых карточках 29.09.2026. В CSV и `ozon_products` они
не попадают:

| Поле           | Описание                                       | Источник                                        |
| -------------- | ---------------------------------------------- | ----------------------------------------------- |
| `card_price`   | Цена с Ozon Картой                             | `webPrice.cardPrice`                            |
| `old_price`    | Зачёркнутая цена «до скидки»                   | `webPrice.originalPrice` (если `showOriginalPrice`) |
| `discount_pct` | Скидка `price` относительно `old_price`, %     | вычисляется                                     |
| `is_available` | Товар в наличии                                | `webOutOfStock` → нет; `webPrice.isAvailable`, `webSale.offer.isAvailable`, запасной — JSON-LD `availability` |

Колонка `source` (`api` или `html`) показывает, откуда разобрана карточка: у
записей из HTML описания и полных характеристик нет (см.
[architecture.md](architecture.md#как-устроен-парсинг-карточки)).
