#!/bin/sh
# Try Open Immigration CRM on this computer with fictional demo records.
#
#   sh scripts/try.sh               set up on the first run, then start and open the browser
#   sh scripts/try.sh --yes         install uv and Python 3.12 without asking, if needed
#   sh scripts/try.sh --port 8010   use another port (the next free one is used if it is taken)
#   sh scripts/try.sh --no-browser  start without opening the browser
#   sh scripts/try.sh --reset       delete the trial and start again
#
# Everything stays in this folder: Python packages in .venv/, and the database,
# uploads, and sign-in details in trial/. It runs in development mode and only this
# computer can reach it. Never put real client information in a trial.
set -eu

cd "$(dirname "$0")/.."

PORT=8000
ASSUME_YES=no
OPEN_BROWSER=yes
RESET=no
while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="${2:?--port needs a number}"; shift 2 ;;
    --yes) ASSUME_YES=yes; shift ;;
    --no-browser) OPEN_BROWSER=no; shift ;;
    --reset) RESET=yes; shift ;;
    -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done

say() { printf '\n==> %s\n' "$1"; }

# Questions go to stderr, so a helper whose output is captured can still ask them.
confirm() {
  [ "$ASSUME_YES" = yes ] && return 0
  if [ ! -t 0 ]; then
    echo "Run again with --yes to allow this." >&2
    return 1
  fi
  printf '%s [y/N] ' "$1" >&2
  read -r answer
  case "$answer" in y|Y|yes|Yes) return 0 ;; *) return 1 ;; esac
}

python_ok() { "$1" -c 'import sys; sys.exit(sys.version_info < (3, 12))' >/dev/null 2>&1; }

find_uv() {
  if command -v uv >/dev/null 2>&1; then command -v uv
  elif [ -x "$HOME/.local/bin/uv" ]; then echo "$HOME/.local/bin/uv"
  fi
}

get_uv() {
  uv_path=$(find_uv)
  if [ -z "$uv_path" ]; then
    echo "This needs Python 3.12. uv (https://docs.astral.sh/uv/) can download it into" >&2
    echo "your home folder, without an administrator password." >&2
    confirm "Install uv now?" || { echo "Install uv or Python 3.12, then run this again." >&2; exit 1; }
    curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >/dev/null
    uv_path="$HOME/.local/bin/uv"
  fi
  echo "$uv_path"
}

if [ "$RESET" = yes ]; then
  confirm "Delete the trial's database, uploads, and sign-in details?" || exit 1
  rm -rf trial
fi

# 1. Python, in .venv/ inside this folder. One already there must be 3.12 or newer.
if [ -e .venv ] && ! python_ok .venv/bin/python; then
  echo "The .venv folder here is not Python 3.12 or newer. Move it aside and run this again." >&2
  exit 1
fi
# Python 3.12 is what the Docker image and the tests use. uv provides it when it is
# installed; otherwise a python3.12 already on this computer will do.
if [ ! -e .venv ]; then
  say "Setting up Python 3.12"
  if [ -z "$(find_uv)" ] && command -v python3.12 >/dev/null 2>&1 \
    && python3.12 -m venv .venv >/dev/null 2>&1; then
    :
  else
    rm -rf .venv
    uv=$(get_uv)
    "$uv" venv --quiet --python 3.12 .venv
  fi
fi

# 2. The CRM's packages, installed again only when they change.
mkdir -p trial
if ! cmp -s requirements.lock trial/installed-requirements.lock; then
  say "Installing the CRM's packages (a minute or two the first time)"
  if .venv/bin/python -m pip --version >/dev/null 2>&1; then
    .venv/bin/python -m pip install --quiet --disable-pip-version-check -r requirements.lock
  else
    uv=$(get_uv)
    "$uv" pip install --quiet --python .venv/bin/python -r requirements.lock
  fi
  cp requirements.lock trial/installed-requirements.lock
fi

# 3. A database of its own, with an administrator and fictional records the first time.
export DEBUG=true
export MEDIA_ROOT="$PWD/trial/media"
DATABASE_URL=$(.venv/bin/python -c 'import pathlib, urllib.parse; print("sqlite:///" + urllib.parse.quote(str(pathlib.Path("trial/db.sqlite3").resolve())))')
export DATABASE_URL
# The sign-in details are written last, so a first run that stops partway starts over.
[ -f trial/login.txt ] || rm -f trial/db.sqlite3
.venv/bin/python manage.py migrate --noinput -v0
if [ ! -f trial/login.txt ]; then
  say "Adding fictional demo records"
  ADMIN_PASSWORD=$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(12))')
  ADMIN_PASSWORD="$ADMIN_PASSWORD" .venv/bin/python manage.py bootstrap_admin --username admin >/dev/null
  .venv/bin/python manage.py seed_demo --username admin >/dev/null
  (umask 077 && printf 'Username: admin\nPassword: %s\n' "$ADMIN_PASSWORD" > trial/login.txt)
fi

# 4. Start on the first free port and open the browser.
PORT=$(.venv/bin/python - "$PORT" <<'EOF'
import socket, sys
start = int(sys.argv[1])
for port in range(start, start + 50):
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", port)) != 0:
            print(port)
            break
else:
    sys.exit(f"No free port from {start} to {start + 49}.")
EOF
)
URL="http://127.0.0.1:$PORT/"
say "Open Immigration CRM is running at $URL"
cat trial/login.txt
echo "The sign-in details are also in trial/login.txt. Press Control-C to stop."
if [ "$OPEN_BROWSER" = yes ]; then
  for opener in open xdg-open; do
    if command -v "$opener" >/dev/null 2>&1; then
      (sleep 2 && "$opener" "$URL" >/dev/null 2>&1) &
      break
    fi
  done
fi
exec .venv/bin/python manage.py runserver "127.0.0.1:$PORT" --noreload
