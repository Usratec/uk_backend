FROM python:3.12-slim

# uv is this project's dependency manager. Copy its binary from the official image.
COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /usr/local/bin/uv

# Don't buffer stdout/stderr, so logs appear immediately in `docker logs`.
ENV PYTHONUNBUFFERED=1

# Put the virtualenv outside /app, because docker compose mounts the source code over /app.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
# Let `python`, `gunicorn` etc. resolve to the virtualenv, so commands need no `uv run` prefix.
ENV PATH=/opt/venv/bin:$PATH

# ffmpeg is what apps/learning/transcode.py runs: without it the Celery worker can probe and
# encode nothing. Installed here because every process shares this image; which container is
# actually given the video queue is a deployment decision.
RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies before copying the code, so Docker reuses this layer
# when only application files change. --no-dev skips test and lint tools.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .

# Collect static files (Django admin CSS etc.) so the running container can serve them.
# Local settings are used only because they import without needing environment variables; the
# static files they produce are identical in every environment. At runtime Django reads the
# settings module from DJANGO_SETTINGS_MODULE as usual (config.settings.production by default).
RUN DJANGO_SETTINGS_MODULE=config.settings.local python manage.py collectstatic --noinput

EXPOSE 8000

# Production default. docker compose overrides this with `runserver` for development.
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000"]
