# Pantree storefront for Google Cloud Run.
# The shop's data is downloaded at build time and baked into the image; the live database
# (accounts, carts, orders, activity) is Postgres (e.g. Neon) via the DATABASE_URL secret.
FROM python:3.12-slim

ARG DATA_SIZE=small
ARG RECENT_MONTHS=6

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080 \
    APP_ENV=production \
    PANTREE_LIVE_MIRROR=0 \
    PANTREE_HEAVY_RECOMMENDATIONS=0

# LightGBM needs the OpenMP runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
# CPU-only PyTorch: the image never uses a GPU, and the CUDA build is several GB larger.
RUN pip install -r requirements.txt --extra-index-url https://download.pytorch.org/whl/cpu

COPY prepare_data.py ./
COPY recommendations ./recommendations
COPY webapp ./webapp

# The shop's history (catalogue, customers, orders, recent browsing) for serving and seeding.
# Only data/source/manifest.json is read at runtime (seeding); the raw tables are for the notebook.
RUN python prepare_data.py --size "${DATA_SIZE}" --recent-months "${RECENT_MONTHS}" \
    && find data/source -mindepth 1 ! -name manifest.json -exec rm -rf {} + \
    && rm -rf data/_hf

RUN useradd --create-home --uid 1000 app && chown -R app:app /app
USER app

EXPOSE 8080
# --proxy-headers: Cloud Run terminates HTTPS in front of the container.
CMD ["sh", "-c", "exec uvicorn webapp.app:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
