# FinSight REST API — slim production image.
#
# Runs the FastAPI engine (SEC EDGAR + DCF + sentiment) on uvicorn:8001.
# Ships with FINSIGHT_DEMO=1 so a clone-and-run with no network/keys still
# serves real-shaped data through the engine. Unset it (or pass -e
# FINSIGHT_DEMO=0) to use the live SEC EDGAR path.
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    FINSIGHT_HOST=0.0.0.0 \
    FINSIGHT_PORT=8001 \
    FINSIGHT_DEMO=1

WORKDIR /app

# Install Python deps first for better layer caching.
COPY requirements-api.txt ./
RUN pip install --upgrade pip && pip install -r requirements-api.txt

# Copy the package + packaging metadata and install it.
COPY setup.py README.md ./
COPY finsight ./finsight
RUN pip install --no-deps -e .

EXPOSE 8001

# No --reload in the image; reload is for local dev only. Honor $PORT when the
# host (e.g. Render) injects one, else default to 8001.
CMD ["sh", "-c", "uvicorn finsight.api.app:app --host 0.0.0.0 --port ${PORT:-8001}"]
