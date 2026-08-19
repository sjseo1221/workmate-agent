FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv==0.11.28 \
    && uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

COPY app ./app
COPY migrations ./migrations
COPY tools ./tools

EXPOSE 8001

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
