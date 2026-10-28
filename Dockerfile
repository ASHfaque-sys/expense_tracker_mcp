FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock* ./
RUN uv sync --no-dev

COPY . .

ENV PATH="/app/.venv/bin:$PATH"
ENV DB_PATH=/data/expenses.db
RUN mkdir -p /data

EXPOSE 8000
CMD ["python", "main.py", "--http"]