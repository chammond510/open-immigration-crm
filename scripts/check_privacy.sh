#!/bin/sh
set -eu

tracked="$(git ls-files)"

if printf '%s\n' "$tracked" | grep -Eiq '(^|/)(\.env|db\.sqlite3|media|backups?|exports?)(/|$)|\.(sql|sqlite|sqlite3|pem|key|p12|pfx)$'; then
  echo "Privacy check failed: tracked secret, database, media, backup, or export artifact."
  exit 1
fi

if git grep -I -n -E -e 'chrishammondlaw|CHLF CRM|LawPay|Dialpad|Calendly|Twilio|USCIS_API|GOOGLE_(DRIVE|CLIENT|OAUTH)|OPENAI_API_KEY|ANTHROPIC_API_KEY' -- ':!scripts/check_privacy.sh'; then
  echo "Privacy check failed: firm-specific or removed integration marker found."
  exit 1
fi

if git grep -I -n -E -e '-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9_]{30,}' -- ':!scripts/check_privacy.sh'; then
  echo "Privacy check failed: possible credential found."
  exit 1
fi

echo "Privacy check passed."
