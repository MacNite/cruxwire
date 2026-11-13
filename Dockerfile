FROM python:3.12-slim

LABEL org.opencontainers.image.title="Cruxwire" \
      org.opencontainers.image.source="https://github.com/MacNite/cruxwire" \
      org.opencontainers.image.licenses="MIT"

# tzdata so the scheduler honours TZ (set in compose). Without it the slim
# image has no zoneinfo and the container runs in UTC, firing runs in the
# wrong local window.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# App code + bundled assets. Mutable data lives on the /data volume, not here.
COPY server.py pipeline.py settings.py corpus_archive.py \
     preferences.py onboarding.py images.py article_ids.py netsafe.py users.py oidc.py digest.html login.html \
     favicon.ico favicon-16x16.png favicon-32x32.png apple-touch-icon.png logo-mark.png \
     feeds.sample.json categories.sample.json ./
COPY static/ ./static/

ENV STATE_FILE=/data/state.json \
    DIGEST_FILE=/data/digest.json \
    FEEDS_FILE=/data/feeds.json \
    SETTINGS_FILE=/data/settings.json \
    RUNS_FILE=/data/runs.json \
    EMBEDDINGS_FILE=/data/embeddings.json \
    CATEGORIES_FILE=/data/categories.json \
    PREFERENCES_FILE=/data/preferences.json \
    IMAGE_CACHE_FILE=/data/image_cache.json \
    EMBED_CACHE_FILE=/data/embed_cache.json \
    ONBOARDING_POOL_FILE=/data/onboarding_pool.json \
    STATIC_DIR=/app \
    SEED_FEEDS=/app/feeds.sample.json \
    SEED_CATEGORIES=/app/categories.sample.json \
    PORT=8090

VOLUME /data
EXPOSE 8090

# No build step, no dependencies — pure stdlib.
CMD ["python", "server.py"]
