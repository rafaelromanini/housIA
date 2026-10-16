# Paridade com a Lya Health: status e como continuar

Atualizado em 16/10/2026. Branch de trabalho: `claude/dreamy-pascal-x45x7r`.

O objetivo é fazer **tudo o que a Lya faz, melhor**, mantendo os diferenciais do HousIA: passagem de plantão,
reconciliação na transferência, Waze da alta e fonte em cada item. O plano completo está em
[`backlog.json`](backlog.json). Ele tem três partes:

- `inventario`: 84 funções da Lya e do mercado;
- `avaliacao`: o estado de cada função no HousIA, conferido por revisores céticos;
- `plano.backlog`: 58 itens, de 0 a 57. Cada item traz `o_que_fazer`, `criterios_de_aceite` e `melhor_que_lya`.

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
- Testes: todos os testes do pytest passam (2 xfail estritos, que são limitações documentadas do corpus); ruff,
  typecheck e lint limpos. **O e2e (Playwright) ainda não foi rodado depois da Onda 1.**

### Limitações conhecidas que ficaram da Onda 1

- Pergunta curta do médico, sem pronome e sem "?" ("e a febre", "tem febre"), ainda é atribuída ao paciente.
- Acompanhante sem vocativo ("meu filho tá com febre") é inferido como paciente: o lado está certo, o papel não.
- Falso positivo gerado pela própria IA, por exemplo um alerta sobre medicamento negado, só é contido pelo prompt.
  O `merge_live` descarta apenas refs inválidas.
- A verificação independente do #23/#24 parou no meio por limite de uso. As correções que ela já tinha feito estão no
  commit `1de2b29` e os testes passam, mas vale reconferir os critérios de aceite do #23 e do #24.

## O que falta, na ordem

1. **Conferir o que já foi entregue:** reconstruir as imagens, recarregar o seed e rodar os e2e (veja abaixo).
   Corrigir o que quebrar.
2. **Resto da Onda 1.**
   - Frente A, repositório principal, em sequência: #4+#9 (perguntas para qualquer diálogo e semiologia), #5+#6
     (CID-10 oficial validado e medicamentos e alergias de qualquer falante), #7+#8 (histórico e exames ditos e
     resumo com fonte por frase e lembretes), #10 (relevância de cada fala e o painel "fora da nota").
   - Frente B, worktree: #25+#26 (OPME em três vias e anti-glosa), #30 (Waze da jornada).
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

Na nuvem do Claude Code, se o build do Docker falhar:

- **Erro de TLS:** copie a CA do proxy para `apps/api/.sandbox-ca.crt` e `apps/web/.sandbox-ca.crt`. Esses arquivos
  são ignorados pelo git. Depois use um Dockerfile que a confie.
- **Erro 429 do Docker Hub:** use `DOCKER_BUILDKIT=0`.

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
