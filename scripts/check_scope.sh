#!/bin/sh
set -eu

# Core (everything outside connectors/) stays provider-neutral: no AI, payment,
# messaging, mail, drive, or e-signature provider code. Provider adapters live in
# connectors/, one optional package per provider, and core never imports them.
# Background-queue infrastructure is excluded everywhere; jobs use the database.
# Category-representative package and service names act as tripwires. See
# docs/CONNECTOR_PLAN.md. Untracked files are scanned too, so new files are
# checked before they are staged.
providers='openai|anthropic|gemini|langchain|stripe|paypal|braintree|lawpay|sendgrid|mailgun|postmark|smtplib|twilio|dialpad|calendly|docusign|dropbox|onedrive|google drive|google oauth'
queues='celery|redis|rabbitmq|kafka'

if git grep --untracked -I -n -E "(^|[^a-z])(${providers})([^a-z]|\$)" -- '*.py' '*.html' '*.js' '*.in' ':!connectors/' ':!scripts/check_scope.sh'; then
  echo "Scope check failed: provider code belongs in connectors/, not core."
  exit 1
fi

if git grep --untracked -I -n -E "(^|[^a-z])(${queues})([^a-z]|\$)" -- '*.py' '*.html' '*.js' '*.in' ':!scripts/check_scope.sh'; then
  echo "Scope check failed: background-queue infrastructure found."
  exit 1
fi

if git grep --untracked -I -n -E '^[[:space:]]*(from|import)[[:space:]]+connectors([.[:space:]]|$)' -- '*.py' ':!connectors/'; then
  echo "Scope check failed: core imports from connectors/."
  exit 1
fi

echo "Scope check passed."
