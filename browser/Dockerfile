FROM python:3.12-slim-bookworm

ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright HOME=/tmp/browser-home
RUN pip install --no-cache-dir playwright==1.64.1 \
    && python -m playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 browser \
    && useradd --uid 1000 --gid 1000 --create-home browser

USER 1000:1000
