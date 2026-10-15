# HousIA — Plano de MVP

> Documento de especificação para implementação por uma sessão de código (Claude Code na nuvem).
> Leia inteiro antes de começar. Siga as fases em ordem; cada fase tem critérios de aceite.

---

## 0. Contexto do produto (resumo)

**HousIA** é uma camada inteligente que roda **por cima** dos prontuários eletrônicos (MV, Tasy etc.) via **FHIR R4** e ataca o momento mais perigoso do cuidado: a **transição** do paciente.

- Troca de turno (médico → médico, enfermagem → enfermagem)
- Mudança de setor (UTI → quarto, PS → internação)
- Alta hospitalar → domicílio / home care

Hoje a informação crítica se perde em evoluções longas. O HousIA **lê o histórico fragmentado, resume por perfil de leitor, cruza prescrições e calcula risco** para as próximas 24h e para a reinternação.

Referência de posicionamento: Lya Health (IA que resume consultas médicas). A diferença do HousIA é o foco em **transição de cuidado e previsão de risco**, não na consulta.

### Os 3 pilares (escopo do MVP)

| Pilar | O que o MVP entrega |
|---|---|
| 1. Passagem de plantão por perfil | Resumo gerado por IA, diferente para **médico** e para **enfermagem**, com alertas das próximas horas e citação da fonte de cada afirmação |
| 2. "Waze" da alta | Checklist social/domiciliar + **score de risco de reinternação** (LACE + fatores sociais). Score alto → **trava suave**: a alta exige justificativa registrada |
| 3. Auditoria de prescrição na transição | Reconciliação medicamentosa entre prescrição de origem e destino: omissões, duplicidades, interações e "sintoma novo que pode ser efeito adverso" |

### Princípio clínico inegociável
O HousIA é **suporte à decisão**, nunca decisor. Nenhuma ação é bloqueada sem possibilidade de override humano justificado e auditado. Toda saída da IA mostra **de onde veio** (recurso FHIR de origem) e um aviso de que deve ser conferida.

---

## 1. Stack escolhida (e por quê)

| Camada | Tecnologia | Motivo |
|---|---|---|
| Frontend | **Next.js 15 (App Router) + TypeScript + Tailwind + shadcn/ui** | UI rápida de montar, boa para dashboards, SSR |
| Backend / IA | **Python 3.12 + FastAPI** | Ecossistema de saúde (`fhir.resources`), ML (`scikit-learn`), LLM SDKs; Python é a melhor escolha para o núcleo de IA e score |
| LLM | **Claude API** (SDK `anthropic`) com saída estruturada via tool use / JSON schema | Raciocínio clínico em texto longo, contexto grande |
| Banco | **PostgreSQL 16** + SQLAlchemy 2 + Alembic | Dados próprios do HousIA (resumos, scores, overrides, auditoria) |
| Fonte clínica | **HAPI FHIR Server (Docker)** populado com pacientes sintéticos | Simula o prontuário do hospital sem dado real |
| Dados sintéticos | **Synthea** (gera FHIR R4) + script próprio de cenários de transição em PT-BR | Sem PHI real no MVP |
| Fila | Nenhuma no MVP (chamadas síncronas com cache no Postgres). Celery/Redis só na fase pós-MVP | Simplicidade |
| Auth | Login simples com JWT + papéis (`medico`, `enfermagem`, `gestor`) | Suficiente para demo |
| Infra local | **Docker Compose** (web, api, db, fhir) | Sobe tudo com um comando |

Java foi descartado para o MVP (mais verboso, ecossistema de IA mais fraco). Node puro foi descartado no backend porque o score e a manipulação FHIR são mais maduros em Python.

---

## 2. Arquitetura

```
┌──────────────────────┐        ┌────────────────────────────────────────┐
│  Next.js (web)       │  REST  │  FastAPI (api)                          │
│  - Painel de leitos  │◄──────►│  ├─ fhir_client   (lê HAPI FHIR)        │
│  - Passagem plantão  │        │  ├─ timeline      (normaliza eventos)   │
│  - Planejar alta     │        │  ├─ handoff       (pilar 1, LLM)        │
│  - Reconciliação     │        │  ├─ discharge     (pilar 2, score)      │
│  - Auditoria         │        │  ├─ medrec        (pilar 3, regras+LLM) │
└──────────────────────┘        │  ├─ llm           (wrapper Claude)      │
                                │  └─ audit         (log de tudo)         │
                                └───────┬───────────────────┬────────────┘
                                        │                   │
                                 ┌──────▼──────┐     ┌──────▼──────┐
                                 │ HAPI FHIR   │     │ PostgreSQL  │
                                 │ (sintético) │     │ (HousIA)    │
                                 └─────────────┘     └─────────────┘
```

