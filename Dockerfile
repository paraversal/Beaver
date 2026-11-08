FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /bin/uv
WORKDIR /app

# Use the image's Python only: a version mismatch with requires-python then
# fails the build instead of silently downloading a Python into /root, which
# the unprivileged runtime user cannot read.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
# Code stays root-owned and read-only; the bot only writes to /app/data.
RUN chmod -R a+rX /app && useradd --system --uid 10001 bot
USER bot

# Config comes from environment variables (compose env_file), not a file.
CMD ["/app/.venv/bin/python", "main.py"]
