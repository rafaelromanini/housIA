# 0001 — Fonte FHIR com dois modos: `hapi` e `local`

**Status:** aceita · **Fase:** 1

## Contexto
O plano prevê HAPI FHIR em Docker como "prontuário" sintético. Testes de CI, golden tests dos cenários e
desenvolvimento rápido não deveriam depender de subir um servidor Java.

## Decisão
`FHIR_MODE` escolhe o backend de leitura (`app/fhir_client/client.py`), ambos com a mesma interface:

- `hapi` (padrão no `docker compose`): `HapiFhirClient` faz buscas REST no HAPI FHIR R4.
- `local` (padrão fora do Docker e nos testes): `LocalFhirStore` monta **os mesmos recursos** gerados pelo
  `app/seed/builder.py` em memória.

O seed do HAPI (`data/seed/load.py`) e o modo local usam exatamente o mesmo construtor, então o que é testado
em `local` é o que vai para o HAPI.

## Consequências
- `pytest` roda sem Docker, sem rede e sem chave de LLM.
- No modo `local` os horários são reancorados a cada start da API; no HAPI, a cada `load.py` (rode antes da demo).
- O HousIA continua **somente leitura** sobre o FHIR em ambos os modos.
