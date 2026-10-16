# Benchmark — Lya Health (CTC) × HousIA

> Levantamento de 15/10/2026 a partir de fontes públicas. A análise do site com a skill **REA**
> (`morluto/rea`, observação passiva de navegador) não foi possível neste ambiente: o domínio
> `lyahealth.com.br` é bloqueado pela política de rede do container e não resolve via fetch externo.
> Nada aqui reproduz código ou material proprietário: só funcionalidades descritas publicamente.

## O que a Lya faz (segundo as fontes)

| Funcionalidade | Fonte |
|---|---|
| Transcrição em tempo real da conversa médico-paciente e geração de anotações clínicas | [CTC — blog](https://ctctech.com.br/blog/ctc-utiliza-inteligencia-artificial-para-auxiliar-medicos-no-diagnostico-e-transcricao-de-dados/), [Olhar Digital](https://olhardigital.com.br/2024/10/13/medicina-e-saude/ia-brasileira-sugere-perguntas-e-diagnosticos-medicos-em-hospitais/) |
| Prontuário por voz: identifica seções (sintomas, diagnóstico, medicamentos) e preenche os campos do prontuário | [CTC — prontuário por voz](https://ctctech.com.br/blog/prontuario-por-voz-ia-para-consultas-mais-ageis-e-humanizadas/) |
| Evolução em formato SOAP, sugestão de CID/CIAP, integração via FHIR (atenção primária) | [CTC — Lya](https://ctctech.com.br/solucoes/health/inteligencia-artificial/) |
| Cirurgia: ditado → transcrição → sugestão de CID/TUSS/CBHPM, conciliação de OPME, envio por API | [CTC — Lya](https://ctctech.com.br/solucoes/health/inteligencia-artificial/) |
| Sugestão de perguntas e hipóteses diagnósticas a partir do diálogo (base CID) | [360 News — CONAHP 2024](https://360news.com.br/inovacoes-em-inteligencia-artificial-e-interoperabilidade-sao-destaque-da-ctc-no-conahp-2024/) |
| Resumos clínicos automáticos, alertas durante o atendimento, tradução de idiomas | [Medicina S/A — Sabará](https://medicinasa.com.br/ia-cuidado-pediatrico/), [Future Health — 95 idiomas](https://futurehealth.cc/plataforma-lya-traducao-tempo-real-95-idiomas/) |
| Implantação no Hospital Infantil Sabará: 6 mil+ assistências, ~25% dos atendimentos; 78% dos médicos relatam melhora na relação com pacientes, 60% menos sobrecarga (pesquisa interna) | [Computer Weekly](https://www.computerweekly.com/br/noticias/366631954/Ferramenta-de-IA-desenvolvida-no-Brasil-transforma-atendimento-em-hospital-pediatrico), [Medicina S/A](https://medicinasa.com.br/ia-cuidado-pediatrico/) |

Lacunas nas fontes: não há descrição pública clara de uma etapa formal de revisão/aprovação do texto pelo
médico, nem de como cada afirmação é rastreada até a fonte.

## Posicionamento

A Lya é forte **na consulta** (captura e estruturação do que é dito). O HousIA é focado **na transição**
(o que se perde entre turnos, setores e na alta) e no rastreamento de cada afirmação até o recurso FHIR.

## O que trouxemos para o HousIA

| Ideia (inspirada em) | Implementação no HousIA | Por que encaixa |
|---|---|---|
| Estruturação SOAP para o prontuário (prontuário por voz / APS) | **Sumário de alta em SOAP** com `refs` por seção e botão copiar (o HousIA não escreve no prontuário) | Alta é transição; reduz digitação mantendo a fonte de cada linha |
| Captura do que é falado + alertas | **Conferência da passagem verbal**: o profissional cola/digita o que passou verbalmente e o HousIA aponta **omissões** de fatos críticos e **divergências** com o prontuário | Ataca o ponto exato onde a informação se perde: a passagem verbal |
| Tradução de idiomas e foco na relação com família | **Orientações de alta em linguagem simples** para paciente/cuidador (pt-BR, espanhol, inglês), imprimíveis | Teach-back e home care (v2 do plano) dependem de material compreensível |
| Métricas de adoção e satisfação (Sabará) | **Feedback útil/não útil** em cada saída da IA + taxa de aceitação de alertas na auditoria | Prepara o piloto (seção 15 do plano: alertas aceitos × descartados) |

## Paridade com a Lya: o que o HousIA entrega e onde vai além (16/10/2026)

A pedido do produto, o HousIA ganhou a **Consulta com IA** ([ADR 0009](decisions/0009-consulta-com-ia.md)). A lista
de funções da Lya veio do site do produto (Smart Apps) e de fontes públicas. O site não pôde ser aberto pela skill
REA neste ambiente: o domínio está bloqueado na rede do container.

| Lya | HousIA | Onde vai além |
|---|---|---|
| Transcrição da consulta em tempo real | Microfone do navegador (texto parcial ao vivo), transcrição no servidor plugável (Whisper on-premise), digitação ou roteiro simulado; quem fala por botão/atalho ou automático | Não grava áudio; caminho on-premise para LGPD; a IA corrige quem falou |
| **Sugestão de perguntas** | Atualiza a cada fala; a pergunta **sai da lista quando já foi feita ou respondida** ("já abordadas"); perguntas antes da conversa a partir do prontuário | Cada pergunta mostra o motivo e a fonte (fala ou registro) |
| **Queixa principal** (tags) | Tags de queixas e **negativas pertinentes** (riscadas) | Fonte de cada tag na conversa |
| **Análise multidimensional** (emoções) | Ansiedade, preocupação, tristeza, frustração, dúvida, dor, alívio e confiança, separados entre paciente e acompanhante, com **como abordar**; linha emocional da conversa; métricas de comunicação (quem falou mais, perguntas abertas × fechadas) | Evidência textual de cada emoção |
| **Histórico médico** | Diagnósticos com data de início e há quanto tempo, cirurgias, consultas e idas ao PS (FHIR); antecedentes familiares relatados | Distingue o que está no prontuário do que só foi relatado |
| **Cirurgia** | Histórico cirúrgico, cirurgias citadas ou planejadas e **checagens pré-operatórias** (anticoagulantes, hipoglicemiantes no jejum, alergias, TFG, PA) | Checagens a partir dos dados reais do paciente |
| **Alergias** | Prontuário + **relatadas na conversa** (com aviso para registrar), reatividade cruzada, o que evitar, alternativas e dicas práticas; **conflito com medicamento em uso** | Alerta de conflito determinístico |
| **Exames** em linguagem acessível | Explicação simples, tendência (subiu/caiu desde a última vez) e **TFG estimada (CKD-EPI 2021)** | Usa a série histórica do FHIR |
| **Sumarizador** | Resumo dinâmico + destaques críticos | Destaques vêm das regras de segurança |
| **Possíveis hipóteses diagnósticas** | Diferenciais com CID-10, confiança, evidências a favor/contra, próximos passos e sinais de alarme | Regras e IA separadas; cada evidência com fonte |
| **Histórico de medicamentos** (ajuste de dose/alternativas) | Em uso, suspensos e **relatados**; ajuste por **função renal** e **Beers**; interações incluindo o que o paciente toma por conta própria; "tríplice ameaça" renal | Camada determinística antes da IA |
| Geração automática de notas (cronológica) | Nota SOAP + anamnese, CID-10, **cronologia**, pendências e orientações ao paciente no idioma dele | Revisão/edição obrigatória; refs por seção |
| Organização da anamnese | Anamnese estruturada ao vivo (QP, HDA, antecedentes, medicações, alergias, hábitos, revisão de sistemas) | — |
| Envio ao prontuário com um clique | `DocumentReference` FHIR R4 após aprovação (`EHR_WRITEBACK`), copiar texto ou baixar FHIR | Padrão aberto (FHIR) em vez de integração proprietária |
| Lya Centro Cirúrgico (ditado, CID, OPME, verificações, faturamento) | Relatório estruturado enquanto o cirurgião dita, **campos obrigatórios faltantes** (anti-glosa), **conciliação de OPME** com a guia, alergia × antibiótico, CID | Não inventa campos nem códigos (catálogo OPME de demonstração) |
| Atenção primária (protocolos) | **Protocolos de acompanhamento** (DM2, HAS, DRC) checados no prontuário: em dia, pendente, sem registro, no alvo | Determinístico e auditável |
| Assistentes por especialidade | 8 perfis (clínica, cardio, endócrino, geriatria, urologia, pediatria, enfermagem, cirurgia) com perguntas e foco próprios | — |
| Tradução em tempo real (95 idiomas) | Tradução simultânea paciente ↔ profissional (9 idiomas na interface, ampliável) com **voz** para o paciente | — |

**Além do que a Lya divulga:** a passagem de plantão por perfil, a conferência da passagem verbal, a
reconciliação na transferência e o "Waze" da alta com trava suave. Tudo isso na mesma timeline FHIR e com
auditoria (gestão).

## O que ficou de fora (e por quê)

- **Códigos TUSS/CBHPM automáticos**: exigem tabela licenciada. O centro cirúrgico usa um catálogo OPME de
  demonstração com códigos fictícios.
- **Gravação de áudio**: decisão de privacidade. O HousIA só guarda texto.
- **Comercial (teste grátis/plano ilimitado)**: fora do escopo do software do MVP.
