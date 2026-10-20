# HousIA

Camada inteligente que roda **por cima** do prontuário (via **FHIR R4**) para os momentos mais perigosos do cuidado:
a **transição** do paciente (troca de turno, mudança de setor e alta) e a própria **consulta**, em que a IA
transcreve a conversa ao vivo e sugere perguntas em tempo real.

> **AVISO: AMBIENTE DE DEMONSTRAÇÃO, DADOS SINTÉTICOS. O HousIA NÃO é um dispositivo médico.** É uma ferramenta de
> apoio à decisão, sem registro na ANVISA, e não substitui o julgamento do profissional de saúde. Toda sugestão
> da IA precisa ser revisada por quem atende. Não use com pacientes reais.

Especificação do MVP em [`HOUSIA_MVP_PLAN.md`](HOUSIA_MVP_PLAN.md). Comparação com a Lya Health em
[`docs/benchmark-lya.md`](docs/benchmark-lya.md).

## O que é o HousIA

| | Para quê | O que entrega |
|---|---|---|
| **1. Passagem de plantão por perfil** | Ninguém perde o que importa na troca de turno | Resumo SBAR diferente para médico e enfermagem, riscos das próximas horas, pendências com horário e a fonte (recurso FHIR) em cada item. Confere o relato verbal de quem sai contra o prontuário e aponta omissões e divergências |
| **2. "Waze" da alta** | Alta segura, sem reinternação evitável | Checklist social pré-preenchido do prontuário, LACE e score social transparentes, sugestões da IA e **trava suave** que só o médico libera com justificativa. Gera sumário SOAP e orientações ao paciente em linguagem simples (pt-BR, es, en) |
| **3. Auditoria de prescrição na transição** | Erro de medicação na mudança de setor | Compara origem e destino por princípio ativo: omissão, duplicidade, interação, alto alerta e alergia. Hipóteses da IA (efeito adverso, retirada) ficam separadas das regras |
| **4. Consulta com IA** | Mais atenção ao paciente, menos digitação | Transcrição ao vivo e perguntas sugeridas que saem da lista quando o profissional já as fez, Smart Apps que se atualizam a cada fala e uma nota SOAP + CID revisada e enviada ao prontuário |

Princípio comum: **fatos duros calculados por regras antes de qualquer LLM**, e **nenhum item da IA sem fonte
verificável** no prontuário ou na fala.

## Funcionalidades principais

**Painel e prontuário**
- Painel de leitos por setor, agenda do ambulatório, visão geral do paciente com linha do tempo clínica unificada.
- Perfis com permissões distintas (médico, enfermagem, gestão) e tela de auditoria com métricas.

**Consulta com IA**
- Transcrição ao vivo por microfone do navegador, por servidor Whisper compatível, por digitação ou por roteiro simulado.
- Smart Apps em tempo real: perguntas sugeridas, queixas em tags, emoções e comunicação, hipóteses com CID-10 e diagnósticos
  diferenciais com evidência contra, histórico com datas, alergias (inclusive as relatadas na fala), medicamentos
  (ajuste renal, Beers, interações com o que o paciente toma por conta própria), exames em linguagem simples, protocolos
  de atenção primária, resumo e anamnese por queixa. Smart Apps funcionam como plug-ins
  ([guia](docs/guias/como-criar-um-smart-app.md)).
- Pedidos e prescrições em rascunho, com item crítico bloqueado sem justificativa.
- Tradução com voz, assistentes por especialidade e modelos de nota.
- Revisão com atestação e envio da nota aprovada ao prontuário como `DocumentReference` (FHIR).
- **Centro cirúrgico por ditado**: 15 modelos de procedimento, verificações de Cirurgia Segura da OMS, lateralidade,
  contagem, garrote e profilaxia por peso, OPME em três vias (fala, guia autorizada, etiqueta GS1) e anti-glosa com
  score de prontidão.
- Privacidade: consentimento, criptografia em repouso, expurgo da transcrição e exclusão pelo titular.

**Jornada do paciente**: tela `/jornada` que organiza o caminho do paciente e as pendências entre etapas
([ADR 0010](docs/decisions/0010-jornada-do-paciente.md)).