**Regra de ouro:** o HousIA **lê** do FHIR e **grava só no próprio banco**. No MVP não escreve de volta no prontuário.

### Conceito central: Timeline Clínica Normalizada
Todo pilar consome a mesma estrutura. O módulo `timeline` converte recursos FHIR em uma lista ordenada de eventos:

```python
class ClinicalEvent(BaseModel):
    id: str                 # id interno
    patient_id: str
    timestamp: datetime
    kind: Literal["vital", "lab", "med_order", "med_admin", "note",
                  "procedure", "transfer", "diagnosis", "io"]  # io = diurese/balanço
    summary: str            # texto curto legível
    value: dict | None      # valor estruturado (ex: {"code":"8480-6","value":180,"unit":"mmHg"})
    source_ref: str         # ex: "Observation/123" — OBRIGATÓRIO, usado nas citações
    encounter_id: str | None
    location: str | None    # setor (UTI, Enfermaria 3B...)
```

Recursos FHIR usados: `Patient`, `Encounter`, `Location`, `Observation`, `MedicationRequest`, `MedicationAdministration`, `Condition`, `Procedure`, `DocumentReference`/`ClinicalImpression` (evoluções em texto), `AllergyIntolerance`, `RelatedPerson`.

---

## 3. Estrutura do repositório

```
housia/
├── docker-compose.yml
├── .env.example
├── README.md
├── apps/
│   ├── web/                      # Next.js
│   │   ├── app/
│   │   │   ├── (auth)/login/
│   │   │   ├── leitos/                 # painel principal
│   │   │   ├── pacientes/[id]/
│   │   │   │   ├── page.tsx            # visão geral + timeline
│   │   │   │   ├── passagem/           # pilar 1
│   │   │   │   ├── alta/               # pilar 2
│   │   │   │   └── reconciliacao/      # pilar 3
│   │   │   └── auditoria/
│   │   ├── components/
│   │   └── lib/api.ts                  # cliente tipado da API
│   └── api/                      # FastAPI
│       ├── app/
│       │   ├── main.py
│       │   ├── config.py
│       │   ├── auth/
│       │   ├── fhir_client/
│       │   ├── timeline/
│       │   ├── handoff/
│       │   ├── discharge/
│       │   ├── medrec/
│       │   ├── llm/
│       │   │   ├── client.py
│       │   │   └── prompts/            # prompts versionados em arquivos .md
│       │   ├── audit/
│       │   └── db/ (models, alembic)
│       └── tests/
├── data/
│   ├── seed/                     # scripts de geração e carga no HAPI
│   ├── scenarios/                # cenários de transição em JSON (PT-BR)
│   └── reference/                # tabela de interações, meds de alto risco
└── docs/
    └── decisions/                # ADRs curtos
```

---

## 4. Modelo de dados (Postgres do HousIA)

```
users(id, name, email, password_hash, role, created_at)

handoff_reports(id, patient_id, encounter_id, audience[medico|enfermagem],
                from_location, to_location, window_hours,
                content_json, model, prompt_version, input_hash,
                created_by, created_at)

alerts(id, patient_id, source[handoff|medrec|discharge], severity[info|atencao|critico],
       title, detail, source_refs[], status[aberto|reconhecido|descartado],
       acted_by, acted_at, created_at)

discharge_assessments(id, patient_id, encounter_id,
                      social_checklist_json, lace_components_json,
                      lace_score, social_score, final_risk[baixo|moderado|alto],
                      suggestions_json, created_at)

discharge_overrides(id, assessment_id, user_id, justification, created_at)

medrec_runs(id, patient_id, from_encounter_or_location, to_encounter_or_location,
            findings_json, created_at)

audit_log(id, user_id, action, entity, entity_id, payload_json, created_at)
```

`input_hash` permite cache: mesma timeline + mesmo público + mesmo prompt → não chama o LLM de novo.

---

## 5. Especificação dos pilares

### 5.1 Pilar 1 — Passagem de plantão por perfil

**Endpoint:** `POST /patients/{id}/handoff` body `{audience, window_hours=12, to_location?}`

