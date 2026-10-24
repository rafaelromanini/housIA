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

### Prompt da próxima sessão (substituído pelo da Pausa 4, no fim do arquivo)

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

## #43 sinais de comunicação (2026-10-23)

Branch `claude/integracao-paridade-lya`. Sem migração e sem dependência nova.
- **Intensidade por léxico** (`data/reference/consult_intensity.csv`, `app/consult/sinais.py`): vale o maior nível entre as
  falas da emoção, olhando 30 caracteres em volta da palavra da emoção; "muito medo", "pavor", "demais" = alta;
  "muito", "bastante" = média; "um pouco" rebaixa; repetir a emoção não sobe mais a intensidade (antes: 3 falas = alta).
  Na saída da IA a intensidade é recalculada pelo léxico sobre as falas citadas. O card leva `pistas` e o rótulo
  "sinal de comunicação, não diagnóstico". "pavor" entrou nas palavras de ansiedade.
- **Dúvidas e preocupações sem resposta** (`Insights.preocupacoes`): frase do paciente/acompanhante com "?" ou começo
  de pergunta, ou com ansiedade/preocupação/confusão, ligada às falas seguintes do profissional por termos em comum
  (radical de 5 letras, sem palavras genéricas). Resposta direta ("pode", "sim") só vale na fala imediatamente seguinte;
  "já explico"/"depois" adiam; preocupação aceita acolhimento ("entendo o medo") na fala seguinte. Itens aceitam
  "descartar" como as demais. Sem `emocoes` no consentimento tudo isso fica desligado. A lista "Antes de encerrar" está
  na tela ao lado do botão de finalizar (não bloqueia).
- **Passagem de plantão e alta**: ao finalizar, cada item aberto vira `Alert(source="comunicacao", atencao)` com só o
  assunto (termos) e a consulta no detalhe, nunca o texto da fala; sandbox e cirurgia não geram. A passagem lê como fato
  `preocupacao_nao_respondida` (pendência "retomar com teach-back"); o Waze da alta ganha a sugestão "responder e fazer
  teach-back" (prompt `discharge` 1.2.0). Fonte FHIR do alerta: `Encounter/{consulta}` (ou `Patient/{id}`).
- Purge: `termos` é zerado como `trecho`. Golden do cen6 atualizado. 18 testes novos em `tests/test_consult_sinais.py`.
- Pendências/ressalvas: o alerta não é fechado sozinho quando a pergunta é respondida depois (a consulta já está
  finalizada); a ligação por sobreposição de termos erra para respostas sem o mesmo vocabulário (a dúvida fica aberta
  e o profissional descarta, o erro seguro); falas em outro idioma usam a tradução/sinônimos das regras;
  `test_smart_apps.py::test_duplicate_app_id_and_module_without_run_are_refused` já falhava antes (SyntaxError no
  módulo de teste do registro; fora do escopo).

## #44 cartão "Antes de entrar" (2026-10-23)

Sem migração. `GET /patients/{id}/pre-consulta[?consulta=N][&resumo=true]` (`app/consult/pre_consulta.py`, leitura do banco em
`pre_consulta_service.py`), mesmo papel `clinician` da consulta e guarda de paciente avulso por organização.
- **Conteúdo**: motivo (`Encounter.reasonCode` da consulta de hoje), o que mudou desde a última consulta ambulatorial
  finalizada (exames novos com tendência e as duas refs, remédios iniciados/suspensos **a partir do início** da última
  consulta, para a HCTZ iniciada nela aparecer; PS e internações), pendências (`ServiceRequest` sem resultado =
  `record.pending_exams`; pendências da última nota finalizada do HousIA; `JourneyTask` aberta) e resumo da última alta
  (LACE, plano, achados da reconciliação, sinais de alarme; só com o módulo `alta` e até 180 dias). `chart.history()` agora
  inclui internações finalizadas. Cada linha tem `refs` FHIR e `fonte`; pendência de nota cita a nota de origem.
- **Some quando tratada**: pendência com ≥2 termos de conteúdo em comum (1 se o assunto só tem 1) com qualquer fala da
  consulta vira `tratada` com a `Fala/N`. Derivado a cada GET (nada gravado), a tela recarrega a cada fala.
- **Resumo opcional**: mock = 3 linhas do cartão; real = prompt `consult_pre_consulta` 1.0.0 sobre o cartão pseudonimizado,
  frase sem ref do cartão ou com número fora do cartão cai (cai para as regras). Audit `consulta.pre_consulta` com contagens.
- **Seed**: cenário 6 ganhou `pedidos` (RAC e retinografia sem resultado; chave nova no DSL, README atualizado).
- **Tela**: cartão na agenda (botão "Antes de entrar" ao expandir), no topo da tela inicial da consulta e recolhível na
  coluna da transcrição durante a consulta.
- Decisões/pendências: **Task do FHIR não é lida** (entrar em `PATIENT_SCOPED` faria o `remove_stale` do loader apagar as
  Tasks criadas pela jornada ao recarregar a demo; as tarefas vêm de `JourneyTask`). **Internação não foi semeada no
  cen6**: uma `IMP` anterior vira `record.hospitalization` e trataria a paciente ambulatorial como internada; o critério é
  coberto por teste que monta o cen6 com uma internação no builder. A tendência do exame compara com o resultado
  anterior, não com a meta; a resposta por termos em comum tem os mesmos limites do #43; não há e2e (a cobertura é de API).

## #43/#44 verificados (2026-10-23)

