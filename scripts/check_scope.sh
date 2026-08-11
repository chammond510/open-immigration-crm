#!/bin/sh
set -eu

# The product boundary excludes AI providers, payment processing, email and
# calendar delivery, cloud-drive sync, messaging, e-signature, and background
# queues. Category-representative package and service names act as tripwires.
if git grep -I -n -E '(^|[^a-z])(openai|anthropic|gemini|langchain|celery|redis|rabbitmq|kafka|stripe|paypal|braintree|sendgrid|mailgun|postmark|smtplib|docusign|dropbox|onedrive|google drive|google oauth)([^a-z]|$)' -- '*.py' '*.html' '*.js' '*.in' ':!scripts/check_scope.sh'; then
  echo "Scope check failed: AI, provider integration, or background-queue code found."
  exit 1
fi

echo "Scope check passed."