**Pipeline:**
1. Buscar timeline das últimas `window_hours` + contexto fixo (diagnósticos ativos, alergias, meds ativas).
2. **Pré-processamento determinístico** (antes do LLM) gera "fatos duros":
   - Sinais vitais fora da faixa e tendência (ex: PA subindo nas últimas 3 medidas)
   - Último registro de diurese e horas desde então
   - Próximos horários de medicação (a partir de `MedicationRequest.dosageInstruction`)
   - Mudanças de prescrição no período
   - Escala de risco de queda se existir `Observation` correspondente
3. Chamar o LLM com: fatos duros + evoluções em texto + instrução por público.
4. Saída **estruturada** (JSON schema), validar com Pydantic:

```json
{
  "headline": "string (1 frase)",
  "situacao": "string",              
  "mudancas_no_periodo": [{"texto": "...", "refs": ["MedicationRequest/45"]}],
  "riscos_proximas_horas": [{"texto": "...", "horizonte_h": 2, "severidade": "critico", "refs": ["Observation/88"]}],
  "pendencias": [{"texto": "...", "horario": "20:00", "refs": ["..."]}],
  "perguntas_para_quem_sai": ["..."]
}
```

Formato baseado em **SBAR** (Situação, Background, Avaliação, Recomendação) — padrão de comunicação clínica.

**Diferença por público (no prompt):**
- `medico`: instabilidades, mudanças terapêuticas, exames pendentes, hipóteses, riscos clínicos.
- `enfermagem`: cuidados práticos, horários, acessos, dispositivos, risco de queda/lesão por pressão, diurese, dieta.

**Regras obrigatórias de prompt:**
- Toda afirmação precisa de `refs` existentes na timeline. O backend **descarta** itens com refs inválidas e registra isso.
- Proibido inventar valores, doses ou horários. Se não houver dado, dizer "sem registro".
- Linguagem em PT-BR clínico, frases curtas.

**UI:** card com headline em destaque, riscos em vermelho/amarelo, cada item com link "ver fonte" que abre o evento na timeline. Botões: "Reconhecer alerta", "Regenerar", "Copiar".

### 5.2 Pilar 2 — "Waze" da alta

**Endpoint:** `POST /patients/{id}/discharge-assessment` body `{social_checklist}`

**Checklist social (formulário no front):**
- Mora sozinho? Tem cuidador? Cuidador capacitado?
- Escadas em casa / mobilidade
- Acesso aos medicamentos (consegue comprar? disponível no SUS/Farmácia Popular? — no MVP é resposta manual)
- Precisa de dispositivos (O2, sonda, curativo complexo) e já tem?
- Consulta de retorno agendada?
- Entende o plano de alta (teach-back feito)?

**Score:**
- **LACE** calculado do FHIR: Length of stay, Acuity (internação via urgência), Comorbidities (Charlson simplificado a partir de `Condition`), ED visits nos últimos 6 meses (`Encounter`).
- **Score social** próprio: soma ponderada do checklist (pesos em arquivo de configuração, documentados como **heurística provisória, não validada**).
- `final_risk` = combinação documentada em `discharge/scoring.py` (ex: LACE ≥ 10 ou social ≥ X → alto).
- Toda a composição do score é exibida (sem caixa-preta).

**Trava suave:**
- Risco `alto` → o botão "Confirmar alta" no HousIA fica desabilitado até: (a) aplicar sugestões e reavaliar, ou (b) registrar **override com justificativa** (mín. 20 caracteres), gravado em `discharge_overrides` e `audit_log`.
- LLM gera `suggestions_json`: ajustes concretos no plano (ex: "agendar visita de enfermagem domiciliar em 48h", "simplificar posologia para 1x/dia se clinicamente possível — avaliar com médico").

### 5.3 Pilar 3 — Auditoria de prescrição na transição

**Endpoint:** `POST /patients/{id}/medrec` body `{from, to}` (encounters ou locations)

**Camada determinística (primeiro, sempre):**
- **Omissão:** med ativa na origem e ausente no destino sem `status=stopped` com motivo → alerta.
- **Duplicidade terapêutica:** mesma classe/princípio ativo duas vezes.
- **Interações:** tabela local `data/reference/interactions.csv` (curada, pequena, ~50 pares de alto impacto: anticoagulantes, opioides+benzodiazepínicos, K+ e IECA etc.). Marcar como base de demonstração.
- **Alto risco:** lista de medicamentos potencialmente perigosos (insulina, heparina, opioides, KCl concentrado...) em `high_alert_meds.csv` → exige dupla checagem.
- **Alergia x prescrição:** cruzar `AllergyIntolerance`.

