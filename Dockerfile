# Container image for Railway / Render / Fly / any container host.
# Long-lived FastAPI process (WebSockets + a background reminder loop), so it must run as
# a persistent service, not serverless. Reproducible via uv.lock.

FROM python:3.12-slim

# uv binary from the official image.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# Install dependencies first (cached unless the lockfile changes).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Then the app itself.
COPY . .
RUN uv sync --frozen --no-dev

# Railway/Render inject $PORT; the app reads it (defaults to 8080 locally).
EXPOSE 8080
CMD ["uv", "run", "--no-dev", "python", "-m", "clinic_agent.main"]
