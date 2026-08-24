# Playwright's image already contains Chromium and every system library it
# needs, which is what makes USE_PLAYWRIGHT_FALLBACK work on a cloud host.
# If you only ever use the direct API (the default), you can swap this for a
# plain `python:3.12-slim` base and drop the playwright install below.
FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir playwright

COPY laguerite/ ./laguerite/
COPY tests/ ./tests/

# Persisted between restarts if you mount a volume at /app/state (optional).
RUN mkdir -p /app/state /app/logs

# Logs go to stdout so the host collects them; the file handler is a bonus.
ENV STATE_FILE=/app/state/state.json \
    LOG_FILE=/app/logs/monitor.log

CMD ["python", "-m", "laguerite.monitor"]
