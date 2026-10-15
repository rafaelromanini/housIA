# HousIA — MVP

Camada inteligente que roda **por cima** dos prontuários (via **FHIR R4**) para o momento mais perigoso do
cuidado: a **transição** do paciente — troca de turno, mudança de setor e alta. Especificação completa em
[`HOUSIA_MVP_PLAN.md`](HOUSIA_MVP_PLAN.md).

> **AMBIENTE DE DEMONSTRAÇÃO — DADOS SINTÉTICOS.** Ferramenta de apoio à decisão. Não substitui avaliação
> profissional. O HousIA só **lê** do FHIR e grava apenas no próprio banco.

| Pilar | O que o MVP entrega |
|---|---|
| 1. Passagem de plantão por perfil | Resumo SBAR diferente para médico e enfermagem, riscos das próximas horas, pendências com horário, fonte (recurso FHIR) em cada item; **conferência da passagem verbal** (omissões e divergências) |
| 2. "Waze" da alta | Checklist social pré-preenchido do FHIR, LACE + score social transparentes, sugestões da IA, **trava suave** com override justificado; **sumário SOAP** e **orientações ao paciente** em linguagem simples (pt-BR/es/en) |
| 3. Auditoria de prescrição na transição | Origem × destino por princípio ativo: omissão, duplicidade, interação, alto alerta, alergia + hipóteses da IA (efeito adverso/retirada) separadas das regras |

## Como rodar

```bash
cp .env.example .env        # LLM_MODE=mock roda sem chave; LLM_MODE=real + ANTHROPIC_API_KEY usa o Claude
docker compose up --build   # ou: make up
```

| Serviço | URL |
|---|---|
| Web (Next.js) | http://localhost:3000 |
| API (FastAPI) | http://localhost:8000 — OpenAPI em `/docs`, saúde em `/health` |
| HAPI FHIR | http://localhost:8080/fhir |

O serviço `seed` carrega no HAPI os 5 cenários roteirizados + 19 pacientes de fundo, com horários ancorados no
momento da carga. **Antes de cada demo rode `make seed`** (ou `docker compose run --rm seed`) para reancorar os
horários no "agora" — a carga é idempotente e remove recursos obsoletos de cargas anteriores.

Usuários de demonstração (senha `housia123`):

| Perfil | E-mail | Pode |
|---|---|---|
| Médico(a) | `medico@housia.dev` | tudo dos pilares, override e confirmação de alta |
| Enfermagem | `enfermeira@housia.dev` | pilares 1–3 e checklist (sem override/confirmação) |
| Gestão | `gestor@housia.dev` | tela de auditoria e métricas |

### Usar o Claude de verdade

No `.env`: `LLM_MODE=real`, `ANTHROPIC_API_KEY=...` e opcionalmente `LLM_MODEL` (padrão `claude-opus-5-5`) e
`LLM_EFFORT` (`low` | `medium` | `high`; padrão `medium`). Cada geração leva ~10–45 s. Saída estruturada com JSON
Schema, 1 tentativa de reparo, fallback automático em recusas e erro amigável (ver
[ADR 0005](docs/decisions/0005-llm-saida-estruturada-e-mock.md)).

### Desenvolvimento sem Docker

```bash
make api-dev   # API com FHIR em memória (mesmos cenários), SQLite e LLM mock — porta 8000
make web-dev   # Next.js em modo dev — porta 3000
make test      # pytest (62 testes: regras, scores, refs, pseudonimização, cache, golden dos cenários)
make e2e       # Playwright (7 fluxos) contra a stack rodando
```

## Roteiro de demo (5 minutos)

> Rode `make seed` antes. Para mostrar o LLM real, use `LLM_MODE=real`; no modo mock tudo funciona igual, só que
> com texto determinístico.

**0:00 — Contexto (painel de leitos).** Entre como `medico@housia.dev`. Mostre a faixa de demonstração, o painel
por setor e clique em **"Só cenários da demo"**: 5 pacientes roteirizados.

**0:40 — Pilar 1: passagem de plantão (Antônio, cenário 1).** Abra *Antônio Carlos Ribeiro* → aba
**Passagem de plantão** → público **Médico(a)** → **Gerar passagem**.
- Destaque: "Em uma frase" aponta a **PA subindo 152 → 168 → 184** após a troca de anlodipino por losartana.
- Clique em **ver fonte** em um risco: abre o registro FHIR exato (`Observation/...`). Nenhum item aparece sem fonte.
- Troque para **Enfermagem** e gere de novo: agora aparecem o **antibiótico das 20h**, o **AVP com 72h**,
  **sem diurese desde a retirada da SVD** e o **risco de queda** — mesmo paciente, foco diferente.
- **Reconhecer alerta** em um risco crítico.

**1:50 — Conferência da passagem verbal.** No mesmo paciente, cole no campo "Relato de quem está saindo":
*"Paciente veio da UTI, pós-op de colecistectomia, PA estável, sem intercorrências. Ceftriaxona às 20h."* →
**Conferir com o prontuário**. O HousIA mostra a **divergência** ("PA estável" × PA crítica em ascensão) e o que
**não foi dito** (SVD, queda, acesso, troca do anti-hipertensivo).

