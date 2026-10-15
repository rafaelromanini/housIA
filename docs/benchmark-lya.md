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

## O que ficou de fora (e por quê)

- **Transcrição por voz**: o plano coloca áudio na v2. Ditado pelo navegador (Web Speech API) enviaria áudio
  de paciente a um serviço de terceiros sem contrato de operador — risco de LGPD. Fica para a v2 com
  transcrição contratada/on-premise.
- **Codificação CID/CIAP/TUSS automática**: útil para faturamento e APS; fora do foco de transição do MVP.
