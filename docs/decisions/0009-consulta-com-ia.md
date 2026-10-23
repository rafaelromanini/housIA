# 0009 — Consulta com IA: transcrição, Smart Apps em tempo real e envio ao prontuário

**Status:** aceita (16/10/2026) · **Contexto:** pedido de paridade com a Lya Health (benchmark em
[`docs/benchmark-lya.md`](../benchmark-lya.md)). Muda duas linhas do plano original: a transcrição de áudio, que estava
na v2 (seção 12), entra agora; e o HousIA passa a **escrever** no prontuário, mas só uma coisa: a nota revisada e
aprovada pelo profissional.

## Decisões

1. **Captura plugável, sem gravar áudio.**
   - *Microfone do navegador* (Web Speech API, Chrome/Edge): texto parcial ao vivo, zero infraestrutura. O áudio
     passa pelo serviço de fala do navegador, o que exige contrato e avaliação de LGPD para uso real. Por isso a
     tela avisa e o modo é tratado como demonstração.
   - *Servidor* (`STT_URL`): qualquer serviço compatível com `/v1/audio/transcriptions` (Whisper on-premise, como o
     faster-whisper-server, ou um provedor contratado). A tela envia trechos de 8 s; o HousIA repassa e descarta.
     Este é o caminho para produção. Com o servidor de fala disponível, o padrão passa a ser o áudio em fluxo
     (`/ws/consultations/{id}/audio`, PCM 16 kHz em blocos de 500 ms): texto parcial na tela, frase fechada no
     silêncio (VAD) e janela deslizante de 8 s com 1 s de sobreposição, deduplicada pelo tempo das palavras. O áudio
     fica só em memória. O POST `/audio` de 8 s continua como alternativa.
   - *Digitar* e *simular roteiro* (`data/consultations/*.json`): demonstração sem microfone, e2e e CI.
   - Quem fala: botões e atalhos (Alt+1/2/3) ou "automático" (heurística na hora; a IA corrige depois apenas as
     falas marcadas como automáticas). O modo Microfone começa em "automático" (exceto com paciente em outro
     idioma, em que quem fala define o idioma do reconhecimento). No centro cirúrgico a equipe de sala também dita:
     anestesista, circulante e instrumentador (Alt+4/5/6). Falas gravadas antes continuam válidas (os valores antigos
     não mudaram).

2. **Duas velocidades: regras a cada fala, IA em ciclos.** A cada fala, a camada determinística (`app/consult/engine.py`,
   em milissegundos) atualiza queixas, emoções, perguntas (as já abordadas saem da lista), hipóteses, alertas de
   dose e interação, alergias, exames, protocolos e anamnese. A IA roda em ciclos de até 12 s quando há falas novas
   (cerca de 30 s com o Claude) e sua saída é **re-mesclada** com as regras a cada fala seguinte, sem esperar o próximo ciclo.
   As regras de segurança nunca são removidas nem substituídas pela IA.

3. **Duas chamadas paralelas na análise ao vivo.** Um schema único com todos os Smart Apps excedeu o limite da
   saída estruturada do Claude ("compiled grammar is too large"). A análise foi dividida em *conversa* (queixas,
   emoções, perguntas, resumo, quem falou) e *clínica* (hipóteses, medicamentos, cirurgia, histórico relatado,
   anamnese), executadas em paralelo com `effort="low"`. A nota final usa `effort="medium"`.

4. **Fonte em tudo, inclusive na conversa.** Cada item cita `Fala/N` (trecho da transcrição) e/ou o recurso FHIR.
   Refs inválidas são descartadas e registradas (`descartados`), como nos pilares. Na tela, "fala N" rola até o
   trecho citado e o destaca.

5. **Pseudonimização da conversa.** Nomes do paciente e dos familiares (`RelatedPerson`), CPF e telefone saem da
   transcrição antes de ir ao LLM. A tela e o texto exportado trocam os tokens pelos nomes (`reidentify`). O
   `audit_log` registra contagens, nunca o texto da conversa.

