FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RESUMO_METEO_SQLITE_JOURNAL_MODE=DELETE \
    PATH="/app/.venv/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends libeccodes0 libeccodes-data \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE NOTICE.md ./
COPY src ./src
RUN pip install --no-cache-dir uv==0.10.4 \
    && uv sync --frozen --no-dev --extra postgres --no-editable \
    && rm -rf /root/.cache/uv

ENTRYPOINT ["resumo-meteo"]
CMD ["--help"]
