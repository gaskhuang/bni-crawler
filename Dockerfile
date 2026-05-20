# Cloud Run runs this image. We deliberately do NOT install Selenium / Chrome
# because the async scraper uses pure aiohttp — a slim image starts faster and
# costs less.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install only the runtime deps the web server needs. (Skipping selenium /
# webdriver-manager keeps the image well under 200 MB.)
COPY requirements.txt ./
RUN pip install --no-cache-dir \
        "aiohttp>=3.9.0" \
        "beautifulsoup4>=4.12.0" \
        "lxml>=4.9.0" \
        "requests>=2.31.0" \
        "pyyaml>=6.0" \
        "fastapi>=0.110.0" \
        "uvicorn[standard]>=0.27.0" \
        "pydantic>=2.6.0" \
        "google-cloud-storage>=2.14.0"

# Copy application source. We don't need the legacy Selenium-based scrapers
# (bni_scraper.py, bni_scraper_parallel.py) on Cloud Run, but copying everything
# is simpler and a few extra KB is fine.
COPY bni_scraper_async.py bni_core.py server.py config.yaml ./
COPY static/ ./static/

# Cloud Run sets PORT (default 8080); server.py honours it. Bind to 0.0.0.0
# so the container is reachable from outside.
ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-8080}"]