Branch `claude/integracao-paridade-lya`. Sem migração, sem dependência nova, sem mudança no web. Critérios de aceite dos
dois itens conferidos contra o código e os testes (cen6: dúvida da filha sem resposta até o médico responder; "muito
medo" = alta; internado → passagem e alta; cen6: creatinina 1,3 → 1,5 e HCTZ com as refs; internação no histórico;
pendência da nota some ao ser citada). Testes novos: `tests/test_consult_sinais_verificacao.py` e
`tests/test_consult_pre_consulta_verificacao.py`.
- **#43, achados corrigidos** (o erro perigoso é dar por respondida): (1) o médico que só repetia a palavra, devolvia a
  pergunta ("Qual ibuprofeno a senhora toma?") ou adiava citando o assunto ("já explico a catarata depois") fechava a
  dúvida; agora só conta a oração afirmativa e sem adiamento. (2) "Não sei", "não precisa falar disso agora" e "sim" a
  "o que é…?" valiam como resposta direta; "calma, vamos ao exame" valia como acolhimento. (3) Dois alertas de
  `comunicacao` de consultas do **mesmo encontro** (internado) se sobrescreviam (o `upsert_alert` casa por fonte FHIR e
  ambos citam `Encounter/x`): a dúvida sem resposta da primeira consulta sumia da passagem; cada dúvida agora é um alerta
  (consulta + fala + assunto), idempotente ao finalizar de novo. (4) Na mescla com a IA a intensidade lia a fala crua:
  "tengo mucho miedo" com tradução virava "baixa"; agora lê a mesma fala que as regras. (5) O registro de decisões
  (`insight_actions_json`) guardava o assunto da dúvida descartada e sobrevivia ao expurgo; agora guarda só o tipo.
- **#43, cobertos sem mudança**: negação ("não tenho medo"), emoção do acompanhante atribuída a ele, consentimento de
  emoções desligado (sem insights nem alerta), fala fora de alerta/auditoria/passagem/insights expurgados, item
  descartado não vira alerta (consulta finalizada não aceita mais ação), lista GENERIC.
- **#44, achados corrigidos**: (1) o paciente que dizia "não fiz a retinografia" marcava o pedido como tratado; a oração
  negada do paciente/acompanhante não trata mais, e os termos têm de estar na mesma oração. (2) O validador do resumo
  aceitava número que só existia dentro de um id de recurso (`Observation/123`); agora vale só número do texto do cartão.
- **#44, cobertos sem mudança**: paciente avulso de outra organização e consulta de outro paciente/organização (404),
  paciente sem histórico, remédio iniciado/suspenso fora da janela ou sem data, encontro sem fuso ou só com data, ref
  inventada e frase sem ref (caem para as regras), módulo de alta ausente (sem alta no cartão nem no resumo),
  internação anterior não torna a consulta de hoje internação, tarefa de jornada aberta/concluída/de outra organização.
- **Task do FHIR (decisão do implementador): aceita.** Os critérios não pedem a leitura; `JourneyTask` é a fonte da
  verdade da jornada (a Task do FHIR é cópia, fechada junto) e ler Task em `PATIENT_SCOPED` apagaria as Tasks da jornada
  no `remove_stale`. Limite: Task criada por outro sistema do prontuário não aparece. Agora com teste.
- **`test_smart_apps.py::test_duplicate_app_id_and_module_without_run_are_refused`**: era do ambiente (Windows), não do
  Python 3.13: o teste gravava o módulo com `write_text` sem `encoding`, o Windows grava em cp1252 e o import lê UTF-8
  ("m\xf3dulo" → SyntaxError). Corrigido com `encoding="utf-8"` no teste; no Linux/CI já passava.
