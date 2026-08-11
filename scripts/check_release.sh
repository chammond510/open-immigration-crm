#!/bin/sh
set -eu

./scripts/check_privacy.sh
./scripts/check_scope.sh
.venv/bin/ruff check .
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/coverage erase
.venv/bin/coverage run manage.py test
.venv/bin/coverage report
.venv/bin/pip-audit --requirement requirements.lock --no-deps --disable-pip

DEBUG=False \
SECRET_KEY=release-check-secret-key-000000000000000000000000000000000000 \
ALLOWED_HOSTS=crm.example.test \
CSRF_TRUSTED_ORIGINS=https://crm.example.test \
DATABASE_URL=postgresql://unused:unused@127.0.0.1:9/unused \
SECURE_HSTS_INCLUDE_SUBDOMAINS=True \
SECURE_HSTS_PRELOAD=True \
.venv/bin/python manage.py check --deploy

echo "Release checks passed."