6. **Escrita no prontuário restrita e explícita.** `POST /consultations/{id}/send` cria um `DocumentReference`
   (LOINC 11488-4 nota de consulta; 11504-8 relatório cirúrgico) com a nota **revisada** pelo profissional. Fica atrás
   de `EHR_WRITEBACK` (padrão `false`; `true` no compose porque o HAPI é sintético). Sem a flag, o botão vira
   "Aprovar e exportar": copiar o texto ou baixar o FHIR. Nada é escrito automaticamente. No relatório cirúrgico, o
   exame de imagem de controle citado no ditado vai junto, como `ServiceRequest` **proposto** (`intent=proposal`,
   ligado ao documento), e só se o cirurgião não o retirar na revisão; a passagem de plantão e o Waze da alta o
   mostram como exame pendente enquanto não houver resultado.

7. **Centro cirúrgico sem códigos inventados.** O relatório por ditado aponta os campos obrigatórios que faltam
   (horário, equipe, contagem de compressas: o que costuma gerar glosa), concilia materiais citados com os da guia
   usando um catálogo OPME **de demonstração** (códigos fictícios `DEMO-OPME-*`) e cruza antibiótico com alergias. Não
   sugerimos TUSS/CBHPM porque não há tabela licenciada no projeto. O CID vem do prontuário ou de diagnóstico
   inequívoco.

8. **Assistentes por especialidade = perguntas + foco.** Cada perfil (clínica, cardiologia, endocrinologia,
   geriatria, urologia, pediatria, enfermagem, cirurgia geral) adiciona perguntas próprias
   (`consult_questions.csv`, coluna `especialidade`) e muda o foco do prompt.

9. **Protocolos de atenção primária determinísticos e vivos na conversa.** `data/reference/protocols/*.yaml` (um arquivo
   por eixo: criança, mulher, adulto crônico, idoso, saúde mental e telessaúde; ver o README da pasta) checa no
   prontuário os itens da linha de cuidado por diagnóstico, idade, sexo ou tema da conversa, cada um com a fonte
   (órgão, documento, ano e página ou seção). `chart.protocols(falas, escalas)` lê as falas pelas regras de negação das
   queixas: item tratado vira "abordado — Fala/N", puxa a próxima pergunta do mesmo protocolo e sai das pendências da
   nota; gatilho de risco (ideação suicida, item 9 do PHQ-9) vira alerta crítico. `protocols/local/*.yaml` (ou
   `PROTOCOLOS_LOCAIS_DIR`) troca campos, desliga ou acrescenta itens por instituição ou município. As páginas citadas
   são capítulos e seções: a instituição confere a edição que adota. Não substitui a diretriz local.