- **Pendências**: "é grave?" sem outro assunto não tem termo de conteúdo e não vira dúvida rastreada; o assunto (termos)
  continua no título do alerta de passagem depois do expurgo (decisão do #43: só o assunto); resumo da alta usa a
  avaliação mais recente (sem conferir se a internação já terminou); o cen6 não tem internação semeada (uma `IMP`
  anterior vira `record.hospitalization`); a resposta por termos em comum ainda erra para resposta sem o mesmo
  vocabulário (fica aberta, o erro seguro).

## #45 documentos derivados e resumo ao paciente (2026-10-23)

Branch `claude/integracao-paridade-lya`. Sem migração e sem dependência nova (estado em `extra_json["documentos"]`).
- **Módulos**: `app/consult/documentos.py` (regras puras) e `documentos_service.py` (estado, aprovação, envio). Rotas em
  `/consultations/{id}/documentos[/{tipo}]` (`GET` lista e disponibilidade; `POST` gera; `PUT` edita; `/aprovar`;
  `/export`, `/export.pdf`; `/send`). Aba "Documentos" na revisão (`components/consult/documents-panel.tsx`).
- **Sete tipos**: resumo ao paciente, atestado, encaminhamento, contrarreferência, relatório para convênio, receita e
  solicitação de exames. Receita e exames saem só dos pedidos que o médico **assinou**; encaminhamento só dos cards
  decididos como "encaminhar"; sem isso o tipo fica indisponível com o motivo.
- **Fonte em cada linha**: item sem `refs` é descartado e listado em `descartados` (exceto o texto-padrão do modelo e o que o
  profissional escreve, marcados `origem` "modelo"/"profissional"). As orientações soltas da nota (`orientacoes_paciente`,
  sem fonte) não entram no resumo: vão para `descartados` com aviso.
- **Resumo ao paciente**: remédios com horário e para quê (reaproveita `PURPOSE`/`home_meds` da alta), alergias, **sinais de
  alarme por regra** (gerais, diagnóstico, classe do remédio, remédio novo e urgência do protocolo do encaminhamento),
  folha de exames com tendência (as duas refs) e retorno. **Índice de legibilidade** em português (Flesch adaptado por
  Martins et al., 1996; sílabas por grupo de vogais) com aviso abaixo de 50. Idioma do paciente: tradução item a item por
  IA (prompt `consult_documentos` 1.0.0) só em modo real e com consentimento de tradução; cada item passa por
  `fidelidade.divergencias` e o que diverge fica em português (`divergentes`). No mock o documento sai em português com aviso.
- **Segurança e privacidade**: CID no atestado e no convênio só com `autorizacao_cid` (padrão sem CID, aviso quando pedido sem
  autorização); item `obrigatorio` (alergia e sinais de alarme) não pode ser removido nem esvaziado na edição (422); editar
  zera a aprovação; exportar/enviar exige nota revisada e atestada, sem pendência crítica (`_gate_clinical_export`),
  documento aprovado e a nota igual à da geração (hash; nota mudou = "desatualizado"). Audit só com contagens.
- **FHIR**: cada documento é um `DocumentReference` próprio (identifier `{consulta}:{tipo}`, LOINC do tipo, `docStatus` final,
  `context.related` com os recursos citados); reenvio devolve 409. Sem `EHR_WRITEBACK`, ou em sandbox, só devolve o recurso.
- **Contrarreferência de alta**: com o módulo `alta` e uma avaliação do paciente, traz as sugestões do Waze, o risco (LACE
  e social) e a lista de remédios em uso como reconciliação.
- **Pendências/ressalvas**: **QR na impressão não foi feito** (nenhuma biblioteca de QR no projeto e sem dependência nova): o
  PDF e o texto levam o "Código de verificação" (hash da nota); os códigos LOINC por tipo (69730-0, 51852-2, 57133-1,
  18761-7, 34133-9, 57833-6, 46209-3) vêm de memória e devem ser conferidos na tabela oficial; o PDF é texto simples
  (sem assinatura digital); a "folha de exames" lista até 8 dosagens (as que mudaram primeiro); o consentimento de CID é
  uma caixa na tela, ainda não um registro no consentimento da consulta; a versão em outro idioma não foi exercitada com um
  LLM real. Testes: `tests/test_consult_documentos.py` (21).

## #46 edição da nota por instrução, com diff e memória de estilo (2026-10-23)

Branch `claude/integracao-paridade-lya`. **Uma migração** (`c301`, sobre `e351`): tabela `estilo_notas` (tenant, uma linha por
profissional); golden de `tests/test_journey.py` atualizado. Sem dependência nova.
- **Rota**: `POST /consultations/{id}/note/sections/{key}/rewrite {instrucao, aplicar}` (`reescrita.py`, `reescrita_service.py`,
  prompt `consult_note_rewrite` 1.0.0). `aplicar=false` (padrão) devolve só a prévia (antes, depois, diff por palavra, refs,
  descartadas); `aplicar=true` grava só aquela seção, **tira a atestação** (a nota volta à revisão) e registra a proveniência
  (`extra_json["reescritas"]`, sem texto). Vale para as nove seções clássicas; pendências não são reescrevíveis (422).
  Tela: campo "Reescrever por instrução" abaixo de cada seção, com prévia lado a lado e botão "Aplicar".
- **Fonte e origem**: a saída do modelo (ou do mock) é remontada pelo servidor. Frase igual a uma existente volta como estava;
  com refs válidas é "ia"; sem refs herda as da frase parecida (razão ≥ 0,5); sem parecida só entra se for o que a instrução
  mandou escrever (origem **"profissional"**, sem refs); fora disso é descartada ("sem fonte"). Número ou medicamento que não
  estava na seção nem na instrução derruba a frase (fato novo). Ref inventada cai.
- **Trava de segurança**: são protegidas a seção de alergias inteira, as frases com o texto de um alerta de medicamento
  (crítico ou de atenção) e as que citam um alérgeno; elas precisam seguir **literalmente** na seção nova, senão 422
  `reescrita_bloqueada` (nada é gravado, audit `consulta.nota_reescrita_bloqueada` só com códigos). Além disso a nota candidata
  não pode criar pendência crítica nova (`clinical_pending` antes x depois). O prompt também manda copiar as protegidas.
- **Memória de estilo** (`estilo.py`): a cada nota **atestada** que difere do rascunho da IA guarda 3 números por consulta
  (razão de tamanho nas seções que ele mexeu, variação de siglas por 100 palavras, se reordenou frases); salvar de novo
  substitui, não soma; guarda as 30 mais recentes. Com ≥ 3 notas editadas o perfil fica ativo (mais_curto/mais_longo,
  mais_siglas/menos_siglas, reordena) e vai ao prompt da reescrita como dicas de forma. **Nenhum texto** entra na tabela.
  `GET/PUT/DELETE /consult/estilo` (ver, desligar, apagar); excluir os dados do paciente tira as contribuições daquelas consultas.
- **Mock** (`LLM_MODE=mock`): obedece "resuma/encurte", "remova/retire X" (inclusive "o alerta"), "acrescente: …", "sem siglas /
  use siglas" e "no meu estilo"; instrução que não reconhece deixa a seção igual e avisa. O mock obedece de propósito ao pedido de
  tirar o alerta, para que a trava (que é do servidor) seja exercitada.
- **Pendências/ressalvas**: a proteção é por frase literal, então o plano do cen6 (todas as frases citam um alerta) não
  encurta, só ganha texto; a regra de "fato novo" olha números e medicamentos do catálogo, não afirmações qualitativas; o
  estilo vai só ao prompt da reescrita, não ao da geração da nota (mudaria a versão de `consult_note`); o perfil aprende de
  qualquer edição atestada, inclusive correções clínicas (não só de forma); seções do modelo (`secoes`) são aceitas pela
  chave no servidor mas a tela só oferece a reescrita nas nove clássicas; sem e2e. Testes: `tests/test_consult_reescrita.py` (19).

## #45/#46 verificados (2026-10-23)

Branch `claude/integracao-paridade-lya`. Sem migração nova (a `c301` é a única cabeça do Alembic), sem dependência nova, sem
mudança no web. Critérios de aceite dos dois itens conferidos contra o código e os testes (cen6: remédios e sinais de alarme
com refs; atestado sem CID; contrarreferência com o Waze; um DocumentReference por documento; "resumir" mantém as refs;
alerta de dose bloqueado; perfil muda com 3 notas). Testes novos: `tests/test_consult_documentos_verificacao.py` (25) e
`tests/test_consult_reescrita_verificacao.py` (16).
- **#45, achados corrigidos**: (1) o relatório de convênio sem autorização trazia o código CID por outra seção
  ("Hipóteses ... (N17.9, confiança alta)"): agora o código sai de todas as linhas e o diagnóstico em palavras fica
  (`sem_codigo_cid`). (2) O hash de "desatualizado" olhava só a nota: pedido que voltava a pendente depois da aprovação
  deixava a receita/exames exportáveis; agora o hash inclui os pedidos assinados (receita, exames, convênio, resumo) e os
  encaminhamentos decididos, por tipo (o atestado não cai por causa de pedido). (3) A enfermagem aprovava receita, atestado e
  solicitação de exames (a assinatura do pedido já era só do médico): 403. (4) A conferência da tradução deixava passar
  "Pare de tomar X" → "Take X", "de manhã" → "in the evening" e alérgeno trocado quando não era remédio do catálogo; agora
  há conferência extra (parar x tomar, manhã/tarde/noite, alérgeno por cognato) e o item que diverge fica em português.
  (5) A edição podia reescrever a linha de alergia sem o alérgeno: 422. (6) O envio usa criação condicional por
  identifier (`ifNoneExist`), então um envio que caiu depois de gravar no prontuário não duplica o DocumentReference.
  (7) O display do LOINC 34133-9 era "Summary of episode note"; o nome oficial é "Summarization of episode note".
- **#45, os 6 pontos do implementador**: *QR ausente*: aceito como pendência (o critério de aceite não pede QR; o texto e o PDF
  levam o código de verificação, hash do que foi aprovado). *LOINC*: 69730-0 Instructions, 51852-2 Letter, 57133-1 Referral
  note, 18761-7 Transfer summary note, 34133-9 Summarization of episode note, 57833-6 Prescription for medication e 46209-3
  Provider orders conferem com o meu conhecimento do LOINC (sem acesso à tabela oficial; só o display do 34133-9 foi
  trocado); conferir na tabela antes de produção. *Autorização de CID só caixa na tela*: aceito para esta entrega, o servidor
  nunca inclui CID sem `autorizacao_cid=true`; fica a pendência de gravar a autorização no consentimento da consulta e
  na auditoria. *Aprovação por documento*: aceita, agora com o papel (médico) nos documentos de ato médico.
