FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY patterns.yaml ./
COPY src/ ./src/

# Run unprivileged. /data is chowned before the VOLUME declaration so a freshly
# created named volume inherits the ownership; a bind mount must be chowned on
# the host to uid 10001.
RUN useradd --system --uid 10001 --create-home watchdog \
    && mkdir -p /data/diagnostics \
    && chown -R watchdog:watchdog /data /app

USER watchdog

VOLUME ["/data"]

CMD ["python", "-m", "src.main"]
