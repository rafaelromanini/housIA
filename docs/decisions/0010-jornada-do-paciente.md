# 0010 — Waze da jornada: pacientes em risco de abandono, contato pós-alta e métricas

## Contexto

O Waze da alta termina na confirmação da alta. Quem some depois (não atende, falta ao retorno, ninguém agenda) nunca
volta ao radar, porque as regras do HousIA só rodavam dentro da consulta ou da internação.

## Decisão

Módulo `apps/api/app/journey/` (rules, service, metrics, router) e tela `/jornada` (enfermagem e gestão).

- **Lista determinística** (`GET /journey/worklist`), sem LLM: seis regras sobre o FHIR e o banco do HousIA, cada uma
  com fonte, severidade, prazo e papel sugerido. Parâmetros e textos de fonte em `data/reference/journey_rules.yaml`.
  1. alta de alto risco sem contato em 72 h (LACE + social, do Waze da alta);
  2. retorno pós-alta não agendado (nenhum Appointment depois da alta, carência de 48 h);
  3. falta (Appointment `noshow`) sem remarcação nem contato depois;
  4. protocolo de APS vencido (mesma base `care_protocols.csv` da Consulta com IA);
  5. sinal de não adesão numa consulta recente (sugestão de adesão das regras da consulta, com as falas citadas);
  6. medicamento trocado (`priorPrescription`) sem reconciliação medicamentosa executada depois da troca.
- **Alta**: o fim do `Encounter` IMP ou, se o prontuário ainda o mostra aberto, a confirmação da alta no HousIA (o
  HousIA não escreve no encontro). Internado sem alta confirmada não entra na lista.
- **Risco**: a avaliação de alta (`DischargeAssessment`) quando existe; sem ela, estimativa do prontuário (LACE + o pouco
  de social que o FHIR sabe, o resto neutro), sempre rotulada "estimado".
- **Resolução**: contato efetivo resolve alta sem contato, falta e não adesão; Appointment novo resolve retorno e
  falta; reconciliação resolve a troca. Tentativa sem sucesso não resolve (3 tentativas sobem a severidade e sugerem
  visita domiciliar).
- **Tarefas**: criadas a partir do item que o servidor reavalia na hora (texto e fontes não vêm do cliente), uma aberta
  por paciente e regra. Viram `Task` FHIR com `performerType` (papel), `restriction.period.end` (prazo) e `input`
  (fontes). O estado fica no banco (`journey_tasks`); concluir/cancelar atualiza o Task espelhado.
- **Contato pós-alta**: roteiro em `journey_contact_script.yaml` (identidade, teach-back, retirada dos remédios,
  sinais de alarme gerais e por condição/medicamento). Todos os sinais de alarme precisam ser perguntados. Resposta
  "sim" abre alerta crítico (`source=jornada`); o contato vira `DocumentReference`. O texto da conversa não vai ao
  audit, só ids. **SMS/WhatsApp fora de escopo**: o HousIA gera um texto pronto para a equipe copiar e não envia nada.
- **Métricas** (`GET /journey/metrics`), sobre `Encounter` IMP: reinternação em 30 dias e retornos perdidos por coorte
  (risco LACE, mês, CID principal). Só altas com seguimento completo entram nas taxas; cada coorte devolve os casos
  com a referência do Encounter, para a conta ser conferida.

## Consequências e limites

- Escrita no FHIR passa a incluir `Task` e `DocumentReference` da jornada (além da nota da consulta). O mirror de
  `Task` é por criação/atualização; a fonte da verdade do estado é o banco do HousIA.
- A lista varre todos os pacientes não internados a cada chamada (busca por paciente no HAPI). Para escala real é
  preciso busca FHIR por data/status e cache. Demonstração: ~1,5 s com 54 pacientes.
- Prazos (72 h, 48 h, 60 dias, 30 dias) e os intervalos de protocolo são heurísticas de demonstração, sem validação
  clínica. O risco da coorte nas métricas é só o LACE da internação índice (o score social só existe com avaliação).
- A coorte pós-alta de fundo (`alt01`…`alt18`) é sintética e desenhada para exercitar as métricas; as taxas dela não
  dizem nada sobre desempenho real.
- Migração `d001` (`journey_contacts`, `journey_tasks`).