**Gestão e impacto**: auditoria de alertas críticos, overrides de alta, utilidade percebida da IA e telas de gestão e impacto.

## Arquitetura

```
apps/web (Next.js 15)  ──REST──►  apps/api (FastAPI)  ──lê──►  HAPI FHIR R4 (sintético)
  microfone / STT ──────────────►    │  fhir_client · timeline · handoff · medrec · discharge · consult · journey · llm · audit
                                     ├──grava──► PostgreSQL (resumos, alertas, scores, overrides, consultas, auditoria)
                                     └──envia──► DocumentReference (só a nota aprovada, EHR_WRITEBACK)
```

- **API**: FastAPI + SQLAlchemy/Alembic, SDK `anthropic`. Código em `apps/api/app` (módulos `handoff`, `medrec`, `discharge`,
  `consult`, `journey`, `timeline`, `fhir_client`, `llm`, `audit`, `auth`, `gestao`, entre outros).
- **Web**: Next.js 15 + Tailwind 4 + componentes shadcn/ui (Radix). Rotas em `apps/web/app` (`leitos`, `pacientes`,
  `consultas`, `jornada`, `auditoria`, `gestao`, `impacto`).
- **Dados**: PostgreSQL para o estado do HousIA; HAPI FHIR R4 como prontuário sintético. O HousIA **só lê** do FHIR e
  grava no próprio banco; a única escrita no FHIR é a nota aprovada (e itens da jornada), controlada por `EHR_WRITEBACK`.
- **`LLM_MODE`**: `mock` devolve fixtures determinísticas e roda sem chave; `real` chama o Claude (`ANTHROPIC_API_KEY`).
  Saída estruturada com JSON Schema, uma tentativa de reparo, fallback em recusas
  ([ADR 0005](docs/decisions/0005-llm-saida-estruturada-e-mock.md)).
- **Timeline clínica normalizada** (`app/timeline`): todo pilar consome a mesma lista de eventos, cada um com `source_ref`.
- **Segurança por desenho**: pseudonimização antes do LLM, validação de referências depois (item sem fonte válida é
  descartado e auditado), prompts versionados em `apps/api/app/llm/prompts/*.md` e cache por `input_hash`.
- `data/` guarda cenários, tabelas de referência curadas e o script de carga no HAPI; `docs/` guarda ADRs e benchmark.

Decisões em [`docs/decisions/`](docs/decisions/README.md); design system em [`design-system/`](design-system/housia/MASTER.md).

## Como rodar

### Com Docker Compose

```bash
cp .env.example .env        # LLM_MODE=mock roda sem chave
docker compose up --build   # ou: make up (em segundo plano)
```

| Serviço | URL |
|---|---|
| Web (Next.js) | http://localhost:3000 |
| API (FastAPI) | http://localhost:8000 (OpenAPI em `/docs`, saúde em `/health`) |
| HAPI FHIR | http://localhost:8080/fhir |

### Na nuvem do Claude Code

O container não sobe o Docker sozinho nem confia na CA do proxy. Use o script, que repassa qualquer argumento ao
`docker compose`:

```bash
scripts/sandbox-stack.sh up -d --build
```

Ele inicia o `dockerd`, baixa as imagens base por um mirror, gera cópias dos Dockerfiles que confiam na CA do proxy
(em `/tmp/housia-sandbox`, sem alterar os arquivos versionados) e sobe db, HAPI, seed, api e web.

### Seed (dados sintéticos)

O serviço `seed` carrega no HAPI os cenários roteirizados e os pacientes de fundo, com horários ancorados no momento da
carga. A carga é idempotente. **Antes de cada demonstração, recarregue** para reancorar os horários no "agora":

```bash
make seed        # ou: docker compose run --rm seed
```

### Usuários de demonstração

Senha de todos: `housia123` (definida em `apps/api/app/auth/router.py`).

| Perfil | E-mail | Pode |
|---|---|---|
| Médico(a) | `medico@housia.dev` | tudo dos pilares, override e confirmação de alta |
| Enfermagem | `enfermeira@housia.dev` | pilares 1 a 3 e checklist (sem override nem confirmação) |
| Gestão | `gestor@housia.dev` | auditoria e métricas |

### Desenvolvimento sem Docker

