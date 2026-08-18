FROM python:3.14.7-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd --system crm && useradd --system --gid crm --create-home crm

WORKDIR /app

COPY requirements.lock ./
RUN pip install --no-cache-dir --requirement requirements.lock

COPY --chown=crm:crm . .
RUN DEBUG=False \
    SECRET_KEY=build-only-secret-key-not-used-at-runtime-00000000000000000000 \
    ALLOWED_HOSTS=localhost \
    python manage.py collectstatic --noinput

RUN mkdir -p /app/media && chown -R crm:crm /app/staticfiles /app/media
USER crm

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/', timeout=3)"

CMD ["./scripts/start.sh"]
