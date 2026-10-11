#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

docker compose --env-file .env -f docker-compose.https.yml config --quiet
mkdir -p .local/https/data .local/https/config
chmod 700 .local/https .local/https/data .local/https/config
docker compose --env-file .env -f docker-compose.https.yml up -d

root_cert=.local/https/data/caddy/pki/authorities/local/root.crt
for attempt in {1..30}; do
    if [[ -s "$root_cert" ]]; then
        cp -- "$root_cert" static/local-ca.crt
        chmod 644 static/local-ca.crt
        printf '%s\n' 'HTTPS 已启动。请在访问电脑上信任 static/local-ca.crt，然后使用 HTTPS 地址录音。' '原有 HTTP 页面可通过 /static/local-ca.crt 下载此公共证书。'
        exit 0
    fi
    sleep 1
done
printf '%s\n' 'HTTPS 证书尚未生成，请检查 docker compose -f docker-compose.https.yml logs。' >&2
exit 1