- **#45, cobertos sem mudança**: linha sem fonte, receita/exame só de pedido assinado (crítico exige justificativa), card
  não decidido ou "manejar na APS", nota sem atestação e com pendência crítica, reenvio (409), documento enviado não edita
  nem regera, outra organização (404), exclusão LGPD apaga os documentos junto com a consulta, índice de legibilidade.
- **#46, achados corrigidos**: (1) uma frase nova com ref válida do prontuário (de fora da seção) entrava como "ia"; agora só
  vale ref que a seção já tinha. (2) Fato novo qualitativo ("nega dor torácica") com ref passava; agora a frase sem
  parecida na seção, sem pedido na instrução e com mais da metade das palavras de conteúdo novas é descartada. (3) A frase
  parecida com a negação invertida ("não subiu" x "subiu") herdava a fonte; agora é descartada. (4) O texto que a instrução
  manda escrever é sempre "profissional", sem refs, mesmo que o modelo anexe uma ref. (5) Seção "Sem registro" que o modelo
  esvaziava virava vazia; e a seção do modelo numa nota antiga (sem `modelo`) podia "aplicar" sem gravar nada. (6) Memória
  de estilo desligada continuava aprendendo, e apagar a memória religava; agora desligada não aprende e apagar mantém o
  desligado.
- **#46, os pontos do implementador**: *proteção só por frase literal*: aceita (erra para o lado seguro: bloqueia e não
  grava); fica a pendência de proteger também o alerta reescrito com o mesmo sentido. *Estilo aprendendo de correção
  clínica*: aceito, pois o perfil guarda 3 números, vai só ao prompt da reescrita como dica de forma e o conteúdo passa pelas
  travas do servidor; o efeito de uma correção clínica é no máximo "prefere mais curto/longo". *Seção fora das nove*: agora
  com teste (cardiologia, `classe_nyha`).
- **#46, cobertos sem mudança**: alergia/alerta/"apagar a parte do remédio" (422), injeção na instrução e no conteúdo (a
  instrução não vai à auditoria; frase mandada pelo modelo sem fonte cai), número/medicamento novo, ref inventada, aplicar
  derruba a atestação, estilo sem texto clínico, de outro profissional e de outra organização, LGPD tira as contribuições,
  `alembic heads` = `c301`.
- **Pendências**: o painel Documentos mostra o 403 da enfermagem como erro genérico (sem tratamento específico na tela);
  a conferência de tradução por cognato manda para o português o alérgeno sem cognato óbvio (camarão/shrimp): é o erro seguro;
  a tradução ainda não foi exercitada com um LLM real; o mock de resumir corta a frase na primeira vírgula e pode perder
  detalhe (só no modo sem IA externa).

## #47 pendências viram tarefas (FHIR Task) com responsável, prazo e ligação com passagem e alta (2026-10-23)

Branch `claude/integracao-paridade-lya`. **Uma migração** (`c302`, sobre `c301`): `journey_tasks` ganha `consultation_id`, `critical` e
`chave` (idempotência); golden de `tests/test_journey.py` atualizado. Sem dependência nova.
- **Regras** (`app/consult/tarefas.py`, só fala da equipe, sem IA): "retorno em X" (dias, semanas, meses, números por extenso,
  "semana que vem"), "solicitar/pedir" exame, parecer ou encaminhamento (vários por frase), "ligar com o resultado" e protocolo
  atrasado (`pendente`) ou gatilho de risco (`critico`). Pergunta, negação, condicional ("se piorar, volte em 3 dias") e retorno
  sem prazo não viram tarefa. "Urgente/hoje mesmo/imediato" na frase marca a tarefa como **crítica**. `sem_registro` de protocolo
  não é atraso (cen6 teria 10+ tarefas de ruído).
- **Gravação** (`tarefas_service.py`, chamado no `finish`): cada proposta vira `JourneyTask` + FHIR `Task` (papel em
  `performerType`, prazo em `restriction.period`, `focus` = Encounter, `reasonCode` = "Consulta #N, Fala/M", `input` com a fala e
  os recursos, `priority` urgent se crítica). Sandbox e cirurgia não geram. A mesma chave não duplica (finalizar de novo; protocolo
  já aberto de outra consulta). Se o FHIR falhar a tarefa fica só no banco. O texto da fala nunca vai ao Task, à tarefa ou à
  auditoria (só o assunto).
- **Revisão**: `GET/POST /consultations/{id}/tarefas` (lista; pendência escrita pelo profissional, origem profissional),
  `PUT /journey/tasks/{id}` (título, detalhe, papel, prazo, criticidade; atualiza o Task; tirar a marca de crítica exige
  justificativa), `GET /journey/tasks?consulta=&patient_id=`. Tela: `components/consult/tasks-panel.tsx` na revisão.
