# Paridade com a Lya Health: status e como continuar

Atualizado em 16/10/2026 (fim da tarde). Branch de trabalho: `claude/dreamy-pascal-x45x7r`.

O objetivo é fazer **tudo o que a Lya faz, melhor**, mantendo os diferenciais do HousIA: passagem de plantão,
reconciliação na transferência, Waze da alta e fonte em cada item. O plano completo está em
[`backlog.json`](backlog.json). Ele tem três partes:

- `inventario`: 84 funções da Lya e do mercado;
- `avaliacao`: o estado de cada função no HousIA, conferido por revisores céticos;
- `plano.backlog`: 58 itens, de 0 a 57. Cada item traz `o_que_fazer`, `criterios_de_aceite` e `melhor_que_lya`.

Para retomar numa sessão nova, use o prompt de [`PROXIMA-SESSAO.md`](PROXIMA-SESSAO.md).

Comparação item a item: [`../benchmark-lya.md`](../benchmark-lya.md). Decisões de arquitetura:
[`../decisions/0009-consulta-com-ia.md`](../decisions/0009-consulta-com-ia.md).

## Já feito

- MVP: os 3 pilares, auditoria e e2e.
- Consulta com IA:
  - transcrição ao vivo (microfone, servidor, digitar ou roteiro);
  - Smart Apps em tempo real (perguntas, alertas, queixas, emoções, resumo, hipóteses, anamnese, cirurgia,
    medicamentos, alergias, histórico, exames, protocolos);
  - nota revisada enviada ao prontuário (DocumentReference);
  - tradução, especialidades e centro cirúrgico básico.
- Onda 1, parte entregue:
  - **#1** Regras que aguentam fala real: falante, negação e frases sem pontuação. Inclui o corpus
    `data/consultations/regressao_falas.json`, com 109 frases, e `tests/test_consult_regressao.py`.
  - **#23** Centro cirúrgico por modelos: `data/reference/surgical_templates/*.yaml`, com 15 procedimentos em 7
    especialidades, campos de radioscopia, RX de controle proposto e os roteiros cen4b (artroplastia) e cen4c
    (cesárea).
  - **#24** Verificações cirúrgicas em `app/consult/surgical_checks.py`:
    - Cirurgia Segura da OMS;
    - consentimento;
    - lateralidade × `bodySite`;
    - contagem;
    - garrote;
    - profilaxia por peso, com janela e redose (`surgical_prophylaxis.csv`).

    A equipe de sala (anestesista, circulante, instrumentador) usa Alt+4/5/6.
- Onda 1, parte entregue (continuação):
  - **#23/#24 reconferidos** (commit `58a7c68`): todos os critérios ok. Só a equipe registra etapa, lado e dose
    (antes, fala de paciente mudava o modelo e gerava lateralidade crítica falsa), lado negado e antecedente com ano
    não contam, microfone do modo cirúrgico começa em "Profissional".
  - **#4** Perguntas sugeridas que acompanham qualquer diálogo: 66 sintomas, 198 perguntas com fonte, idade e sexo;
    "já abordada" só com fala do profissional (inclusive parafraseada), com a resposta do paciente; perguntas que
    separam as 2 hipóteses de maior confiança sobem na ordem.
  - **#9** Anamnese por queixa (`app/consult/semiology.py`, `data/reference/consult_semiology.yaml`): campos
    semiológicos com fonte, HDA redigida no modo regras, tags com os atributos e queixa principal que o médico fixa.
    **A verificação independente do #4+#9 parou no meio por limite de uso.** O trabalho parcial dela está em
    `docs/paridade-lya/wip/verificacao-4-9-parcial.patch` (não aplicado: deixa 1 teste falhando,
    `test_posologia_e_inicio_da_noite_nao_viram_frequencia_nem_piora_da_queixa`).
  - **#25** OPME em três vias (fala × guia autorizada × etiqueta GS1), rastreio do implante (Device,
    Procedure.usedReference, cartão do implante na alta, implante na passagem). Verificado (commit `a4e4e43`).
  - **#26** Anti-glosa em dados (`data/reference/glosa_rules/`), score de prontidão, Claim em rascunho, trava 409
    `prontidao_critica` no envio sem justificativa. Verificado. Audit da justificativa por linha de OPME com texto e
    regra (`7da3c4c`).
  - **#30** Waze da jornada (`app/journey/`, tela `/jornada`, ADR 0010, migração `d001`, cenários cen7..cen7e e
    coorte alt01..alt18). **Implementado, mas a verificação independente não terminou** (parou por limite de uso,
    sem commits).
  - Frente B juntada no branch principal no merge `5cd7e6f`. pytest completo verde depois do merge.