**Camada LLM (depois):**
- Recebe: achados determinísticos + meds suspensas/omitidas + sintomas novos na timeline das últimas 48h.
- Pergunta: algum sintoma novo é compatível com efeito adverso ou com retirada de medicação? Ex: "taquicardia + agitação após suspensão abrupta de betabloqueador não registrada na transferência".
- Saída com `confidence` (baixa/média/alta) e `refs`. Hipóteses do LLM aparecem visualmente separadas das regras ("Hipótese da IA — confirmar").

**UI:** tabela lado a lado Origem | Destino com status (mantida, alterada, suspensa, **omitida?**, nova) + lista de alertas.

---

## 6. Wrapper do LLM (`app/llm/client.py`)

- Uma função `generate_structured(prompt_name, variables, schema) -> BaseModel`.
- Prompts em `app/llm/prompts/*.md` com cabeçalho `version:`; a versão é gravada em cada resultado.
- Modelo configurável por env (`LLM_MODEL`). Usar o modelo Claude mais atual disponível na conta; conferir o nome exato do modelo na documentação em docs.claude.com.
- Temperatura baixa. Timeout e 1 retry. Se o JSON vier inválido: 1 tentativa de reparo, depois erro amigável.
- **Pseudonimização antes de enviar:** nome, CPF, telefone e endereço são substituídos por tokens (`[PACIENTE]`) — o LLM nunca recebe identificadores diretos. Mesmo com dado sintético, o fluxo já nasce assim.
- Modo `LLM_MODE=mock` que retorna fixtures, para testes e para rodar sem chave.

---

## 7. Dados sintéticos e cenários de demo

Criar em `data/scenarios/` pelo menos **5 cenários roteirizados** em PT-BR, carregados no HAPI FHIR via `data/seed/load.py`:

1. **UTI → Enfermaria**: pós-op, PA instável às 14h, anti-hipertensivo trocado, antibiótico com horário às 20h. *Esperado:* resumo médico destaca risco de pico hipertensivo; enfermagem destaca horário do ATB e acesso.
2. **Omissão na transferência**: betabloqueador ativo na UTI some na prescrição da enfermaria; 12h depois, taquicardia. *Esperado:* alerta de omissão + hipótese de efeito de retirada.
3. **Alta de alto risco**: idoso, mora sozinho, 3 idas ao PS em 6 meses, insulina nova. *Esperado:* risco alto, trava, sugestões.
4. **Alta de baixo risco**: jovem, apendicectomia sem intercorrência. *Esperado:* risco baixo, sem trava.
5. **Interação + alergia**: opioide + benzodiazepínico em idoso; alergia a dipirona com dipirona prescrita. *Esperado:* 2 alertas críticos determinísticos.

Synthea pode gerar população de fundo (20–50 pacientes) para o painel de leitos parecer real; os 5 cenários são os da demo.

Todo nome de paciente é fictício e o app mostra a faixa **"AMBIENTE DE DEMONSTRAÇÃO — DADOS SINTÉTICOS"**.

---

## 8. Telas (Next.js)

1. **Login** (usuários seed: médico, enfermeira, gestor).
2. **Painel de leitos**: grid por setor, cada leito com nome, dias de internação, badges de alertas abertos e risco de alta. Filtro por setor.
3. **Paciente → Visão geral**: dados básicos, alergias em destaque, meds ativas, **timeline** vertical filtrável por tipo.
4. **Paciente → Passagem de plantão**: seletor de público e janela, resultado do pilar 1.
5. **Paciente → Planejar alta**: checklist + score detalhado + sugestões + trava/override.
6. **Paciente → Reconciliação**: pilar 3.
7. **Auditoria** (gestor): log de gerações, overrides e alertas, com filtro por data. Mini-métricas: nº de alertas críticos, overrides, altas de alto risco.

Design: limpo, clínico, alto contraste, legível em tablet (uso à beira do leito). Cores de severidade consistentes: crítico vermelho, atenção âmbar, info azul. Componentes shadcn/ui.

---

## 9. Fases de implementação

### Fase 0 — Esqueleto (meio dia)
- Monorepo, Docker Compose (web, api, db, hapi-fhir), `.env.example`, healthchecks.
- **Aceite:** `docker compose up` sobe tudo; `GET /health` ok; página Next abre.

### Fase 1 — Dados e timeline
- Scripts de seed (Synthea opcional + 5 cenários), `fhir_client`, módulo `timeline`.
- Endpoints: `GET /patients`, `GET /patients/{id}`, `GET /patients/{id}/timeline`.
- Telas: login, painel de leitos, visão geral com timeline.
- **Aceite:** os 5 cenários aparecem com timeline correta e ordenada; cada evento tem `source_ref`.