- **Segurança**: tarefa crítica só conclui com justificativa (≥10 caracteres, 422); cancelar sempre exige motivo; o fechamento
  audita `jornada.tarefa_concluida/cancelada` (crítica, consulta, se houve justificativa, nunca o texto) e a edição
  `jornada.tarefa_editada` (campos).
- **Passagem de plantão**: `tarefa_facts` (alerts/service) entra em `generate_handoff` e na conferência verbal: tarefa aberta com
  responsável e prazo, crítica para os dois públicos (vira risco e pendência), vencida sobe para atenção; não cria Alert. Prompt
  `handoff` 1.1.0. Omitir a tarefa crítica no relato verbal aparece como omissão crítica.
- **Alta (Waze)**: sugestão "Resolver a pendência aberta" por tarefa (prompt `discharge` 1.3.0). **Trava suave**: tarefa crítica
  aberta bloqueia `confirm` (409) mesmo com risco baixo, até a tarefa ser concluída com justificativa ou haver override com
  justificativa registrado depois da tarefa mais recente; `register_override` agora aceita esse caso. `assessment_out` traz
  `tarefas` e `trava.por_tarefas`; a tela mostra as tarefas abertas.
- **Cartão "Antes de entrar" e LGPD**: a linha de tarefa não leva `Fala/N` de outra consulta e não repete o protocolo que a nota já
  listou; excluir os dados do paciente apaga as tarefas nascidas das consultas (o FHIR Task já criado **não** é apagado).
- Mantida a decisão do #44: `Task` fora de `PATIENT_SCOPED` (o `remove_stale` apagaria as Tasks da jornada).
- Pendências/ressalvas: não há fechamento automático quando a tarefa é cumprida em outra consulta (retorno feito, exame
  resultado); o papel padrão é fixo por regra (médico, exceto ligação = enfermagem) e a edição corrige; "retorno" dito pelo
  paciente/acompanhante não conta; frases como "vamos pedir exames" sem nome de exame conhecido não geram tarefa (lista
  `_ORDERABLE`); sem e2e. Testes: `tests/test_consult_tarefas.py` (31).

## #42 passagem de plantão falada com conferência ao vivo (2026-10-23)

Branch `claude/integracao-paridade-lya`. Sem migração e sem dependência nova. A conferência verbal (`check.py`) deixou de depender de
texto digitado ou colado.
- **Endpoint incremental** `POST /patients/{pid}/handoff/verbal-check/segments` (`{audience, window_hours, segmentos:[{n, texto,
  origem}], final, ciente}`): sem estado no servidor, o cliente reenvia as falas acumuladas e as **regras** rodam em milissegundos
  (o texto não vai à IA). Cada item volta com a fonte do prontuário (`refs`) e a fala que o mencionou ou contradisse (`fala_ref`
  "Fala/N"); "PA estável" com PA 184/102 vira `divergente` crítico na mesma chamada da fala. `final=true` roda o caminho completo
  (IA em modo real, mock em `LLM_MODE=mock`), **audita** `handoff.conferencia` com contagens e `ao_vivo`, `segmentos`, `origens`
  (nunca o texto; as chamadas parciais não auditam, para não inflar o indicador de omissões da gestão) e, se a IA cair, devolve
  as regras com aviso.
- **Correção no meio da fala** (`check.py`): a divergência de estabilidade vale pela última palavra; se depois dela o relato traz
  valor ou tendência sem afirmar estabilidade ("corrigindo: subiu até 184/102"), o fato passa a `mencionado`; "mas agora está
  estável" volta a divergir.
- **Trava de segurança** (`ao_vivo.garantir_criticos`, vale também na conferência digitada): omissão ou divergência **crítica**
  apontada pelas regras nunca é rebaixada pela IA (nem apagada se ela a esquece); o item sai com "(mantido pela regra de
  segurança)" e `meta.regra_prevaleceu`. Tarefas abertas (#47) também são fatos da conferência.
- **Roteiro SBAR** (`roteiro` na resposta, `GET /handoff/verbal-check/roteiro`): Situação, Histórico, Avaliação e Recomendação, com
  a etapa coberta (palavras-chave), o que ainda falta dizer (fatos críticos/de atenção omitidos, por etapa) e o guia "Próximo: …".
