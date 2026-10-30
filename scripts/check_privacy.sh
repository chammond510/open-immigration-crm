#!/bin/sh
set -eu

# Untracked (not ignored) files are checked too, so new files fail before they
# are staged.
tracked="$(git ls-files --cached --others --exclude-standard)"

if printf '%s\n' "$tracked" | grep -Eiq '(^|/)(\.env|db\.sqlite3|media|backups?|exports?)(/|$)|\.(sql|sqlite|sqlite3|pem|key|p12|pfx)$|check_privacy\.local$'; then
  echo "Privacy check failed: tracked secret, database, media, backup, export, or local-pattern artifact."
  exit 1
fi

# Connector fixtures must use fictional values; these catch real provider keys,
# including the AI, payment, and OAuth client formats connectors handle.
if git grep --untracked -I -n -E -e '-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9_]{30,}|sk-ant-[A-Za-z0-9_-]{20,}|sk-(proj|svcacct|admin|or-v1)-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{40,}|(sk|rk)_live_[0-9A-Za-z]{20,}|whsec_[0-9A-Za-z]{20,}|GOCSPX-[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35}' -- ':!scripts/check_privacy.sh'; then
  echo "Privacy check failed: possible credential found."
  exit 1
fi

# Operators may keep deployment-specific markers (firm names, internal system
# names, vendor identifiers) in scripts/check_privacy.local, one extended
# regular expression per line. The file is ignored by Git and must stay local.
if [ -f scripts/check_privacy.local ]; then
  if git grep --untracked -I -i -n -E -f scripts/check_privacy.local -- ':!scripts/check_privacy.sh'; then
    echo "Privacy check failed: local marker pattern found in tracked content."
    exit 1
  fi
fi

echo "Privacy check passed."
