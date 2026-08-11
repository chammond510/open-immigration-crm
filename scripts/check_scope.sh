#!/bin/sh
set -eu

if git grep -I -n -E '(^|[^a-z])(openai|anthropic|gemini|lawpay|gmail|google drive|google oauth|celery|redis|twilio|dialpad|calendly)([^a-z]|$)' -- '*.py' '*.html' '*.js' '*.in' ':!scripts/check_scope.sh'; then
  echo "Scope check failed: removed AI, provider integration, or background-queue code found."
  exit 1
fi

echo "Scope check passed."