- **Áudio** `WS /ws/patients/{pid}/handoff/audio?audience=&janela_h=&ciente=true`: mesmo PCM 16 kHz da consulta
  (`stt_stream.Sessao`, vocabulário do paciente do #38), conferência por regras a cada trecho, **parcial inclusive**; texto e áudio só
  na memória da conexão; audita `handoff.audio_stream` (bytes e frases). Plano (módulo `passagem_plantao`, só clínica/hospital),
  paciente avulso e perfil médico/enfermagem conferidos na conexão. O WebSocket mora num `ws_router` à parte (a dependência HTTP do
  módulo não serve a WebSocket). **Ciência obrigatória** (`ciente`) nas duas portas, no lugar do consentimento da consulta (a
  passagem é fala da equipe, não do paciente).
- **Tela** (`verbal-check.tsx` ganhou "Digitar ou colar" × "Falar ao vivo"; `verbal-live.tsx`, `resultado-conferencia.tsx`):
  captura por servidor (`useServerStream`, agora com `opts.url`/`onMessage`), navegador (aviso de demonstração), **roteiro de
  demonstração** (`data/handoff/cen1_passagem_enfermagem.json`, `GET /handoff/demo-scripts`) ou fala digitada. Lista ao vivo de
  divergências, "ainda não citado" (crítico em destaque) e mencionado, com a fala e "ver fonte".
- **Testes**: `tests/test_handoff_ao_vivo.py` (19: critérios, SBAR, roteiro fala a fala, auditoria, trava contra a IA, ciência, limites,
  plano, avulso de outra organização, WebSocket com o motor falso) e e2e `apps/web/e2e/passagem-falada.spec.ts` (rodado local no
  Chromium: roteiro de demonstração e fala digitada; `pilares` e `melhorias` seguem verdes).
- **Pendências/ressalvas**: o contexto do prontuário da conexão de áudio é lido uma vez no início (não acompanha novos registros
  durante a passagem); a atribuição da fala usa as mesmas palavras-chave das regras (com IA no `final`, um fato que o modelo
  achou por sinônimo fica `mencionado` sem `fala_ref`); a conferência parcial por regras é por palavra-chave, então sinônimos
  coloquiais podem aparecer como "ainda não citado" até o `final` com IA; sem teste com motor de fala real (só o falso).

## #47/#42: verificação concluída (2026-10-24)

Todos os bugs da verificação interrompida foram corrigidos, cada um com teste de regressão (`test_consult_tarefas.py`,
`test_handoff_ao_vivo.py`; `test_journey.py` verde; ruff limpo).
- **#47**: negação "não é necessário" (`_NEGATION`); urgência negada ("não é urgente", "sem urgência") não marca crítica;
  "encaminhar a senhora para o cardiologista" gera tarefa; título sem "urgente"; fala de `falante_auto` vira tarefa com aviso no
  detalhe e na auditoria (`tarefas_falante_automatico`); `edit_task` não rebaixa a prioridade FHIR (só muda se a criticidade foi
  editada).
- **#42**: `_says_stable` por palavra inteira e sem negação ("também", "não está estável", "Ok" solto); `CORRECTION` só aceita valor
  clínico ou tendência (hora/número solto não apaga a divergência); relato acima de 6000 caracteres fecha o `final` só com regras
  (aviso, audita), teto duro de 60000 (413 no HTTP; erro 1009 no WebSocket); auditoria registra `ciente`.
- **LGPD**: o FHIR Task é mantido; `DELETE /patients/{id}/consultation-data` e a auditoria informam `tasks_fhir_mantidos`.
- **Verificação cética extra**: prompt-injection no relato (prompt `handoff_check` 1.0.1 trata o relato como dado; a regra
  prevalece sobre a IA), `garantir_criticos` no caminho final, token de outra organização no WebSocket e teto de duração
  (`MAX_SESSAO_BYTES`) testados.

## Pausa 4 (2026-10-23, cota em ~60%+)

Trabalho acumulado na branch **`claude/integracao-paridade-lya`** (a partir da main em `15c64db`, cabeça Alembic **c302**),
**sem PR** de propósito: decidido juntar as funcionalidades e testar tudo junto para poupar cota. Tudo está no remoto.
A suíte completa (~20 min) e os e2e **ainda não foram rodados** nesta branch; só testes parciais por item.

Já na branch, implementado e verificado: #11 (Whisper no servidor), #12 (PWA no celular), #38, #43, #44, #45, #46.
Implementado mas com verificação interrompida: **#47 e #42** (bugs corrigidos, ver "#47/#42: verificação concluída"). Ambiente: Windows, venv `apps/api/.venv/Scripts/python` (Python 3.13).

Falta, na ordem:
1. (feito) bugs de #47/#42 corrigidos e verificados.
2. (#41 e #48 feitos, ver "#41 ditado e adendo" e "#48 laudos em PDF ou foto").
3. **Suíte completa + e2e + PR único** da branch de integração (CI: API, Web, Helm, E2E), merge na main. Não deixe para o fim
   da fila: faça logo após o item 2.
4. Validar com LLM real (`ANTHROPIC_API_KEY`, `LLM_MODE=real`): saída estruturada dos itens novos só foi testada em mock.
5. P1 restantes na ordem: #52, #53, #54, #40, #56. Fora do piloto salvo pedido: #39, #49, #50, #51, #55, #57.
6. Vídeo de pitch (#0) com foco na Consulta com IA (scripts em `brag-output/scripts/`, que apontam para
   `/home/user/housIA/...`: ajustar para o Windows), `share-copy.txt`, `login-form.tsx`, `description` em `app/layout.tsx`,
   topo do README. Depois polir o front.

Conferência humana pendente (não é código): códigos LOINC dos documentos (#45), tabelas oficiais CIAP-2/SIGTAP, faixas
pediátricas, avisos de consentimento em 9 idiomas.

### Prompt da próxima sessão

> Continue o HousIA (rafaelromanini/housIA) de forma AUTÔNOMA na branch `claude/integracao-paridade-lya` (já no remoto;
> faça `git fetch` e `git checkout`). Leia a seção "Pausa 4" e "Regras do projeto" em docs/paridade-lya/STATUS.md.
> Regras: UM agente por vez, em série (implementação, depois verificação cética de escopo fechado: critérios de aceite
> + ~12 entradas adversariais; sem worktree paralelo); agentes Sonnet (effort medium; high só na verificação de
> segurança clínica/privacidade; Haiku para tarefas mecânicas); orquestrador com effort baixo e respostas curtas;
> NÃO abrir PR por item: junte tudo nesta branch e teste junto; commit e push após cada entrega; LLM_MODE=mock; só
> dados sintéticos; nunca commitar chaves. Migração nova encadeia na cabeça atual (c302) e atualiza o golden de
> tests/test_journey.py. Ordem: (1) corrigir os bugs abertos de #47/#42 listados em "#47/#42: verificação interrompida"
> e concluir a verificação; (2) #41 e #48; (3) rodar a suíte completa (`pytest -o addopts=""`), ruff, typecheck/lint web
> e e2e no modo do CI, abrir UM PR em português da branch contra a main e fazer o merge só com API/Web/Helm/E2E verdes;
> (4) validar com LLM real se houver chave; (5) #52, #53, #54, #40, #56; (6) vídeo pitch e polimento do front. Ao
> chegar a 80% da cota: não inicie entrega nova; commit wip, push e atualize este STATUS com novo prompt. Se eu
> informar a cota, use-a; senão, pare depois de abrir o PR do item (3).

## #41 ditado e adendo (2026-10-24)

Branch `claude/integracao-paridade-lya`, sem migração (o desfazer fica em `extra_json["adendos"]`). Decisões:
- `POST /consultations/{cid}/addendum {secao, segmentos[], via}` (`adendo.py`, `adendo_service.py`): grava falas de origem
  `adendo` (falante sempre médico, decidido no servidor) e acrescenta frases só à seção alvo, cada uma citando `Fala/N`
  do adendo (origem "profissional"; sem IA, texto literal). Funciona com a consulta finalizada, até o envio; consulta de
  sandbox também. Cirurgia fica fora (409). Origem `adendo` no POST `/segments` e no `/finish` é 422.
- Comandos de voz só como fala inteira: `próximo campo`, `apagar última frase`, `desfazer`, `nova linha` (não viram fala).
  Apagar frase de alergia ou com texto de alerta de medicamento é bloqueado (422, nada gravado); o ditado que criaria
  pendência crítica também. Desfazer é recusado se a seção foi editada à mão depois. Aplicar tira a atestação.
- Travas: consentimento revogado 403; `via: servidor` exige a finalidade de transcrição; texto limpo de controle e
  invisíveis; até 20 trechos de 2000 caracteres por chamada e 8000 por seção; `client_id` torna o reenvio idempotente;
  o audit (`consulta.adendo_ditado`/`adendo_bloqueado`) guarda só contagens.
- Front: `components/dictate-button.tsx` (`DictateButton`, `DictateText`, `DictateSection`) e `lib/voice-commands.ts`
  em cada seção da nota, no override da alta, no descarte de alerta e na justificativa do envio. Usa só o
  reconhecimento do navegador; o streaming de servidor (#11) não foi ligado ao ditado. Não validado em navegador real.
- Pendente: ditado nas justificativas da reconciliação (sem campo de texto livre hoje); e2e com microfone.

## #48 laudos em PDF ou foto (2026-10-24)

Branch `claude/integracao-paridade-lya`, sem migração (laudos em `extra_json["laudos"]`, contador `laudos_seq`). Dependência
nova: `pdfplumber` (núcleo); extra `ocr` (pytesseract + pillow) e tesseract-ocr-por no Dockerfile.
- `POST /patients/{pid}/documents?consulta=&ciente=true&guardar=` (corpo bruto, até 8 MB), `GET` e `DELETE /{n}`. Leitura
  **só por regras** (nada vai à IA): PDF por pdfplumber (sem ele, leitor mínimo de fluxos Flate); foto por tesseract `por`
  (em `LLM_MODE=mock` o texto vem do metadado sintético `housia-ocr` do PNG/JPEG, determinístico).
- Só linha que **começa** pelo nome de exame conhecido, com unidade compatível (conversões `/mm3`, mEq/L, µUI/mL), vira valor;
  `<`/`>`, sem unidade, unidade errada, valor impossível e valores conflitantes são ignorados com o motivo. Cada valor leva
  `Documento/N p. X` e o trecho. Texto do laudo é dado: injeção de prompt não vira ação.
- Valores entram no Smart App Exames e na nota (`exames_complementares`), com a comparação com a série FHIR ("subiu de 7,1 %
  (data)"); valor crítico vira alerta crítico; Observation `preliminary` (LOINC de `codes.LABS`) em `propostas` (tipo `exame`),
  aceita item a item. `Documento/` é fonte válida (não é recurso FHIR; fora de `_fhir_refs`).
- Segurança: magic bytes (Content-Type e extensão não valem), 413 acima de 8 MB (leitura em fluxo), imagem > 40 MP, PDF
  criptografado ou com script/ação/anexo recusado, bomba de descompressão barrada, nome no laudo diferente do paciente (422),
  `ciente` obrigatório, consentimento revogado (403) ou pausa (409) barram, outra organização 404, consulta enviada 409.
  Arquivo descartado por padrão; `guardar=true` (só médico, fora do sandbox, com `EHR_WRITEBACK`) cria DocumentReference.
  Audit só com contagens. LGPD: excluir os dados do paciente apaga os laudos e informa `laudos_fhir_mantidos`.
- Front: `components/consult/laudos-panel.tsx` na revisão; `SourceRefs` mostra "laudo N, p. X". Testes:
  `tests/test_consult_laudos.py` (31). Pendências: OCR real (tesseract) não testado aqui; PDF escaneado (sem texto) pede
  foto; PDF com fontes subconjunto/CMap próprio depende do pdfplumber; sem e2e nem upload em duas páginas por foto.

## #52 perfis brasileiros: BR Core/RNDS, CNS/CNES/CBO e exportação e-SUS (LEDI) (2026-10-24)

Branch `claude/p1-restantes`. Migração c303 sobre c302 (cabeça única; golden de `test_journey.py` e do teste de
reescrita atualizados): `organizations.cnes`, `users.cns`, `users.cbo`. Decisões:
- `app/br/identificadores.py`: CPF (aceita máscara), CNS (definitivo 1/2 e provisório 7/8/9, com dígito verificador),
  CNES (7 dígitos) e CBO (6 dígitos), mensagens em português. NamingSystems da RNDS para CNS e CNES (mesmo prefixo do CPF).
- Cadastro só pelo gestor: `PUT /orgs/{id}/cadastro` (CNES) e `PUT /orgs/{id}/membros/{uid}/cadastro` (CNS, CBO);
  vazio apaga; auditoria `org.cadastro_br`.
- `app/br/perfis.py`: o Bundle estruturado (/export e /send) declara `meta.profile` em Composition, DocumentReference
  e Practitioner; com cadastro, junta CNS no Practitioner, Organization (CNES, `Composition.custodian`) e
  PractitionerRole (CBO). Sem cadastro nada é inventado.
- `GET /consultations/{cid}/export/ledi[?baixar=true]` (`app/br/ledi.py`, `app/consult/ledi_service.py`): ficha de
  atendimento individual JSON, só de consulta ambulatorial finalizada, revisada e atestada (mesmas travas do /export),
  papéis médico e enfermagem, auditoria `consulta.ledi_exportado` só com contagens. Exige CNES, CBO, CNS do
  profissional e CNS ou CPF válido do paciente (senão 422 com a lista `faltas`). Só CID/CIAP confirmados (hipótese fica
  fora); procedimento só se for SIGTAP oficial de 10 dígitos (os DEMO-* vão para `pendencias`). Botão "Exportar e-SUS
  (LEDI)" na revisão da nota.
- Pendências de conferência humana: (1) URLs dos perfis em `app/br/perfis.py` (BRIndividuo, BRProfissional,
  BREstabelecimentoSaude, BRLotacaoProfissional, BRRegistroAtendimentoClinico-2.0, BRDocumentoReferencia) e dos
  NamingSystems/CodeSystem BRCBO foram escritas sem validar contra os pacotes de IG; rodar o validador FHIR antes de
  produção. (2) Nomes e enumerações do LEDI (versão 7.3.x, `app/br/ledi.py`) sem o dicionário oficial: conferir turno,
  sexo, limite de 2 problemas e `uuidFicha`; a serialização Thrift que o PEC importa não foi feita. (3) CBO só valida
  formato, sem a tabela CBO-2002. (4) Sumário de Alta e RAC completos nos perfis oficiais (seções obrigatórias) não
  foram gerados: só a declaração no Composition atual. (5) INE, local e tipo de atendimento e IBGE do município ficam
  para o PEC (listados em `pendencias`). (6) Tabela SIGTAP real: seguem os DEMO-*. (7) CNS do paciente é lido do
  Patient FHIR; o seed sintético não traz CNS (a exportação cai no CPF).
- Testes: `tests/test_br_perfis.py` (29: validadores, cadastro, LEDI feliz/CPF/sem cadastro/não finalizada/sem atestação/
  papel/sem login/inexistente, procedimento DEMO e hipótese, Bundle com e sem cadastro).

### Verificação #52 (2026-10-24)

Verificador cético, 20 testes novos em `tests/test_br_perfis.py`. Bugs achados e corrigidos: (1) o administrador da plataforma
(gestor da organização padrão) podia alterar CNES/CNS/CBO de outra organização via `_alvo`: agora só o gestor da própria
(404 nos demais); (2) `PUT .../cadastro` do profissional apagava o campo omitido: agora só muda o que veio no corpo;
(3) LEDI de consulta de sandbox era exportado: 422; (4) a ficha usava o CNS/CBO de quem exporta, não de quem atendeu
(`created_by`); (5) `cids` nulo ou código não textual quebrava (500) e texto livre entrava no arquivo como CID: só formato
CID-10 sai, o resto vira pendência; (6) CNS provisório `700000000000000` passava na conta do dígito: recusado; (7) resposta
do LEDI sem `Cache-Control: no-store`. Sem achado: IDOR de consulta/profissional entre organizações (404), auditoria sem
CNS/CPF/CNES/CBO. Pendência: o Bundle `/export` ainda usa o cadastro de quem exporta (comportamento anterior ao #52).

## #53 dossiê de conformidade vivo e auditoria à prova de adulteração (2026-10-24)

Branch `claude/p1-restantes`. Migração c304 (sobre c303): `audit_log.prev_hash/hash` + gatilhos contra UPDATE/DELETE
(Postgres, com TRUNCATE; SQLite). Cabeça única c304.
- Cadeia (`app/audit/chain.py`): HMAC-SHA256 (`AUDIT_CHAIN_KEY`, senão JWT_SECRET) por organização, selada em
  `log_action` (lock consultivo no Postgres). `GET /audit/verify` aponta a primeira linha quebrada. Linhas sem hash antes
  da primeira encadeada = "anteriores ao encadeamento" (não quebram); sem hash depois = quebra. UPDATE/DELETE bloqueados
  no ORM e em massa (`AuditoriaImutavel`) e no banco. Limite assumido: apagar só a cauda exige comparar a `cabeca` guardada
  fora do banco; quem tem a chave e o banco refaz a cadeia; girar a chave quebra a verificação das linhas antigas.
- `GET /audit/export?formato=json|csv&since&until` (gestor): SHA-256 do arquivo + HMAC em cabeçalhos (e no corpo do JSON),
  sem nome/e-mail (só id e perfil), CSV sem injeção de fórmula, teto de 50 mil linhas; a exportação é auditada.
- Leituras auditadas com deduplicação de 5 min por usuário/recurso (`log_leitura`): `consulta.visualizada` (antes gravava
  a cada GET), `consulta.nota_visualizada`, `consulta.documento_visualizado`, `paciente.visualizado`,
  `paciente.timeline_visualizada`. Exportações da consulta já eram auditadas.
- Acesso (`app/consult/acesso.py`, dependência no router da consulta + `stream_context` do WebSocket): modelo do projeto
  não tem equipe/CareTeam, então o vínculo do médico é só ser o autor da consulta. Sem vínculo: 403 (tentativa auditada);
  `POST /consultations/{cid}/quebra-de-vidro` com motivo (10+ caracteres efetivos, invisíveis não contam) libera aquela
  consulta por 8 h, grava `consulta.quebra_de_vidro` e abre alerta "atencao" ao gestor. Outra organização segue 404.
- Dossiê: `data/compliance/dossie.yaml` (RoPA de 7 operações, RIPD resumido, matriz de 14 linhas) + parte calculada
  (retenção, cadeia, quebras de vidro, criptografia, provedor de LLM, suboperadores). `GET /compliance/dossie?formato=json|md`
  (gestor, auditado); tela `/conformidade`; cartão de integridade/exportação em `/auditoria`.
  `tests/test_compliance_dossie.py` garante que todo código e teste citado existe e que "não implementado" não cita controle.
- **Precisa de revisão jurídica humana**: todas as bases legais do RoPA, o texto do RIPD, os artigos da Resolução CFM
  2.454/2026 (marcados "a confirmar"), CEM art. 73 e CFM 1.821/2007, transferência internacional (provedor de LLM) e prazo
  de guarda. O dossiê não afirma conformidade.
- Pendente: relatório de acessos para o paciente em PDF (LGPD art. 18) e FHIR AuditEvent; vínculo por equipe/CareTeam
  (enfermagem hoje sem trava; listagem global de consultas ainda mostra metadados ao médico); tela web para o médico pedir
  a quebra de vidro (hoje só API; o 403 traz a instrução); `patient_id` em `consulta.audio`; incidente (art. 48) e
  encarregado ficam com a instituição. Testes: pytest direcionado (385 passaram); suíte completa e e2e não rodados.
