#!/bin/sh
# Sobe a stack (db, HAPI, seed, api, web) num container da nuvem do Claude Code, atrás do proxy com CA própria.
# Uso: scripts/sandbox-stack.sh up -d --build   (qualquer argumento do docker compose)
# Não altera os Dockerfiles versionados: gera cópias que confiam na CA do proxy em um diretório temporário.
set -e
ROOT=$(cd "$(dirname "$0")/.." && pwd)
CA=/root/.ccr/ca-bundle.crt
TMP=${SANDBOX_DIR:-/tmp/housia-sandbox}
mkdir -p "$TMP"

# 1. dockerd (o container não sobe o daemon sozinho)
if ! docker info >/dev/null 2>&1; then
  (dockerd >"$TMP/dockerd.log" 2>&1 &)
  for _ in $(seq 1 30); do docker info >/dev/null 2>&1 && break; sleep 1; done
fi

# 2. imagens base pelo mirror do Google (o Docker Hub devolve 429 aqui)
for img in library/postgres:16-alpine hapiproject/hapi:v8.12.0-1 library/python:3.12-slim library/node:22-alpine; do
  short=${img#library/}
  docker image inspect "$short" >/dev/null 2>&1 || { docker pull -q "mirror.gcr.io/$img" && docker tag "mirror.gcr.io/$img" "$short"; }
done

# 3. CA do proxy dentro dos contextos de build (arquivos ignorados pelo git)
cp "$CA" "$ROOT/.sandbox-ca.crt"; cp "$CA" "$ROOT/apps/web/.sandbox-ca.crt"
sed 's#^COPY apps/api/pyproject.toml ./#COPY .sandbox-ca.crt /usr/local/share/ca-certificates/sandbox-ca.crt\nENV PIP_CERT=/usr/local/share/ca-certificates/sandbox-ca.crt SSL_CERT_FILE=/usr/local/share/ca-certificates/sandbox-ca.crt REQUESTS_CA_BUNDLE=/usr/local/share/ca-certificates/sandbox-ca.crt\nCOPY apps/api/pyproject.toml ./#' \
  "$ROOT/apps/api/Dockerfile" >"$TMP/api.Dockerfile"
sed -e '0,/^COPY package.json package-lock.json .\//s##COPY .sandbox-ca.crt /sandbox-ca.crt\nENV NODE_EXTRA_CA_CERTS=/sandbox-ca.crt\nCOPY package.json package-lock.json ./#' \
  -e 's#^COPY --from=deps /app/node_modules ./node_modules#COPY .sandbox-ca.crt /sandbox-ca.crt\nENV NODE_EXTRA_CA_CERTS=/sandbox-ca.crt\nCOPY --from=deps /app/node_modules ./node_modules#' \
  "$ROOT/apps/web/Dockerfile" >"$TMP/web.Dockerfile"
cat >"$TMP/compose.sandbox.yml" <<YML
services:
  seed:
    build:
      dockerfile: $TMP/api.Dockerfile
  api:
    build:
      dockerfile: $TMP/api.Dockerfile
  web:
    build:
      dockerfile: $TMP/web.Dockerfile
YML

# 4. compose com o builder clássico (o BuildKit consulta o Docker Hub mesmo com a imagem local)
cd "$ROOT"
DOCKER_BUILDKIT=0 COMPOSE_BAKE=false docker compose -f docker-compose.yml -f "$TMP/compose.sandbox.yml" "$@"