```bash
make api-dev   # API com FHIR em memória (mesmos cenários), SQLite e LLM mock, porta 8000 (requer uv)
make web-dev   # Next.js em modo dev, porta 3000
```

### Usar o Claude de verdade

No `.env`: `LLM_MODE=real`, `ANTHROPIC_API_KEY` preenchida e, opcionalmente, `LLM_MODEL` (padrão `claude-opus-5-5`) e
`LLM_EFFORT` (`low` a `max`; padrão `medium`). Cada geração leva de 10 a 45 s.

### Transcrição e envio ao prontuário

- **Microfone do navegador** (Chrome/Edge): funciona sem configurar. O áudio passa pelo serviço de fala do navegador,
  então use só em demonstração. Em produção, desligue com `ALLOW_BROWSER_STT=false`.
- **Servidor**: `STT_URL` apontando para um serviço compatível com `/v1/audio/transcriptions` (Whisper on-premise ou
  provedor contratado), com `STT_API_KEY` e `STT_MODEL` se necessário. O HousIA não grava áudio.
- **Envio**: com `EHR_WRITEBACK=true` (padrão no compose, pois o HAPI é sintético) a nota aprovada vira um
  `DocumentReference`. Com `false`, o profissional copia o texto ou baixa o FHIR
  ([ADR 0009](docs/decisions/0009-consulta-com-ia.md)).

## Testes

```bash
# API (Python 3.12)
cd apps/api && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
LLM_MODE=mock FHIR_MODE=local .venv/bin/python -m pytest -q     # ou: make test
.venv/bin/ruff check app tests && .venv/bin/ruff format --check app tests

# Web
cd apps/web && npm ci && npm run typecheck && npm run lint       # make lint roda ruff + lint + typecheck

# E2E (Playwright, contra a stack rodando em :3000 e :8000)
cd apps/web && npx playwright test                                # ou: make e2e / npm run test:e2e
```

Os specs e2e ficam em `apps/web/e2e` (`pilares`, `consulta`, `melhorias`). A CI (`.github/workflows/ci.yml`) roda ruff,
pytest com LLM mock, lint, typecheck, build e Playwright.

## Variáveis de ambiente importantes

Modelo em [`.env.example`](.env.example). Nunca commite o `.env` nem coloque valores secretos em documentação.

| Variável | Para que serve |
|---|---|
| `LLM_MODE` | `mock` (determinístico, sem chave) ou `real` (Claude) |
| `ANTHROPIC_API_KEY`, `LLM_MODEL`, `LLM_EFFORT` | Chave, modelo e profundidade de raciocínio no modo `real` |
| `FHIR_MODE`, `FHIR_BASE_URL` | `hapi` lê o HAPI FHIR; `local` monta os cenários em memória (testes e dev) |
| `DATABASE_URL`, `JWT_SECRET` | Banco e segredo de sessão (troque o `JWT_SECRET` por um valor longo e próprio) |
| `DATA_ENCRYPTION_KEY` | Uma ou mais chaves `id:segredo`, separadas por vírgula. A primeira cifra e todas decifram (permite rotação). Colunas clínicas ficam cifradas em repouso com AES-GCM. Vazia: gravadas sem criptografia |
| `TRANSCRIPT_RETENTION_DAYS` | Expurgo da transcrição: `90` vale para todos os tipos; `consulta=90,visita=30,cirurgia=365` define por tipo. Vazio: não expurga |
| `ALLOW_BROWSER_STT` | `false` impede que o áudio vá ao fabricante do navegador e esconde o modo Microfone (sobram servidor, digitação e simulação). Padrão `true` |
| `STT_URL`, `STT_API_KEY`, `STT_MODEL` | Transcrição no servidor (opcional) |
| `EHR_WRITEBACK` | Envia a nota aprovada ao prontuário. Desligue em produção |
| `LOG_PROMPTS` | `false` por padrão: prompts não vão para o log |
| `NEXT_PUBLIC_API_URL` | URL da API vista pelo navegador |

Para cifrar o que ficou em texto puro ou recifrar após trocar a chave: `python -m app.db.rekey` (também roda na
inicialização), dentro de `apps/api`.

## Privacidade e segurança clínica