10. **Regras medidas por um corpus público de regressão.** Alerta falso destrói o diferencial "regras antes da IA".
    `data/consultations/regressao_falas.json` reúne frases de consulta real (sem pontuação, com vocativo, negação,
    passado e pergunta sem "?") com o que as regras devem e não devem extrair; `tests/test_consult_regressao.py`
    mede falso positivo e falso negativo por categoria a cada build e falha com qualquer falso positivo. Limitações
    conhecidas ficam no corpus como xfail estrito, à vista, até a regra ser corrigida. Para isso o texto ganhou
    alcance de negação, status do medicamento citado (em uso, suspenso relatado, negado) e quebra de oração por
    marcadores da fala ("só", "mas", "agora", vocativo). A negação vale também para cirurgia ("não há necessidade
    de operar"), antecedente familiar e alergia ("não tenho alergia a dipirona"); hipótese não é queixa ("se tiver
    febre, procure o pronto-socorro"); alergia citada não é uso do medicamento; e o suspenso que voltou ("mandou
    parar, mas eu continuo tomando") segue em uso, para não apagar um alerta verdadeiro.

11. **Centro cirúrgico por modelos em dados, com verificações explícitas.** Os campos do relatório saem de
   `data/reference/surgical_templates/*.yaml` (modelo base + regras por especialidade + 15 procedimentos em 7
   especialidades), versionados e editáveis pelo hospital; cada campo traz sinônimos, obrigatoriedade e o motivo com a
   fonte. O modelo é escolhido pelo procedimento ditado, depois pelo agendado (`ServiceRequest`/`Appointment`) e, por
   fim, pela especialidade. `app/consult/surgical_checks.py` confere a Cirurgia Segura da OMS (voz ou botão), o termo
   de consentimento (`Consent`), a lateralidade ditada contra o `bodySite` agendado, a contagem ("não confere" é
   crítico), o tempo de garrote e a antibioticoprofilaxia (`surgical_prophylaxis.csv`, ASHP/IDSA 2013: agente,
   janela antes da incisão, dose pelo peso do prontuário, redose pela duração). Tudo determinístico: a IA só
   reescreve o texto dos campos e não remove nem contradiz uma verificação.
   Só a equipe registra etapa, dose, horário, lado e procedimento: fala de paciente ou acompanhante identificada à mão
   fica fora (o termo de consentimento e a alergia valem ditos por qualquer pessoa); o falante automático conta como equipe.

12. **Perguntas sugeridas e anamnese por queixa em dados, com fonte.** `consult_symptoms.csv` (66 sintomas, com os
   pediátricos e faixa de idade e sexo) e `consult_questions.csv` (id estável, fonte, `idade_min`/`idade_max`/`sexo`
   e as hipóteses que a resposta ajuda a separar) dão pelo menos 2 perguntas por sintoma, 7 de clínica médica e 14 de
   pediatria (sinais gerais de perigo e hidratação da AIDPI). A pergunta só sai da lista como "abordada" quando o
   **profissional** a faz, inclusive parafraseada (`asked_words`, `@sintoma` e, na IA, `perguntas_abordadas` com ids
   validados pelo `RefValidator`); a resposta do paciente na fala seguinte fica guardada com a pergunta e alimenta a
   anamnese. Paciente dizer a palavra não responde pergunta que ninguém fez. As perguntas que ajudam a decidir entre as
   2 hipóteses de maior confiança vêm primeiro dentro da mesma prioridade. A semiologia (`app/consult/semiology.py`,
   `consult_semiology.yaml`) lê início/duração, localização, intensidade, piora, melhora, frequência e sintomas
   associados da fala da queixa e das 2 falas seguintes do paciente, cada campo como `{valor, refs}`; o que falta vira
   pergunta `semiologia:<campo>`, a HDA do modo regras é redigida por queixa, sem aspas, e o médico pode fixar a
   queixa principal. No merge com a IA vale o campo da regra que tem fonte.

12. **OPME em três vias e rastreio do implante.** O que o cirurgião narra (`Fala/N`, com quantidade e tamanho
   — "parafuso 3,5 por 40"), o que a guia autorizou e o que a etiqueta GS1 mostrou (`app/consult/gs1.py`: GTIN, lote,
   validade e série, lidos pela câmera do tablet com `BarcodeDetector` ou por leitor USB) são conciliados linha a
   linha, cada via com o seu status (`app/consult/opme.py`). A guia vem de entrada manual (autocomplete do catálogo
   de demonstração), de um `ClaimResponse` FHIR ou de um XML de guia TISS (leitor tolerante, **sem** validação
   contra o XSD da ANS), então o roteiro de demo deixou de ser a única fonte. Validade vencida é crítica. Na revisão
   o cirurgião edita só a quantidade citada e a justificativa; guia e etiquetas continuam do servidor. Ao enviar, o
   implante vira `Device` (UDI com o GTIN, lote, série, validade), a cirurgia vira `Procedure` com `usedReference`
   e o cartão do implante entra nos documentos de alta; o dispositivo também é fato da passagem de plantão.
13. **Anti-glosa em dados, com score de prontidão.** As regras não ficam no código: `data/reference/glosa_rules/*.csv`
   (regra, tipo, condição, severidade, peso, mensagem, fonte e tipo da fonte — manual, contrato ou motivo de glosa
   TISS) com um conjunto genérico, um convênio de demonstração e o SUS; tabelas de CID × procedimento, OPME ×
   procedimento, auxiliares, duração e anestesia por procedimento. `app/consult/antiglosa.py` só sabe avaliar cada
   tipo de regra sobre o relatório, a cada fala e de novo na revisão, e devolve o score 0–100 ("pronto para
   faturar", com teto enquanto houver pendência crítica) e as pendências com a fonte. Os códigos de procedimento são
   `DEMO-TUSS-*` e `DEMO-SIGTAP-*` e as tabelas clínicas são curadoria de demonstração, a substituir pelas tabelas
   licenciadas. O envio da cirurgia tem trava suave: pendência crítica devolve 409 até haver justificativa, que fica
   no audit; o audit `cirurgia.prontidao` registra as pendências antes e depois no finish e no envio (glosas
   evitadas). O envio ainda grava um `Claim` em rascunho e o export traz um Bundle de transação.

14. **Copiloto "Discutir o caso" / "Perguntar ao prontuário", com fonte em cada frase.** `POST /consultations/{id}/ask`
    (`app/consult/ask.py`) tenta primeiro intenções por regra sobre o `ChartView` e o `Engine`: último valor de um
    exame, TFG, alergias, o que mudou desde a última consulta, dose por TFG/idade, "posso usar X?" (o medicamento
    perguntado passa pelas mesmas regras de dose, interação e alergia da consulta, e o veredito vem delas), interação
    entre dois medicamentos e alertas. Cada frase cita refs FHIR/`Fala/N` e a fonte da regra. Só o que sobra vai ao
    LLM (`consult_ask.md`), com o contexto pseudonimizado e os resultados das regras como ferramentas: o LLM não
    calcula (número fora dos dados recebidos derruba a frase) e o `RefValidator`-equivalente descarta a frase sem ref
    válida. Em mock só as regras respondem. O histórico fica em `extra_json["copiloto"]`; "inserir na nota" leva o
    texto e as refs para a seção escolhida (na hora, se a nota já existe; ao finalizar, se não) e registra a
    proveniência `origem: "copiloto"` em `nota.copiloto`. O audit (`consulta.copiloto`) guarda intenção e contagens,
    nunca o texto. A pergunta por voz (Alt+Q, segurar) usa um reconhecimento próprio e vai ao copiloto: não vira
    `Fala/N`. Os comandos "adicionar ao plano …" e "criar pendência …" entram na nota pelo mesmo caminho.

15. **Sinais de comunicação (#43): intensidade por léxico e dúvidas sem resposta.** A intensidade da emoção vem de intensificadores da fala (`consult_intensity.csv`: "muito medo", "pavor", "um pouco"), não da contagem de falas, inclusive na saída da IA. `Insights.preocupacoes` liga cada dúvida ou preocupação do paciente ou da família às falas seguintes do profissional por termos em comum (`app/consult/sinais.py`); sem resposta, aparece antes de encerrar e, ao finalizar, vira `Alert(source="comunicacao")` só com o assunto, lido pela passagem de plantão e pelo plano de alta com teach-back recomendado. É sinal de comunicação, não diagnóstico, e fica desligado sem o consentimento de emoções.

16. **Cartão "Antes de entrar" (#44).** `GET /patients/{id}/pre-consulta` (`app/consult/pre_consulta.py`) monta por regras, com fonte em cada linha, o motivo agendado, o que mudou desde a última consulta (exames com tendência, remédios iniciados ou suspensos, idas ao PS e internações), as pendências (`ServiceRequest` sem resultado, pendências da última nota do HousIA, tarefas de jornada) e o resumo da última alta (só com o módulo "alta" no plano). Com `?consulta=N`, a pendência citada na conversa fica `tratada`; com `?resumo=true` vêm até 3 frases (IA em modo real, com o cartão pseudonimizado e frase sem ref do cartão ou com número novo descartada; regras em mock).

## Consequências

- Novo cenário ambulatorial (cen6) e agenda do dia (Encounter `AMB`). O construtor de cenários aceita
  `ambulatorio` no lugar de `internacao`.
- Cenários cirúrgicos cen4b (artroplastia de joelho direito, 130 kg) e cen4c (cesariana), com roteiros de ditado. O
  construtor aceita `cirurgias` (gera `ServiceRequest` com `bodySite`, `Appointment` da sala e `Consent`) e `peso`
  nos sinais; o HousIA passa a ler `ServiceRequest`, `Appointment` e `Consent` do paciente.
- Tabelas curadas novas em `data/reference/` (sintomas, emoções, perguntas, hipóteses, exames em linguagem simples,
  alergias cruzadas, alertas de dose renal/Beers, protocolos, OPME de demonstração).
- Para uso real, valem as exigências da [ADR 0004](0004-regulatorio-e-lgpd.md), mais: consentimento do paciente para
  transcrição, STT on-premise ou contratado com contrato de operador, política de retenção da transcrição e
  validação clínica das sugestões em tempo real.

12. **Consentimento registrado e transcrição editável.** Toda consulta nasce com um `consentimento` (quem consentiu,
   finalidades `transcricao`, `ia_externa`, `traducao`, `emocoes`, ou recusa), guardado em `extra_json` e, com
   `EHR_WRITEBACK`, criado como `Consent` R4 (`scope=patient-privacy`, uma sub-`provision` por finalidade) que aparece na
   timeline. Recusa ou `ia_externa=false` bloqueiam análise, tradução e STT (403) e a nota sai só das regras
   (`nota_meta.modo="regras"`, `sem_ia`); `emocoes=false` desliga o cálculo de emoções. Pausa, retomada e revogação são
   marcos (`origem="marco"`) na transcrição: não são fala para as regras, a IA nem a cobertura. A fala pode ser corrigida
   (`PATCH /consultations/{id}/segments/{n}`): as regras rodam de novo, o original fica guardado, o audit registra só o
   número da fala e os campos, e a nota já gerada fica `desatualizada` com as seções que citam a fala. O `/finish` aceita
   as falas ainda na fila de captura, anexadas na mesma transação.

13. **Tradução e idiomas (≥ 95).** O idioma deixou de ser um `Literal`: vale qualquer código de
   `data/reference/languages.csv` (BCP-47, com rótulo e os códigos da Web Speech e do Whisper; `pt`/`es-MX` são
   normalizados). Sem idioma no corpo, a consulta nasce com o `Patient.communication` (preferred) do FHIR. Cada fala
   guarda o próprio `idioma` (chip editável, `PATCH` aceita `idioma`). O motor lê fala estrangeira pela `traducao`
   (a `Fala/N` não muda); sem tradução, ou com tradução divergente, lê o original pelos sinônimos de es/en dos CSVs, com
   a negação estrangeira levada ao "não" só nessas falas (em português "no" é "em o"). A fidelidade é conferida por
   regra, sem LLM (`consult/fidelidade.py`): números, unidades, doses e medicamentos de `drugs.csv` entre original e
   tradução; divergência vira o alerta "Tradução divergente" e tira a tradução das regras. No modo mock a tradução vem
   das gravações dos roteiros (`traducao` em `data/consultations/*.json`, ex.: `cen_hispanica`); fala sem gravação
   recebe o aviso do mock, que as regras ignoram. A nota exportada e a `DocumentReference` registram "Consulta com
   interpretação por IA (es→pt-BR)"; as orientações da alta aceitam os mesmos idiomas (pt-BR no mock).
