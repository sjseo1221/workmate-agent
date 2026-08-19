FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv==0.11.28 \
    && uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

COPY app ./app
COPY migrations ./migrations
COPY tools ./tools

# internal_chat.py의 _schema_validator()가 WORKMATE_SCHEMA_ROOT 미설정 시
# Path(__file__).resolve().parents[2] == "/" 를 기본 루트로 계산해 "/docs/schemas"를 찾는다
# (app/internal_chat.py가 /app/app/ 아래 있으므로). docs/schemas를 이 저장소 안에 같이 두고
# 이미지의 같은 경로("/docs/schemas")에 구워 넣어서, 별도 볼륨 마운트나 외부 체크아웃 없이도
# 어디서 빌드하든 스킬챗(오늘 브리핑·주간 업무보고 등)이 바로 동작하게 한다.
COPY docs /docs

EXPOSE 8001

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
