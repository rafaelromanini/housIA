# Paridade com a Lya Health: status e como continuar

Atualizado em 16/10/2026 (noite, pausa por cota de uso em 90%). Branch de trabalho: `claude/dreamy-pascal-x45x7r`.

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
    Verificação cética concluída (patch aplicado, remédio e queixa na mesma frase, "dor de barriga").
  - **#25** OPME em três vias (fala × guia autorizada × etiqueta GS1), rastreio do implante (Device,
    Procedure.usedReference, cartão do implante na alta, implante na passagem). Verificado (commit `a4e4e43`).
  - **#26** Anti-glosa em dados (`data/reference/glosa_rules/`), score de prontidão, Claim em rascunho, trava 409
    `prontidao_critica` no envio sem justificativa. Verificado. Audit da justificativa por linha de OPME com texto e
    regra (`7da3c4c`).
  - **#30** Waze da jornada (`app/journey/`, tela `/jornada`, ADR 0010, migração `d001`, cenários cen7..cen7e e
    coorte alt01..alt18). Verificado depois (ver abaixo,
    `3cc1205`).
  - Frente B juntada no branch principal no merge `5cd7e6f`. pytest completo verde depois do merge.
- Sessão de 16/10 à noite (tudo verificado, juntado e com push; head `f630fc4`):
  - **#4+#9 verificados** (`5e665ea`): patch parcial aplicado, teste de posologia corrigido, frase com remédio e
    queixa dividida em "e/mas/porém", "dor de barriga" como sinônimo. `wip/` apagada.
  - **#30 verificado** (`3cc1205`): 5 critérios ok, 13 testes adversariais, sem bug.
  - **#5+#6** (`81a136d` + verificação `fda739b`): `cid10.csv` com 14.233 códigos (DATASUS 2008 via pacote npm
    MIT, `scripts/build_cid10.py`), 61 diagnósticos diferenciais com fonte e evidência contra, medicamentos e
    alergias de qualquer falante, marcas (169), efeito adverso por data de início, decisão de hipótese
    (`PUT /consultations/{id}/hipoteses`). Verificação corrigiu: CID "confiável" abusável pela IA, alergia perdida
    em fala corrida, alergia de familiar virando do paciente, J21.9 trocado com J20.9.
  - **#19** pedidos e prescrições em rascunho (`6dcbd31`, verificação `661f2a2`, `d564ba0`): `app/consult/orders.py`,
    `lab_catalog.csv` (26 exames LOINC), ServiceRequest/MedicationRequest draft+proposal só do que foi assinado,
    item crítico só com justificativa, item assinado volta a pendente se surgir alerta crítico, remédio fora do
    catálogo vira rascunho "não reconhecido", cefalexina/cefadroxila em alérgico a penicilina = crítico.
  - Merges das frentes: `016580f`, `2e108c8`, `f630fc4` (prompt `consult_live_clinica` em 1.5.0).
  - pytest, ruff, typecheck e lint verdes em `f630fc4`. e2e 9/9 verdes em `016580f` (antes do #5+#6 e #19:
    **rodar de novo** no fim da Onda 1).
- Testes (antes desta sessão): pytest verde em `5cd7e6f` (1 xfail estrito do corpus); ruff, typecheck e lint limpos. e2e (9/9) rodados e
  verdes em `11262f4`, **ainda não rodados depois do #4, #9, #25, #26 e #30**.

### Limitações conhecidas que ficaram da Onda 1