**2:40 — Pilar 3: reconciliação (Maria, cenário 2, e Iracema, cenário 5).**
- *Maria das Graças* → **Reconciliação** → **Executar**: o **metoprolol** estava na UTI e sumiu na enfermaria
  sem registro de suspensão → **omissão crítica**. Na caixa tracejada **"Hipótese da IA — confirmar"**: taquicardia,
  pressão alta e agitação 12h depois, compatíveis com **retirada de betabloqueador**.
- *Iracema* → **Executar**: **2 alertas críticos determinísticos** — dipirona prescrita em paciente **alérgica a
  dipirona** e **morfina + diazepam** em idosa (FR 11, SpO2 91%).

**3:40 — Pilar 2: alta (Sebastião, cenário 3, e Lucas, cenário 4).**
- *Sebastião* → **Planejar alta**: o checklist já vem com "mora sozinho" e "mobilidade reduzida" sugeridos do
  prontuário (com fonte). Responda o resto como risco (sem cuidador, sem retorno, sem teach-back) → **Calcular**.
  Risco **ALTO** (LACE 15 + social), **Confirmar alta travado**. Mostre a composição do score e as **sugestões**
  (Melhor em Casa, insumos de insulina, teach-back…).
- Registre um **override** (o campo exige 20+ caracteres) → trava liberada → **Confirmar alta**.
- **Documentos de alta** → idioma *Español* (com LLM real) → **Gerar**: orientações em linguagem simples para o
  paciente (remédios, sinais de alerta, cuidados) + **sumário SOAP** com botão copiar para o prontuário.
- *Lucas* (apendicectomia): risco **baixo**, sem trava.

**4:40 — Auditoria.** Saia e entre como `gestor@housia.dev` → **Auditoria**: alertas críticos, overrides com
justificativa, altas de alto risco, utilidade percebida da IA e aceitação de alertas; filtre por
"Overrides de alta".

## Arquitetura

```
apps/web (Next.js 15)  ──REST──►  apps/api (FastAPI)  ──lê──►  HAPI FHIR R4 (sintético)
                                     │  fhir_client · timeline · handoff · medrec · discharge · llm · audit
                                     └──grava──► PostgreSQL (resumos, alertas, scores, overrides, auditoria)
```

- **Timeline clínica normalizada** (`app/timeline`): todo pilar consome a mesma lista de eventos, cada um com
  `source_ref` (ex.: `Observation/cen1-sv9-pa`).
- **Fatos duros determinísticos** antes de qualquer LLM; **pseudonimização** (nome, CPF, telefone, endereço,
  familiares) antes de enviar; **validação de refs** depois (item sem fonte válida é descartado e auditado).
- **Prompts versionados** em `apps/api/app/llm/prompts/*.md`; versão gravada em cada resultado; cache por
  `input_hash`.
- Detalhes e decisões em [`docs/decisions/`](docs/decisions/README.md); benchmark com a Lya Health em
  [`docs/benchmark-lya.md`](docs/benchmark-lya.md); design system (skill ui-ux-pro-max) em
  [`design-system/`](design-system/housia/MASTER.md).

```
apps/api      FastAPI + SQLAlchemy/Alembic + SDK anthropic
apps/web      Next.js 15 + Tailwind 4 + componentes shadcn/ui (Radix)
data/         cenários (DSL PT-BR), tabelas de referência curadas, script de carga no HAPI
docs/         ADRs e benchmark
```

## Status por fase

- [x] Fase 0 — esqueleto: compose (web, api, db, fhir, seed), healthchecks, `/health`
- [x] Fase 1 — dados e timeline: 5 cenários + população de fundo, `fhir_client` (HAPI/local), timeline com
      `source_ref` em todo evento, login, painel de leitos, visão geral do paciente
- [x] Fase 2 — passagem de plantão (pilar 1): fatos determinísticos, pseudonimização, Claude com saída
      estruturada (ou mock), validação de refs, cache por `input_hash`, alertas reconhecíveis
- [x] Fase 3 — reconciliação (pilar 3): regras com testes unitários + hipóteses da IA sobre sintomas novos
- [x] Fase 4 — "Waze" da alta (pilar 2): LACE + social transparentes, sugestões, trava suave e override auditado
- [x] Fase 5 — auditoria com métricas, disclaimers, e2e dos 3 pilares, CI, este roteiro
- [x] Pós-MVP (benchmark Lya): conferência da passagem verbal, documentos de alta multilíngues, feedback da IA

## Segurança, LGPD e regulatório

Sem dado real de paciente. Pseudonimização antes do LLM; `LOG_PROMPTS=false` por padrão; `audit_log` de gerações,
visualizações, overrides e status de alertas (sem texto clínico bruto). Para uso real: enquadramento ANVISA
(SaMD, RDC 657/2022), RIPD/DPIA sob a LGPD, contrato de operador com o provedor de LLM e validação clínica
prospectiva — ver [ADR 0004](docs/decisions/0004-regulatorio-e-lgpd.md).
