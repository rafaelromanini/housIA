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
     Este é o caminho para produção.
   - *Digitar* e *simular roteiro* (`data/consultations/*.json`): demonstração sem microfone, e2e e CI.
   - Quem fala: botões e atalhos (Alt+1/2/3) ou "automático" (heurística na hora; a IA corrige depois apenas as
     falas marcadas como automáticas).

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
   "Aprovar e exportar": copiar o texto ou baixar o FHIR. Nada é escrito automaticamente.

7. **Centro cirúrgico sem códigos inventados.** O relatório por ditado aponta os campos obrigatórios que faltam
   (horário, equipe, contagem de compressas: o que costuma gerar glosa), concilia materiais citados com os da guia
   usando um catálogo OPME **de demonstração** (códigos fictícios `DEMO-OPME-*`) e cruza antibiótico com alergias. Não
   sugerimos TUSS/CBHPM porque não há tabela licenciada no projeto. O CID vem do prontuário ou de diagnóstico
   inequívoco.

8. **Assistentes por especialidade = perguntas + foco.** Cada perfil (clínica, cardiologia, endocrinologia,
   geriatria, urologia, pediatria, enfermagem, cirurgia geral) adiciona perguntas próprias
   (`consult_questions.csv`, coluna `especialidade`) e muda o foco do prompt.

9. **Protocolos de atenção primária determinísticos.** `care_protocols.csv` checa no prontuário itens da linha de
   cuidado (HbA1c, TFG, albuminúria, pés, fundo de olho, vacina, PA no alvo, potássio, ECG) por diagnóstico ativo.
   É uma base de demonstração com recomendações usuais e não substitui a diretriz local.

## Consequências

- Novo cenário ambulatorial (cen6) e agenda do dia (Encounter `AMB`). O construtor de cenários aceita
  `ambulatorio` no lugar de `internacao`.
- Tabelas curadas novas em `data/reference/` (sintomas, emoções, perguntas, hipóteses, exames em linguagem simples,
  alergias cruzadas, alertas de dose renal/Beers, protocolos, OPME de demonstração).
- Para uso real, valem as exigências da [ADR 0004](0004-regulatorio-e-lgpd.md), mais: consentimento do paciente para
  transcrição, STT on-premise ou contratado com contrato de operador, política de retenção da transcrição e
  validação clínica das sugestões em tempo real.