- **Somente dados sintéticos.** Nenhum dado real de paciente é usado ou deve ser carregado.
- **Pseudonimização** (nome, CPF, telefone, endereço, familiares) antes de qualquer envio ao LLM.
- **Fonte em cada item**: referências validadas depois da geração; item sem fonte válida é descartado e auditado.
- **Regras antes da IA**: alertas críticos (alergia, interação, alto alerta) são determinísticos; hipóteses da IA ficam
  rotuladas e separadas.
- **Trava suave com responsabilidade**: alta de alto risco e itens críticos só avançam com justificativa registrada.
- **Humano no circuito**: a nota só vai ao prontuário depois de revisada e aprovada pelo profissional.
- **LGPD**: criptografia de campo em repouso, expurgo configurável da transcrição, exclusão pelo titular, consentimento
  da consulta, `audit_log` de gerações, visualizações, overrides e alertas (sem texto clínico bruto), sem gravação de áudio.
- Para uso real seriam necessários: enquadramento ANVISA (SaMD, RDC 657/2022), RIPD/DPIA, contrato de operador com o
  provedor de LLM e validação clínica prospectiva ([ADR 0004](docs/decisions/0004-regulatorio-e-lgpd.md)).

## Roteiro de demonstração (resumo)

Rode `make seed` antes. Entre como `medico@housia.dev` (senha `housia123`).

1. **Painel de leitos**: clique em "Só cenários da demo".
2. **Passagem de plantão** (Antônio): gere para Médico(a) e depois para Enfermagem; clique em "ver fonte"; cole um relato
   verbal e use "Conferir com o prontuário".
3. **Reconciliação** (Maria e Iracema): omissão de metoprolol com hipótese de retirada; alergia a dipirona e morfina + diazepam.
4. **Alta** (Sebastião): checklist, LACE + social, trava, override com 20+ caracteres, documentos de alta.
5. **Consulta com IA** (Joana): menu Consultas, iniciar, simular consulta, finalizar, aprovar e enviar ao prontuário.
   Centro cirúrgico com Lucas e o ditado do relatório.
6. **Auditoria**: entre como `gestor@housia.dev`.

## Roadmap

Plano completo em [`docs/paridade-lya/backlog.json`](docs/paridade-lya/backlog.json); estado atual e como retomar em
[`docs/paridade-lya/STATUS.md`](docs/paridade-lya/STATUS.md).

**Entregue**: os 3 pilares, auditoria, Consulta com IA, centro cirúrgico, OPME e anti-glosa, jornada do paciente (#30),
CID-10 e diagnósticos diferenciais (#5, #6), pedidos e prescrições (#19), tradução (#22), privacidade (#31) e Smart Apps
como plug-ins (#21), entre outros itens das Ondas 1 e 2.

**Em andamento**: #32 (implementação parcial ainda não verificada).

**O que falta**
- **Resto da Onda 3**:
  - Frente A: #27 pacotes de especialidade, #28 protocolos de APS, #17 envio estruturado ao prontuário (Bundle FHIR),
    #18 codificação (CID-10, CIAP-2, SIGTAP), #29 "Antes de encaminhar".
  - Frente B: #33 autocadastro e convite de equipe, #34 modo consultório, #35 planos e teste grátis.
- **Onda 4**: #11 (Whisper on-premise com streaming), #12 (captura em celular e tablet), depois os itens P1 (#38 a #54) e P2 (#55 a #57).
- **Pitch**: refazer o vídeo com a Consulta com IA como foco e atualizar o material de divulgação.
- **Antes de rodar de novo**: confirmar o pytest e rodar os e2e, que não foram executados depois de #22 e #31.

**Fora de escopo** (dependem de terceiros ou de licença): tabelas TUSS/CBHPM, conectores Tasy/MV, envio real à
RNDS/e-SUS, assinatura ICP-Brasil, gateway de pagamento, SMS/WhatsApp, validação de CRM/COREN e registro ANVISA/ISO.
Detalhes em `plano.fora_de_escopo` do `backlog.json`.

## Aviso final

O HousIA é uma demonstração com dados sintéticos. Não é dispositivo médico, não tem registro na ANVISA e não deve
orientar decisões clínicas reais. A responsabilidade clínica é sempre do profissional.
