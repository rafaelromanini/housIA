# Guia de implantação on-premise

Este guia sobe o HousIA a partir do mesmo artefato nas duas formas: Docker Compose (`docker-compose.prod.yml`) e Kubernetes (`deploy/helm/housia`). O `docker-compose.yml` da raiz é só de demonstração.

## O que muda em produção

| Item | Demonstração | Produção |
| --- | --- | --- |
| Portas expostas | 8000, 8080, 3000 | só 80 e 443 (Caddy ou Ingress) |
| HAPI e Postgres | portas no host | rede interna, sem `ports` |
| Senha do Postgres e JWT | padrões no compose | obrigatórios, sem padrão |
| Sessão | token no localStorage | cookie httpOnly SameSite=strict + token CSRF |
| `ENV` | development | production (o app recusa subir com configuração de demonstração) |

Com `ENV=production` a API não sobe se: `JWT_SECRET` for o padrão ou tiver menos de 32 caracteres; `LLM_MODE=mock` sem `ALLOW_MOCK=true`; ou a senha do Postgres em `DATABASE_URL` for uma senha padrão (`housia`, `postgres`, `password`...).

## Docker Compose

1. `cp .env.prod.example .env.prod` e preencha `HOUSIA_DOMAIN`, `POSTGRES_PASSWORD` e `JWT_SECRET` (`openssl rand -hex 32`). Para IA externa, `LLM_MODE=real`, `ANTHROPIC_API_KEY`, `LLM_MODEL` e `LLM_REGION` (a região aparece na tela Ambiente).
2. TLS: com domínio público o Caddy emite o certificado sozinho. Com certificado do hospital, coloque os arquivos em `deploy/caddy/certs/` e defina `CADDY_TLS="/certs/hospital.crt /certs/hospital.key"`.
3. `docker compose --env-file .env.prod -f docker-compose.prod.yml up -d --build`.
4. O HAPI do compose é sintético. Em produção aponte `FHIR_BASE_URL` para o FHIR do hospital.

## Kubernetes (Helm)

```
helm lint deploy/helm/housia -f deploy/helm/housia/ci/lint-values.yaml
helm install housia deploy/helm/housia \
  --set domain=housia.hospital.example \
  --set secrets.jwtSecret="$(openssl rand -hex 32)" \
  --set secrets.postgresPassword="$(openssl rand -hex 16)" \
  --set images.api=registro/housia-api:0.1.0 --set images.web=registro/housia-web:0.1.0
```

Sem `domain`, `secrets.jwtSecret` e `secrets.postgresPassword` o chart recusa renderizar. Use `secrets.existingSecret` para um Secret gerenciado fora do chart (chaves `jwt-secret`, `postgres-password`, `anthropic-api-key`). A imagem do web precisa ser construída com `--build-arg NEXT_PUBLIC_API_URL=https://<domain>/api`. O Ingress assume ingress-nginx (reescrita de `/api`).

## Conferência

Entre como gestor e abra **Ambiente**: o checklist mostra transcrição, IA (provedor, modelo, região), envio ao prontuário, retenção e criptografia, lidos da configuração em vigor.
