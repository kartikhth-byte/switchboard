FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-default-groups   # no dev tools, no voice pipeline
COPY core ./core
COPY integrations ./integrations
COPY agents ./agents
COPY api ./api
# Fictional demo fixtures the agents read at runtime (bookings); no real data lives here.
COPY data/fixtures ./data/fixtures
EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
