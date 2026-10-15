# HousIA — MVP

Camada inteligente sobre prontuários (via **FHIR R4**) para o momento mais perigoso do cuidado: a **transição** do paciente
(troca de turno, mudança de setor, alta). Especificação completa em [`HOUSIA_MVP_PLAN.md`](HOUSIA_MVP_PLAN.md).

> **AMBIENTE DE DEMONSTRAÇÃO — DADOS SINTÉTICOS.** Ferramenta de apoio à decisão. Não substitui avaliação profissional.

## Como rodar

```bash
cp .env.example .env        # LLM_MODE=mock roda sem chave
docker compose up --build
```

| Serviço | URL |
|---|---|
| Web (Next.js) | http://localhost:3000 |
| API (FastAPI) | http://localhost:8000 (docs em `/docs`, saúde em `/health`) |
| HAPI FHIR | http://localhost:8080/fhir |

## Status por fase

- [x] Fase 0 — esqueleto: compose (web, api, db, fhir), healthchecks, `/health`
