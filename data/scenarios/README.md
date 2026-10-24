# Cenários de transição (DSL)

Cada arquivo descreve um paciente sintético. O construtor (`apps/api/app/seed/builder.py`) gera os recursos
FHIR R4. **Todos os horários são relativos à âncora** (o "agora" da carga): `-3d2h`, `-4h`, `-25m`, `+2h`.

```jsonc
{
  "id": "cen1",                       // vira Patient/cen1 e prefixo de todos os IDs
  "tag": "cenario-1", "titulo": "...", "esperado": "...",
  "paciente": {"nome", "sexo": "male|female", "nascimento" /* ou "idade_dias": N, relativo à âncora (criança) */, "cpf", "telefone", "endereco"},
  "internacao": {
    "inicio": "-3d2h", "fim": "-1d" /* opcional: paciente já teve alta */, "tipo": "urgencia|eletiva", "motivo": "...", "alta_prevista": "+3h" /* opcional: previsão de alta (extensão do Encounter), usada em /gestao */,
    "setores": [{"id": "uti", "setor": "UTI Adulto", "leito": "04", "inicio": "-2d22h", "fim": "-3h"}, ...]
  },
  "encontros_anteriores": [{"inicio": "-41d", "motivo": "...", "duracao": "5h"}],   // idas ao PS (LACE "E")
                                      // {"tipo": "internacao", "urgente": true} vira um Encounter IMP anterior (reinternação)
  "diagnosticos": [{"cid": "I10", "texto": "...", "principal": true, "inicio": "-3650d", "registro": "-3d"}],
  "alergias": [{"principio_ativo": "dipirona" | "classe": "penicilina", "texto", "reacao", "criticidade"}],
                                      // sem alergias, o construtor registra "nega alergias conhecidas"; com
                                      // "sem_registro_alergia": true não registra nada (alergia nunca perguntada)
  "familiares": [{"nome", "parentesco", "codigo": "DAU", "cuidador": true, "telefone"}],
  "social": {"moradores": 1},
  "prescricoes": [{
    "id": "losartana", "med": "losartana",           // chave de data/reference/drugs.csv
    "dose": "50 mg", "via": "VO", "horarios": ["08:00", "20:00"],
    "setor": "enf" | "setores": ["uti", "enf"],      // uma MedicationRequest por setor
    "inicio", "fim", "status": "stopped", "motivo", "substitui": "anlodipino",
    "se_necessario": true, "condicao": "...", "administracoes": ["-6h"], "administrar": false
  }],
  "sinais": [{"t": "-25m", "pa": [184, 102], "fc": 92, "fr": 18, "temp": 36.9, "spo2": 96, "glicemia": 210, "dor": 3,
              "peso": 130}],                                      // peso em kg (doses por peso)
  "exames": [{"t": "-10h", "potassio": 4.2, "creatinina": 1.1, "leucocitos": 12.4}],
  "balanco": [{"t": "-7h", "diurese": 400, "obs": "via SVD"}],
  "escalas": [{"t": "-2h40m", "morse": 55, "braden": 16}],
  "procedimentos": [{"t": "-3d", "texto": "...", "categoria": "acesso vascular"}],
  "cirurgias": [{                       // cirurgia agendada → ServiceRequest (bodySite) + Appointment + Consent
    "id": "atj", "procedimento": "Artroplastia total de joelho", "sitio": "Joelho direito",
    "t": "-1h30m", "duracao": "3h", "status": "active|completed", "cirurgiao": "...", "especialidade": "...",
    "sala": "Centro Cirúrgico — Sala 3",           // opcional: gera o Appointment da sala
    "consentimento": {"t": "-1d", "texto": "..."}  // opcional: gera o Consent (termo assinado)
  }],
  "pedidos": [{"id": "rac", "t": "-91d", "categoria": "laboratorio|imagem", "texto": "...", "status": "active", "profissional": "..."}],
                                      // ServiceRequest sem resultado: vira pendência do cartão "Antes de entrar" (#44)
  "agendamentos": [{"id": "ret", "t": "+10d", "status": "booked|noshow|fulfilled|cancelled", "motivo": "...",
                    "servico", "profissional", "duracao"}],              // Appointment ambulatorial (retorno, falta)
  "evolucoes": [{"t": "-4h", "tipo": "medica|enfermagem", "autor": "...", "texto": "..."}]
}
```

O setor de cada evento é inferido pelo horário (ou forçado com `"setor"`). Administrações de medicamentos
aprazados são geradas automaticamente a partir de `horarios`.

| Arquivo | Cenário |
|---|---|
| `cenario1_uti_enfermaria.json` | UTI → Enfermaria, PA instável, anti-hipertensivo trocado, ATB às 20h |
| `cenario2_omissao_transferencia.json` | Betabloqueador omitido na transferência; taquicardia 12h depois |
| `cenario3_alta_alto_risco.json` | Idoso que mora sozinho, 3 idas ao PS em 6 meses, insulina nova |
| `cenario4_alta_baixo_risco.json` | Jovem, apendicectomia sem intercorrências (pedido cirúrgico concluído e termo de consentimento) |
| `cenario4b_artroplastia_joelho.json` | Centro cirúrgico: artroplastia total de joelho direito agendada, 130 kg (dose de cefazolina) |
| `cenario4c_cesarea.json` | Centro obstétrico: cesariana eletiva por cesárea anterior |
| `cenario5_interacao_alergia.json` | Opioide + benzodiazepínico em idosa; dipirona prescrita em alérgica |
| `cenario7_alta_sem_contato.json` | Jornada: alta de alto risco há 4 dias, sem contato pós-alta (lista de risco de abandono) |
| `cenario7b_falta_ao_retorno.json` | Jornada: faltou ao retorno (Appointment noshow) |
| `cenario7c_retorno_nao_agendado.json` | Jornada: alta sem consulta de retorno agendada |
| `cenario7d_troca_sem_reconciliacao.json` | Jornada: anti-hipertensivo trocado sem reconciliação medicamentosa |
| `cenario7e_protocolo_aps_vencido.json` | Jornada: ambulatorial com HbA1c vencida e sem retorno |

Além dos cenários, `app/seed/background.py` gera a coorte pós-alta (`alt01`…`alt18`): altas de 9 a 150 dias atrás, com
retorno feito, falta ou sem retorno, e quatro reinternações em até 30 dias. É ela que dá massa às métricas da jornada.