### Fase 2 — Pilar 1 (passagem de plantão)
- Pré-processamento determinístico + wrapper LLM + prompt + validação de refs + cache.
- **Aceite:** cenário 1 gera resumos diferentes para médico e enfermagem; 100% dos itens exibidos têm ref válida; modo mock funciona sem chave.

### Fase 3 — Pilar 3 (reconciliação)
- Regras determinísticas + tabelas de referência + camada LLM.
- **Aceite:** cenários 2 e 5 geram os alertas esperados; testes unitários cobrem cada regra.

### Fase 4 — Pilar 2 (alta)
- Checklist, LACE, score social, trava suave, override, sugestões.
- **Aceite:** cenário 3 = alto com trava; cenário 4 = baixo; override grava justificativa e aparece na auditoria.

### Fase 5 — Auditoria, polimento e demo
- Tela de auditoria, métricas simples, faixa de demo, disclaimers, README com roteiro de demo de 5 minutos.
- **Aceite:** um avaliador sem contexto consegue seguir o README e demonstrar os 3 pilares.

---

## 10. Testes

- **Backend (pytest):** regras de medrec, cálculo LACE, score social, validação de refs, pseudonimização, cache por `input_hash`.
- **Testes "golden" dos cenários:** para cada cenário, asserts sobre o que *precisa* aparecer (ex: alerta de omissão do betabloqueador) — rodando em `LLM_MODE=mock` no CI e opcionalmente com LLM real localmente.
- **Frontend:** Playwright com 1 fluxo ponta a ponta por pilar.

---

## 11. Segurança, LGPD e regulatório (já no MVP)

- Sem dado real de paciente no MVP. Ponto final.
- Pseudonimização antes do LLM; logs nunca gravam texto clínico bruto do prompt completo em produção (no MVP, flag `LOG_PROMPTS=false` por padrão).
- `audit_log` de toda geração, visualização de resumo, override e mudança de status de alerta.
- Disclaimer visível: "Ferramenta de apoio à decisão. Não substitui avaliação profissional."
- Anotar em `docs/decisions/` que, para uso real, será necessário: avaliação de enquadramento como software como dispositivo médico (ANVISA, RDC 657/2022), DPIA/RIPD sob LGPD, contrato de operador de dados com o provedor de LLM e validação clínica prospectiva.

---

## 12. Fora do escopo do MVP

- Escrita de volta no prontuário (MV/Tasy) ou integração real com eles
- Transcrição de áudio / multimodal (imagem, voz) — fica para a v2
- Modelo de ML treinado próprio para reinternação (MVP usa LACE + heurística)
- Consulta real de disponibilidade em farmácias
- App mobile nativo, multi-hospital, multi-tenant, SSO
- Base completa de interações medicamentosas (MVP usa tabela curada de demonstração)

---

## 13. Variáveis de ambiente

```
ANTHROPIC_API_KEY=
LLM_MODEL=
LLM_MODE=real            # real | mock
DATABASE_URL=postgresql+psycopg://housia:housia@db:5432/housia
FHIR_BASE_URL=http://fhir:8080/fhir
JWT_SECRET=
LOG_PROMPTS=false
NEXT_PUBLIC_API_URL=http://localhost:8000
```

---

## 14. Instruções para a sessão de código

1. Comece pela Fase 0 e só avance quando o critério de aceite da fase atual passar.
2. Faça commits pequenos por fase, com mensagem `fase-N: ...`.
3. Toda decisão relevante fora deste documento vira um ADR curto em `docs/decisions/`.
4. Em dúvida clínica, prefira **mostrar o dado bruto com a fonte** a deixar a IA interpretar.
5. Nunca remova a obrigatoriedade de `refs` nas saídas do LLM.
6. Ao final de cada fase, atualize o README com como rodar e o que já funciona.

---

## 15. Próximos passos depois do MVP (v2)

- Piloto com dados reais anonimizados de um hospital parceiro, medindo: tempo de passagem de plantão, alertas aceitos vs descartados, reinternações em 30 dias.
- Integração FHIR real (via barramento do hospital) e SMART on FHIR para abrir dentro do prontuário.
- Modelo de reinternação treinado com dados locais e calibração.
- Entrada multimodal: áudio da passagem de plantão transcrito e cruzado com a timeline.
- Versão home care: plano de alta compartilhado com cuidador/familiar.
