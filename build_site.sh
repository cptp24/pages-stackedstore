#!/usr/bin/env bash
# Pull, build and encrypt one client page into dist/<site>/index.html.
# Usage: ./build_site.sh <brand-slug> <site>     e.g. ./build_site.sh stackedstore-my stackedstore
# Needs .env (Shopify token, GA4_SA_KEY_PATH) and the page password in <SITE>_PASSWORD.
set -euo pipefail
cd "$(dirname "$0")"

brand="$1"
site="$2"
pw_var="$(echo "$site" | tr '[:lower:]-' '[:upper:]_')_PASSWORD"
password="${!pw_var:-}"
if [ -z "$password" ]; then
  echo "Set $pw_var to the page password." >&2
  exit 1
fi

# Public repo: Actions logs are public, so in CI the steps print no client figures. Errors still show.
step() {
  local label="$1"; shift
  if [ "${CI:-}" = "true" ]; then "$@" > /dev/null; echo "$label: done"; else "$@"; fi
}
step "Shopify pull" python3 tools/shopify_inventory.py --brand "$brand" --out exports
step "GA4 search pull" python3 tools/ga4_site_search.py --brand "$brand" --exclude-country China --out exports
step "Build" python3 build.py --brand "$brand" --site "$site"
title="$(sed -n 's:.*<title>\(.*\)</title>.*:\1:p' "sites/$site/index.html" | head -1)"

# The salt is fixed per site so "Remember me" survives the daily rebuild. It is not a secret.
npx --yes staticrypt@3.5.4 "build/$site/index.html" \
  --password "$password" --short --config false \
  --salt "$(cat "sites/$site/salt.txt")" \
  --remember 30 \
  --directory "dist/$site" \
  --template-title "$title" \
  --template-instructions "Enter the password Z21 Studio shared with you." \
  --template-button "Open" \
  --template-placeholder "Password" \
  --template-remember "Remember me for 30 days" \
  --template-error "That password is not right" \
  --template-color-primary "#000000" \
  --template-color-secondary "#f2efea"

if grep -q 'id="data-inventory"' "dist/$site/index.html"; then
  echo "dist/$site/index.html contains readable data. Not publishing." >&2
  exit 1
fi
echo "encrypted -> dist/$site/index.html"
