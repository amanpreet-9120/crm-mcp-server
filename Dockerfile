FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    CRM_DB_PATH=/app/data/crm.db \
    CRM_LOG_PATH=/app/logs/tool_calls.jsonl \
    CRM_RESET_ON_START=1 \
    HOST=0.0.0.0 \
    PORT=8000

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health')"
CMD ["sh", "-c", "crm-mcp --transport http --host $HOST --port $PORT"]