- Testes: pytest verde em `5cd7e6f` (1 xfail estrito do corpus); ruff, typecheck e lint limpos. e2e (9/9) rodados e
  verdes em `11262f4`, **ainda não rodados depois do #4, #9, #25, #26 e #30**.

### Limitações conhecidas que ficaram da Onda 1

- Pergunta curta do médico sem pronome e sem "?" ("tem febre") ainda é atribuída ao paciente. As que retomam o assunto ("e a febre", "e dor no peito") e as em 3ª pessoa ("o ar tem faltado quando caminha") já são reconhecidas.
- Acompanhante sem vocativo ("meu filho tá com febre") é inferido como paciente: o lado está certo, o papel não.
- Falso positivo gerado pela própria IA, por exemplo um alerta sobre medicamento negado, só é contido pelo prompt.
  O `merge_live` descarta apenas refs inválidas.
- Os campos novos da saída estruturada da IA (`perguntas_abordadas`, `AiAnamnese`) só foram testados em mock: o
  container não tinha `ANTHROPIC_API_KEY` no ambiente.
- OPME: câmera só via `BarcodeDetector` (sem zxing); XML TISS lido de forma tolerante, sem XSD; tabelas de
  procedimento são de demonstração (DEMO-*).
- Jornada: a lista varre todos os pacientes a cada chamada (~1,5 s com 54 pacientes no modo local); Task e
  DocumentReference da jornada são gravados no FHIR mesmo com `EHR_WRITEBACK` desligado (decisão no ADR 0010).

## O que falta, na ordem

1. **Fechar as verificações interrompidas:**
   - #4+#9: aplicar ou descartar `docs/paridade-lya/wip/verificacao-4-9-parcial.patch`
     (`git apply docs/paridade-lya/wip/verificacao-4-9-parcial.patch`), fazer passar o teste que falha, rodar a
     verificação cética (ver `.claude/workflows/housia-onda.js`, `verifyPrompt`, base `58a7c68`) e apagar a pasta `wip/`.
   - #30: verificação cética (base `a4e4e43`, commits `7da3c4c`, `c6b7abe`, `09d6bd6`).
   - Subir a stack, recarregar o seed e rodar os e2e; corrigir o que quebrar.
2. **Resto da Onda 1, frente A, em sequência:** #5+#6 (CID-10 oficial validado e medicamentos e alergias de qualquer
   falante), #7+#8 (histórico e exames ditos e resumo com fonte por frase e lembretes), #10 (relevância de cada fala
   e o painel "fora da nota").
3. **Onda 2.**
   - Frente A: #2+#13 (transcrição editável e consentimento), #3 (aceitar, descartar e fixar insights), #14+#15
     (revisão segura e cronologia), #16 (modelos de nota, SOAP e-SUS), #22 (tradução em 95+ idiomas).
   - Frente B: #19 (pedidos em rascunho), #20 (copiloto), #36+#37 (painel Impacto e copiloto do gestor).
4. **Onda 3.**
   - Frente A: #21 (Smart Apps como plug-ins), #27 (pacotes de especialidade), #28 (protocolos de APS), #17
     (Bundle FHIR), #18 (codificação), #29 ("Antes de encaminhar").
   - Frente B: #31 (privacidade), #32 (prontidão de produção), #33 (organizações), #34 (consultório), #35 (planos).
5. **Onda 4:** #11 (Whisper on-premise com streaming), #12 (PWA e tablet), depois os P1 (#38 a #54) e os P2 (#55 a
   #57).