- Pedidos (#19): remédio fora de `drugs.csv` não tem checagem automática (só o aviso "não reconhecido"); "insulina"
  sem tipo não ganha selo de alto alerta; doses máximas só as de `dose_alerts.csv`; reconciliação só para internado.
- CID (#5): tabela de 2008; códigos mais novos (U07.1, N18.x) só passam se já estiverem no prontuário. OMA só até 12
  anos. Sem hipótese de HSA.
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

Feito e verificado até aqui: Ondas 1 e 2, #22, #31, #21 e #32. pytest 1014 passed/1 xfail, ruff, typecheck e lint
limpos, e2e 16/16. Cabeça do Alembic: c201. Próximas etapas em branches separados a partir da main (um PR por entrega;
ao criar migração, encadeie na cabeça da main para não gerar duas cabeças).

4. **Resto da Onda 3.** Frente A: #27, #28, #17, #18, #29 (c2). Frente B: #33, #34, #35 (d4).
5. **Onda 4:** #11, #12, depois P1 (#38 a #54) e P2 (#55 a #57).
6. **Pitch:** refazer o vídeo (#0) com a Consulta com IA como foco: transcrição ao vivo, pergunta sugerida saindo da
   lista, alerta crítico com chip "fala N", revisão com atestação, nota indo ao FHIR, ponte com passagem e alta.
   Scripts em `brag-output/scripts/` (`capture-consult.mjs`). Atualizar também `brag-output/share-copy.txt`,
   `login-form.tsx`, a `description` em `app/layout.tsx` e o topo do README.
7. **Se sobrar tempo:** polir o front.

Pendências conhecidas (não bloqueiam): AAD do AES-GCM não amarra linha/coluna (#31); sem DATA_ENCRYPTION_KEY os dados ficam em JSON puro (#31); ligação Charlson/LACE com a alta (#7); "tem alergia a dipirona?" devolve a lista
inteira (#20); a pausa não bloqueia insights/copiloto e a revogação não apaga análise já guardada (#13, decidir);
reenvio após falha parcial do FHIR pode duplicar DocumentReference (#14); "Sem registro…" no início conta como seção
vazia (#16); validador do resumo do gestor não confere número × indicador (#37); avisos de consentimento em 9 idiomas
precisam de revisão nativa/jurídica.

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

## #35 Planos e teste grátis (2026-10-21)

- Tabelas `plans` e `entitlements` (migração e351, sobre e341, cabeça única). Planos: teste (10 consultas), profissional,
  clínica (plantão e reconciliação) e hospital (todos os módulos, incluindo alta). Consultório nasce no teste; hospital
  no plano hospitalar. Direito de uso criado na primeira leitura.
- Contagem por `consulta.iniciada` da organização, sem sandbox; a 11ª devolve 402; sandbox sem limite; aviso a partir
  da 8ª; `/consult/plano` e `/auth/me` trazem o mesmo contador da agenda. Troca manual em `PUT /orgs/{id}/plano`, só
  para o administrador da plataforma (gestor da organização padrão); vigência sem fuso vale UTC; limite estrito inteiro.
- Módulos de plantão, reconciliação e alta respondem 403 fora do plano, no backend e no menu. Rebaixar o plano também
  esconde o risco de alta antigo no painel e na ficha.
- Verificação cética achou e corrigiu: corrida na 10ª/11ª consulta (lock + FOR UPDATE), vazamento de `risco_alta`,
  vigência sem fuso (500), limite booleano aceito. Teste de acesso cruzado multiorg: 404 exato restaurado (alta exige
  o plano hospitalar ativado na organização B) e `{escala}` preenchido.
- Decisão: consultório sem o módulo de alta recebe 403 (plano) antes do 404 de organização. O limite de 30 do #34 caiu
  para 10 (briefing). Pendente: cobrança automática (só ativação manual) e e2e do contador.
## #17 bundle FHIR e envio ao prontuário (2026-10-21)

Branch `claude/onda3-17-bundle-fhir-prontuario`, já mesclado com a main (migração c202 sobre e341, cabeça única).
Bundle transaction estruturado, propostas revisáveis, fila `ehr_outbox` (cifrada, isolada por organização) e reenvio
idempotente (If-None-Exist). Verificação de esforço alto concluída: regressão nos rótulos de auditoria corrigida,
critérios de aceite cobertos por testes e entradas adversariais conferidas. Segue o resultado da suíte completa no
histórico de commits.

## #11 transcrição no servidor em fluxo (2026-10-21)

Branch `claude/onda3-11-whisper-servidor`. Sem migração. Decisões:
- WebSocket `/ws/consultations/{cid}/audio` (autenticação por Bearer ou cookie de sessão com Origin permitido; o guard
  global de paciente avulso passou a aceitar `HTTPConnection`, senão quebrava rotas WebSocket). Mensagens `pronto`,
  `parcial`, `final` (com `novos` e `insights`, via `append_segments` com as mesmas travas de consentimento/pausa do
  POST `/audio`), `erro`, `fim`. O POST `/audio` de 8 s segue como alternativa.
- `app/consult/stt_stream.py`: `Sessao` com VAD por energia (silêncio fecha a frase), janela deslizante (8 s, 1 s de
  sobreposição: só fecha palavra que termina antes da sobreposição, o resto é retranscrito) e deduplicação pelo ponto
  médio das palavras. Motor plugável: `FakeMotor` (tons por palavra, determinístico; ligado por `STT_ENGINE=fake`, usado
  nos testes) e `HttpMotor` (faster-whisper-server via `stt.transcribe_words`, que lê timestamps por palavra e
  `avg_logprob`). Nenhum peso é baixado pelo repositório; o contêiner `stt` baixa o modelo na primeira subida.
- O WAV de teste (`tests/fixtures/fala_sintetica_glibenclamida.wav`) é sintetizado por tons, não por TTS: não há TTS
  offline disponível sem baixar modelo. Exercita janela, VAD e deduplicação, não a acurácia de um Whisper real.
- Compose: serviço `stt` no perfil `stt` (imagem de tag fixa, modelo pt `small` em CPU; GPU/large-v3 por variáveis) e
  `STT_PROBE=true`: o `/consult/config` só anuncia `stt_servidor` quando o serviço responde, então `--profile stt up`
  liga o modo sem configurar mais nada. Helm: `stt.url` opcional (o chart não sobe o Whisper).
- Front: hook `useServerStream` (AudioWorklet em Blob, 16 kHz, blocos de 500 ms), parcial no transcript, "Servidor" é o
  modo padrão quando `stt_servidor`. Não validado em navegador real (sem playwright/microfone aqui).
- Verificação independente (esforço alto, privacidade): corrigido o que segue, todos com teste em
  `tests/test_consult_stt_stream.py`. (1) Consentimento revogado, pausa ou consulta finalizada só eram checados ao
  fechar frase: com `HttpMotor` o áudio já seguiria ao serviço de fala; agora cada bloco revalida antes de transcrever
  (`stream_context` também recusa consulta finalizada). (2) Token expirando no meio da transmissão encerra a conexão.
  (3) Mensagem de controle JSON que não é objeto, ou com `falante` não-string (lista/objeto), levantava exceção;
  agora é ignorada. (4) Tetos: texto de controle 4 KB, bloco 256 KB, 2 h de áudio por conexão. (5) Queda do serviço
  de fala durante a transmissão vira mensagem `erro` e fechamento limpo. Cobertos sem mudança de código: consulta de
  outra organização (resposta idêntica a id inexistente, nada gravado), token expirado/forjado/alg none/usuário
  inexistente, cookie com Origin cruzada/`null`/prefixo, nada em disco, log ou auditoria (só bytes, frases, motor),
  guard de paciente avulso nas rotas HTTP.
- Pendente: validar a imagem do faster-whisper-server (tag e variáveis `WHISPER__*`) e a latência em hardware real;
  VAD do serviço (`vad_filter`) não é pedido, o corte é feito pelo VAD de energia do HousIA; `avg_logprob` é lido mas
  ainda não usado para marcar baixa confiança na tela.
## #18 codificação CID-10, CIAP-2 e SIGTAP (2026-10-21)

Branch `claude/onda3-18-codificacao-cid-ciap-sigtap`, sem migração. Novo `app/consult/coding.py`. Decisões:

- **Secundário**: só a comorbidade que alguém citou (fala afirmativa, sem pergunta, negação, hipótese "se for" nem
  familiar) ou que muda a conduta (o `Condition/id` aparece em alerta, hipótese, protocolo ou pedido). A nota mock do cen6
  deixa de listar dislipidemia, obesidade e catarata. Secundário proposto pela IA sem `Fala/N` e fora desse conjunto vai
  para `descartados`.
- **Especificidade**: `cid_especificidade.csv` (lateralidade, agudo × crônico, complicação), a primeira regra que casa
  numa fala afirmativa; sem regra casada e com o CID genérico, vira lacuna. "Sem perfuração" sugere K35.8, que o DATASUS
  2008 não tem: `cid10_revisoes_oms.csv` acrescenta K35.2, K35.3 e K35.8 (revisões da OMS) à tabela.
- **Sexo e idade**: sexo pela `cid10.csv`; idade por `cid_idade.csv` (curadoria: capítulos P e O). Alerta nos insights
  (diagnóstico do prontuário) e pendência na nota.
- **CIAP-2**: `ciap2.csv` e `ciap2_cid10_map.csv` são curadoria de demonstração (conferir os termos de uso da WONCA e
  trocar pela tabela do e-SUS APS antes de distribuir). Aparece em cada CID quando o modelo da nota tem a seção CIAP-2.
- **SIGTAP**: `sigtap_procedimentos.csv` e `sigtap_proc_cid.csv`, gerados por `scripts/build_sigtap.py`. Os códigos são
  DEMO-SIGTAP-*; uma competência real do DATASUS entra pelo mesmo script (`--dir`, `--competencia`, `--mapa`; layout
  conferido só pela documentação, não rodado com arquivos reais). CID principal fora da relação, ou sexo/idade fora do
  procedimento, vira pendência `sigtap` (recalculada na revisão).
- **Bundle e alta**: cada CID confirmado e aceito liga ao encontro por FHIR Patch (`Encounter.diagnosis`); hipóteses
  ficam só como Condition. O `LocalFhirStore` aplica o patch; o PATCH com `urn:uuid` no HAPI real não foi testado. O
  Charlson/LACE deixa de contar Condition provisória, diferencial ou refutada.
- **Verificação independente (2026-10-21)**: códigos CID-10 e CIAP-2 conferidos um a um (todos reais; mapeamentos
  plausíveis). Os dois testes antigos alterados não perdem cobertura (K35.5 não existe em tabela alguma; o teste de
  expurgo ainda exige a nota igual, menos a `frase`). Achados e correções: (1) **privacidade**: a `frase` literal do CID
  sobrevivia ao expurgo em `extra_json` (`nota_original`, `versoes_nota`), `insights_json` e `ai_meta_json`; agora todas
  as cópias passam por `_scrub_frases` (teste novo). (2) "sem abscesso e com peritonite generalizada" fechava K35.8 e
  "sem complicações, mas tenho albuminúria" perdia E11.2: o que o ditado afirma agora vale mais que "sem X". (3) "não há
  sinais de nefropatia", "colecistite aguda descartada" e "não dói nos dois joelhos" refinavam o código; agora são
  negação. (4) Na especificidade, fala com pergunta e resposta ("Tem abscesso? Não. É apendicite sem perfuração.") usa as
  frases afirmativas; para o secundário, fala com pergunta continua sem afirmar nada (a catarata do cen6 segue fora). (5) Interface: o editor de CID mostra frase, mais específico, lacunas e CIAP-2.
- Pendente: o contrato de saída da IA ganhou campos opcionais em `CidSuggestion` (sem teste com o LLM real); CIAP-2 e
  SIGTAP completos dependem de licença/tabela oficial; a interface ainda não mostra `procedimento_sus` (a pendência
  `sigtap` aparece na lista de pendências); Condition provisória fora do Charlson/LACE é decisão de projeto (pode
  subestimar o risco se o prontuário tiver comorbidade só provisória).
## #29 Antes de encaminhar (2026-10-21)

Branch `claude/onda3-29-antes-de-encaminhar`, sem migração (decisões em `extra_json["encaminhamentos"]`, igual às
hipóteses e aos pedidos). Base `data/reference/referral_criteria.csv` (14 linhas, 12 especialidades; a fonte é o
protocolo do Ministério da Saúde/Sírio-Libanês e do TelessaúdeRS, em resumo: a instituição confere a edição vigente).
Decisões:

- Dois gatilhos: o profissional diz que vai encaminhar (só o médico; negação, condição, pergunta e relato do paciente
  não valem) e a especialidade tem uma linha que vale para o paciente (CID no prontuário, hipótese ou tema dito); ou
  uma hipótese já cumpre ao menos um critério objetivo. Condição sem linha na base não abre card.
- Critério com três estados: atendido, não atendido, a confirmar (exame físico, resposta ao tratamento ou dado ausente).
- Exames mínimos reaproveitam `chart._protocol_record` (lab no FHIR, texto em nota ou procedimento), com validade em
  dias: exame antigo conta como faltante ("desatualizado").
- "Encaminhar" com exame mínimo faltando exige justificativa de 10 caracteres ou mais (409 `exames_faltando`);
  "Manejar na APS" não exige. As duas escolhas vão para `consulta.encaminhamento` na auditoria.
- ServiceRequest `draft`/`proposal` com justificativa estruturada e `supportingInfo` só com refs FHIR (Fala/N vai na
  nota e na extensão). Só o card em "Encaminhar" vai no Bundle da exportação e do envio.
- Métricas no painel Impacto: `encaminhamentos_evitados` (manejo na APS ÷ decididos) e `encaminhamentos_completos`
  (encaminhados com todos os exames mínimos ÷ encaminhados; proxy do que a regulação devolveria). A devolução real pela
  regulação ainda não é medida.
- Verificação independente: frases de hesitação ("estou pensando em encaminhar") não abrem card; dois critérios da base
  com texto mais fiel à regra (potássio baixo e anemia) e teste de 404 entre organizações para a rota nova.
- Pendências: a escolha não entra sozinha na nota (plano/avaliação); teleconsultoria é só orientação (sem integração);
  ver a base com especialistas antes de uso real.

## Pausa (2026-10-20, cota)

- #33: branch `claude/onda3-33-multitenant` (commit 1ca67b5 = implementação, 1037 passed; commit wip = verificação
  adversarial interrompida, testes novos em `test_multiorg_adversarial.py` não conferidos). Retomar a verificação.
- #27: implementado e verificado no branch `claude/onda3-27-pacotes-especialidade` (verificação cética: peso em
  lb/g/zero/antigo, mL e concentração, dose diária x por tomada, mg/kg, adolescente com dose de adulto, NYHA/IPSS com
  negação, glossário sem trocar sentido). Pendências: percentil/escore z de IMC da OMS (tabelas LMS não embarcadas) e
  conferência humana das faixas de `pediatric_doses.csv` e dos itens do IVCF-20 contra bula/SBP/MS.

## Pausa 3 (2026-10-21, cota)

Entregue na main nesta sessão (todos com os 4 checks verdes): #7 (fix do CI: rota de escalas no teste multi-org),
#8 (#17 bundle FHIR, c202→e341), #9 (#35 planos, e351→c202), #10 (#29 antes de encaminhar), #11 (#18 codificação;
também deixou determinístico o teste de cookie adulterado em test_prontidao). Cabeça alembic da main: **e351**.

Branches no remoto, ainda sem PR:
- `claude/onda3-11-whisper-servidor` (#11): implementado + verificado (alto); main já mesclada. Falta: suíte completa,
  e2e, PR e merge.
- `claude/onda3-12-pwa-celular-tablet` (#12): implementado + verificado (alto); e2e local verde (22). Falta: merge da
  main, suíte, PR. Decisão: projetos "iPhone 13"/"iPad" rodam em Chromium com emulação (CI só instala Chromium);
  WebKit real pendente.
- `claude/onda4-38-transcricao-vocabulario` (#38, sobre o #11): implementado; verificação de esforço alto interrompida
  (commit wip NÃO verificado). Ponto central: rebaixar alerta crítico por baixa confiança do STT é seguro? (ADR 0009:
  regra de segurança nunca some; preferir manter crítico com "confirmar o que foi ouvido").
- `claude/onda4-43-sinais-comunicacao` (#43): só criado, sem implementação.

Ordem de merge: #11 → #12 → #38 → resto. Nenhuma migração nova nessas branches.

### Prompt da próxima sessão

> Continue o HousIA (rafaelromanini/housIA) de forma AUTÔNOMA a partir da main, seguindo as mesmas regras da sessão
> anterior (ver "Regras do projeto" e "Pausa 3" em docs/paridade-lya/STATUS.md): agentes Sonnet (verificação alta em
> segurança clínica/privacidade), UM agente por vez, em série (implementação, depois verificação; sem frente B nem
> worktree paralelo), uma verificação cética por entrega (verifyPrompt/common
> em .claude/workflows/housia-onda.js), um branch e um PR em português por entrega, merge (não rebase) da main antes do
> PR, merge só com API/Web/Helm/E2E verdes, nunca push na main, LLM_MODE=mock, só dados sintéticos. Antes de cada push:
> pytest -o addopts="" completo, ruff, typecheck/lint web e e2e no modo do CI. Migração nova encadeia na cabeça da main
> (hoje e351) e atualiza o golden de tests/test_journey.py. Preparação: venv apps/api/.venv, npm ci em apps/web. Ordem: (1) PR e merge de #11, #12; (2) terminar a verificação do #38 e fazer merge; (3) #43
> e o restante de P1 (#39–#42, #44–#54) e P2 (#55–#57); (4) vídeo pitch da Consulta com IA (brag-output/scripts,
> capture-consult.mjs), share-copy.txt, login-form.tsx, description em app/layout.tsx e topo do README; (5) polir o
> front. Ao chegar a 80% da cota: não inicie entrega nova; commit wip, push, atualizar este STATUS com novo prompt.
## #12 captura em celular e tablet: PWA, tela acesa, microfone e fila offline (2026-10-21)

Branch `claude/onda3-12-pwa-celular-tablet`. Sem migração (o `client_id` fica no JSON das falas) e sem dependência nova.
- PWA: `manifest.webmanifest`, ícones PNG (192/512/maskable/apple-touch), `sw.js` e `offline.html`. O service worker
  guarda só o shell estático e a página offline; não intercepta API nem páginas autenticadas. Só registra em produção.
- Captura: Wake Lock durante a captura (readquirido em `visibilitychange`), seletor de microfone (`enumerateDevices`,
  `deviceId` em localStorage com try/catch), medidor de nível e "Testar áudio" (só local, 10 s, nada gravado ou enviado).
  A Web Speech do navegador não aceita escolher o microfone: a escolha vale para o modo Servidor e para o teste (dito na tela).
  Sem Web Speech (iOS/Safari) e com STT de servidor ativo, a tela abre no modo Servidor (grava mp4 no Safari) em vez de pedir Chrome/Edge.
- Fila offline: `SegmentIn.client_id` (opcional, validado) e `append_segments` ignora duplicatas; o front reenvia em
  ordem ao voltar a rede (evento `online` e a cada 5 s) com o aviso "N falas aguardando rede".
- Privacidade (decisão conservadora): a fila guarda só texto de fala, nunca áudio. Em disco (IndexedDB) vai cifrada com
  AES-GCM, chave não extraível; sem IndexedDB/WebCrypto fica só na memória da aba. Validade de 12 h, apagada ao finalizar
  a consulta e ao sair da conta. Não protege contra script na própria página nem contra quem usa o navegador aberto.
  Erro de servidor (não de rede) no reenvio para o reenvio automático e mantém as falas até "tentar agora".
- Testes: `tests/test_consult_fila_offline.py` (pytest) e `e2e/mobile.spec.ts` (manifest, Digitar, Simular, offline com
  `setOffline`, Wake Lock mockado); projetos "iPhone 13" e "iPad" (iPad gen 7) no playwright.config rodam só esse
  spec, em **Chromium com emulação de aparelho** (viewport, userAgent, toque, isMobile), porque o CI só instala o
  Chromium. **WebKit real pendente** (Safari/iOS não validado). Os e2e não foram executados nesta verificação.
- Verificação: validade agora por fala (antes a fila inteira renovava a cada gravação), checada na leitura; troca de
  usuário no mesmo aparelho apaga a fila (`garantirDono`, guarda só o id do usuário); gravação em andamento não
  ressuscita a fila após o logout (época). 401 não apaga a fila (a sessão pode voltar), mas outro usuário não a herda.
- Não depende do hook de STT por streaming do #11.
## #38 transcrição que conhece o paciente (2026-10-21)

Branch `claude/onda4-38-transcricao-vocabulario` (sobre a do #11). Sem migração. Decisões:
- `app/consult/stt_vocab.py`: o `prompt` do STT (POST `/audio` e WebSocket) leva medicamentos ativos, diagnósticos e
  alergias do prontuário, depois o vocabulário institucional (`data/reference/vocab_institucional.csv`, caminho
  sobrescrevível por `VOCAB_INSTITUCIONAL_PATH`) e o glossário clínico, limitado a 900 caracteres, paciente primeiro.
  Gestor edita pela API (`GET/PUT /consult/vocabulario`, audit `consulta.vocabulario` com a contagem). É um arquivo
  global, não por organização: separar por organização fica para quando houver mais de uma instituição real.
- Correção posterior por similaridade com **`difflib`**, não `rapidfuzz` (e o WER é calculado por distância de edição
  própria, não `jiwer`): nenhuma dependência nova nem instalação offline. Limiar 0,88 (0,82 para termos do paciente),
  mesma inicial, tamanho mínimo 6, tenta o par de palavras antes ("glibem clamida" → "glibenclamida") e nunca junta com
  palavra seguinte se a primeira já é nome conhecido. Fica em `Segment.corrigidos` (de/para/razão) e no selo
  "corrigido" da tela; só vale com `origem="servidor"` e em português.
- Confiança: `Segment.confianca` = exp(`avg_logprob`) ou média da probabilidade por palavra; `palavras_baixas` abaixo
  de `STT_CONFIANCA_MINIMA` (0,6), sublinhadas no Transcript. Trecho incerto sem palavra incerta marca as palavras
  críticas. `Segment.confirmar` guarda só medicamento, dose/unidade ou alérgeno (até 3 palavras depois de "alergia").
- "Confirmar o que foi ouvido": `Engine.med_safety` marca o alerta que cita uma fala com `confirmar` pendente (texto
  prefixado, `MedSuggestion.confirmar_ouvido`) até `POST /consultations/{id}/segments/{n}/confirm` (audit
  `consulta.fala_confirmada`, sem texto) ou até o profissional corrigir o texto da fala. **A gravidade nunca desce**
  (ver "#38 verificado"). Também vale para o `ask.py` (copiloto), que reaproveita `med_safety`.
- `scripts/wer.py` (CI, depois do pytest, com resumo no `GITHUB_STEP_SUMMARY`): sintetiza o áudio das 12 primeiras
  falas do cen6, passa pela mesma `Sessao` do WebSocket e mede o WER (limite `STT_WER_MAX`, 0,15). **Limitação honesta:**
  o motor é o falso, então o número mede a fidelidade do caminho (janela, VAD, deduplicação, correção) e hoje fica em
  0%; o WER de um Whisper real exige áudio real e `STT_URL`, e não é medido no CI.

## #38 verificado (2026-10-23)

Branch `claude/integracao-paridade-lya` (o #38 já mesclado). Sem migração, sem dependência nova.
- **Alerta crítico nunca é rebaixado por baixa confiança do STT.** O wip já trocara "desce para atenção" por "continua
  crítico, com «Confirmar o que foi ouvido»"; confirmado no código (a única leitura de `confirmar` é `Engine.med_safety`,
  que só acrescenta o aviso e `confirmar_ouvido`; copiloto usa a mesma função). Teste de invariante: a mesma consulta com
  e sem trecho incerto tem o mesmo conjunto (tipo, severidade) de alertas. Docstring do módulo e este STATUS corrigidos.
- **O que falhava** (3 testes): (1) `cardiotase`→`cardiotrace` tem 2 edições; a trava de 1 edição do wip a barra de
  propósito (com 2 edições, `prednisolona` viraria `prednisona` para quem toma prednisona e a conduta mudaria). Não era
  difflib/rapidfuzz nem Python 3.13: era o código endurecido contra o teste antigo. O teste passou a usar uma troca de
  1 letra que só o limiar do paciente aceita (`zolmex`→`Zolmax`, razão 0,83) e ganhou o caso negativo de 2 edições.
  (2) `insulina nph` virava `insulina NPH` com selo de "corrigido": o par de palavras que já forma um nome do catálogo é
  agora deixado como está (teste de idempotência sobre o catálogo inteiro). (3) o teste do endpoint do vocabulário lia o
  arquivo global, mas o PUT já grava por organização (teste corrigido).
- **Achados da verificação cética, todos com teste** (`tests/test_consult_stt_vocab.py`):
  nome trocado pela correção agora também pede «confirmar o que foi ouvido» (é palpite do HousIA; o ouvido fica em
  `corrigidos`); alérgeno em lista longa ou antes de "alergia" (a janela era de 3 palavras) agora vale na oração toda;
  alergia vem primeiro no prompt (o corte por tamanho tirava o alérgeno antes do diagnóstico); fala em outro idioma
  também recebe confiança e `confirmar` (só não há correção); `palavras_baixas`, `corrigidos` e `confirmar` eram cópias
  da fala que sobreviviam ao expurgo (com trecho incerto, a frase inteira): agora são zerados; cache do vocabulário
  por (mtime_ns, tamanho) e limpo ao salvar (mtime sozinho serve arquivo velho em sistemas de arquivos de 1-2 s).
  Cobertos sem mudança: confirmar fala de outra organização (404, nada muda), vocabulário isolado por organização,
  audit sem texto, negação e dose preservadas na correção, `glibem clamida`→`glibenclamida`, 12 pares de medicamentos
  parecidos intactos.
- Decisões: editar o texto de uma fala continua dispensando a confirmação (quem editou conferiu); correção só com
  1 edição (a 2 edições o profissional vê o que foi ouvido).
- Pendências: o WER do CI mede o caminho (motor falso), não um Whisper real; números por extenso ("seiscentos") não
  contam como dose crítica; pares reais de medicamentos a 1 letra de distância fora de `drugs.csv` ainda poderiam
  ser trocados (mitigado pelo selo e pela confirmação); a edição de texto apaga a confirmação mesmo se a palavra
  crítica não foi tocada; vocabulário institucional em arquivo (um por organização), não em tabela; e2e/typecheck do
  front não rodaram (nenhum arquivo do web mudou nesta verificação).
