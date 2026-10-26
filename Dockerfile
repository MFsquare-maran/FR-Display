FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PORT=8765 \
    START_ICAO=LSZH \
    CACHE_DIR=/app/cache

WORKDIR /app
COPY server.py index.html lszh_data.js ./
COPY assets ./assets

# Flughafen-Cache (Overpass-Daten) als Volume, damit er Neustarts überlebt
RUN useradd --system --no-create-home app && mkdir -p /app/cache && chown app /app/cache
USER app
VOLUME /app/cache

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/api/config' % os.environ.get('PORT','8765'),timeout=3)"

CMD ["python", "server.py"]
