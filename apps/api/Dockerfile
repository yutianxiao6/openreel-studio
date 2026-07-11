FROM python:3.11-slim

WORKDIR /workspace/apps/api

RUN pip install uv

COPY apps/api/pyproject.toml apps/api/README.md ./
RUN uv sync --no-dev

COPY apps/api/app/ ./app/
COPY plugins/ /workspace/plugins/

EXPOSE 8000

CMD ["/workspace/apps/api/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
