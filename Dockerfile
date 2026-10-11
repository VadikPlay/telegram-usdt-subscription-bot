FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --upgrade pip && pip install .

RUN mkdir -p /app/data /app/exports

# No secrets are baked in — everything comes from the environment at runtime.
CMD ["python", "-m", "gatekit"]
