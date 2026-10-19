#!/usr/bin/env bash
# Create an isolated deployment folder for one company: deploy/companies/<id>/{company.yaml,.env,compose.yaml}
# Usage: scripts/new-company.sh <company-id> "<Company Name>" <host-port>
set -euo pipefail
id=${1:?company id (e.g. acme)}; name=${2:?company name}; port=${3:?host port (e.g. 8101)}
[[ $id =~ ^[a-z0-9-]+$ ]] || { echo "company id must be lowercase letters, digits, dashes" >&2; exit 1; }
repo=$(cd "$(dirname "$0")/.." && pwd)
dir="$repo/deploy/companies/$id"
[[ -e $dir ]] && { echo "$dir already exists" >&2; exit 1; }
mkdir -p "$dir"
sed -e "s/^  id: .*/  id: $id/" -e "s/^  name: .*/  name: $name/" "$repo/config/company.example.yaml" > "$dir/company.yaml"
secret=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')
tg=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')
wa=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')
sed -e "s|^MM_SECRET_KEY=.*|MM_SECRET_KEY=$secret|" -e "s|^TELEGRAM_WEBHOOK_SECRET=.*|TELEGRAM_WEBHOOK_SECRET=$tg|" \
    -e "s|^WHATSAPP_VERIFY_TOKEN=.*|WHATSAPP_VERIFY_TOKEN=$wa|" "$repo/.env.example" > "$dir/.env"
printf 'COMPANY_ID=%s\nHOST_PORT=%s\nREPO_DIR=%s\n' "$id" "$port" "$repo" >> "$dir/.env"
cp "$repo/compose.yaml" "$dir/compose.yaml"
chmod 600 "$dir/.env"
cat <<MSG
Created $dir
Next:
  1. Edit $dir/company.yaml (timezone, channels, auth, retention, prompt terms)
  2. Edit $dir/.env: MM_PUBLIC_BASE_URL, LLM_API_KEY (gateway key $id/meeting-minutes/prod), SMTP/Telegram/WhatsApp
  3. cd $dir && docker compose --env-file .env up -d --build
  4. docker compose exec web mm add-person --name "Admin" --email admin@example.com --admin --password
  5. Point the reverse proxy (HTTPS) at 127.0.0.1:$port; register the Telegram webhook:
     curl "https://api.telegram.org/bot\$TOKEN/setWebhook" -d url=\$MM_PUBLIC_BASE_URL/webhooks/telegram -d secret_token=<TELEGRAM_WEBHOOK_SECRET>
MSG
