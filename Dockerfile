# Образ парсера: Python + Chromium от Playwright.
#
# Секреты (.env, cookies.json, token.json, credentials.json) в образ не
# попадают (.dockerignore) - docker-compose подключает их при запуске.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # В контейнере нет ни окна, ни системного Chrome: только headless Chromium.
    HEADLESS=1 \
    BROWSER_CHANNEL= \
    TZ=Europe/Moscow

WORKDIR /app

# Зависимости - отдельным слоем: правка кода не пересобирает Chromium.
COPY requirements.txt .
RUN pip install -r requirements.txt "psycopg2-binary==2.9.9" \
    && python -m playwright install --with-deps chromium

COPY . .

# По умолчанию - ежедневный планировщик; разовые команды:
#   docker compose run --rm parser python -m ozon_parser discover
CMD ["python", "-m", "ozon_parser", "schedule"]
