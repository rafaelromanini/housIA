# HousIA — MVP

Camada inteligente sobre prontuários (via **FHIR R4**) para o momento mais perigoso do cuidado: a **transição** do paciente
(troca de turno, mudança de setor, alta). Especificação completa em [`HOUSIA_MVP_PLAN.md`](HOUSIA_MVP_PLAN.md).

> **AMBIENTE DE DEMONSTRAÇÃO — DADOS SINTÉTICOS.** Ferramenta de apoio à decisão. Não substitui avaliação profissional.

## Como rodar

```bash
cp .env.example .env        # LLM_MODE=mock roda sem chave; LLM_MODE=real + ANTHROPIC_API_KEY usa o Claude
docker compose up --build
```

| Serviço | URL |
|---|---|
| Web (Next.js) | http://localhost:3000 |
| API (FastAPI) | http://localhost:8000 (docs em `/docs`, saúde em `/health`) |
| HAPI FHIR | http://localhost:8080/fhir |

O serviço `seed` carrega no HAPI os 5 cenários roteirizados + 19 pacientes de fundo, com horários
ancorados no momento da carga. Para reancorar antes de uma demo: `docker compose run --rm seed`.

Usuários de demonstração (senha `housia123`): `medico@housia.dev`, `enfermeira@housia.dev`, `gestor@housia.dev`.

### Desenvolvimento sem Docker

```bash
# API (FHIR em memória, SQLite, LLM mock)
cd apps/api && uv venv && uv pip install -e ".[dev]"
.venv/bin/uvicorn app.main:app --reload --port 8000
.venv/bin/pytest

# Web
cd apps/web && npm install && npm run dev
```

## Status por fase

- [x] Fase 0 — esqueleto: compose (web, api, db, fhir), healthchecks, `/health`
- [x] Fase 1 — dados e timeline: 5 cenários + população de fundo, `fhir_client` (HAPI/local), timeline
      normalizada com `source_ref` em todo evento, login, painel de leitos, visão geral do paciente
- [x] Fase 2 — passagem de plantão (pilar 1): fatos determinísticos, pseudonimização, Claude com saída
      estruturada (ou mock), validação de refs, cache por `input_hash`, alertas reconhecíveis

## Estrutura

```
apps/api      FastAPI (fhir_client, timeline, handoff, discharge, medrec, llm, audit)
apps/web      Next.js 15 + Tailwind 4 + componentes shadcn/ui
data/         cenários (DSL PT-BR), tabelas de referência curadas, script de carga no HAPI
docs/         ADRs
design-system Design system gerado com a skill ui-ux-pro-max + overrides clínicos
```