6. **Pitch:** refazer o vídeo (#0) com a Consulta com IA como foco. Precisa mostrar a transcrição ao vivo, a pergunta
   sugerida saindo da lista quando é feita, o alerta crítico com o chip "fala N", a nota indo ao FHIR e a ponte com
   a passagem e a alta. Os scripts do vídeo estão em `brag-output/scripts/`; `capture-consult.mjs` captura a
   consulta real. Também atualizar `brag-output/share-copy.txt`, o texto do login (`login-form.tsx`), a
   `description` em `app/layout.tsx` e o topo do README.
7. **Se sobrar tempo:** polir o front com as skills de UI mais usadas.

Fora de escopo, porque depende de terceiros ou de licença (detalhes em `backlog.json`, `plano.fora_de_escopo`):

- TUSS/CBHPM;
- conectores Tasy/MV;
- RNDS/e-SUS em produção;
- assinatura ICP-Brasil;
- gateway de pagamento;
- SMS/WhatsApp;
- validação CRM/COREN;
- ANVISA/ISO.

## Como preparar o ambiente

```bash
# API (Python 3.12)
cd apps/api && python3 -m venv .venv && .venv/bin/pip install -q -e ".[dev]"
LLM_MODE=mock FHIR_MODE=local .venv/bin/python -m pytest -q
.venv/bin/ruff check app tests && .venv/bin/ruff format --check app tests

# Web
cd apps/web && npm ci && npm run typecheck && npm run lint

# Stack completa + e2e
docker compose up -d --build        # db, fhir (HAPI), seed, api, web
cd apps/web && npx playwright test  # e2e em apps/web/e2e
```

Na nuvem do Claude Code, use `scripts/sandbox-stack.sh up -d --build`. Ele sobe o `dockerd`, puxa as imagens
base pelo `mirror.gcr.io` (o Docker Hub devolve 429), gera Dockerfiles temporários que confiam na CA do proxy e roda o
compose com `DOCKER_BUILDKIT=0`. Espere o container `housia-seed-1` sair com código 0 e rode os e2e com
`PLAYWRIGHT_CHROMIUM_PATH=/opt/pw-browsers/chromium npx playwright test --reporter=line`. Para recarregar o seed:
`scripts/sandbox-stack.sh up -d --build seed api web`.

A IA real precisa de `ANTHROPIC_API_KEY` e `LLM_MODE=real` no ambiente da API. A chave fica nos segredos do ambiente
e **nunca** vai para o repositório.

## Como executar uma onda

O workflow genérico está em `.claude/workflows/housia-onda.js`. Ele roda duas frentes em paralelo:

- a frente A no repositório principal;
- a frente B num `git worktree` com `apps/web/node_modules` linkado.

Cada entrega passa por um agente que implementa e por um agente que verifica de forma independente. Chame o
Workflow com `name: "housia-onda"` e args como:

```json
{
  "repo": "/home/user/housIA",
  "wt": "/home/user/wt-onda",
  "revA": "c0",
  "revB": "d0",
  "attribution": "<as linhas de atribuição de commit da sessão>",
  "laneA": [{ "key": "A1-perguntas-semiologia", "items": [4, 9], "notes": "..." }],
  "laneB": [{ "key": "B1-opme-antiglosa", "items": [25, 26], "notes": "..." }]
}
```

Antes de chamar, crie o worktree:

```bash
git worktree add /home/user/wt-onda -b onda-b
ln -s "$PWD/apps/web/node_modules" /home/user/wt-onda/apps/web/node_modules
```

Ao terminar:

- faça o merge da frente B;
- se houver duas heads no Alembic, crie uma migração de merge;
- rode tudo e faça o push.

Cada onda com 8 entregas consome bastante da cota de uso. Rode uma onda por janela de uso, ou só uma frente.

## Regras do projeto

- Nunca commitar chaves. Antes de cada commit, `git grep -n "sk-ant"` deve sair vazio.
- Só dados sintéticos.
- Push só no branch de trabalho, sem abrir PR sem pedido.
- Texto da interface em português do Brasil com acentos.
- Toda afirmação da IA leva fonte (`Fala/N` e/ou referência FHIR). As regras de segurança nunca são removidas pela
  IA.
