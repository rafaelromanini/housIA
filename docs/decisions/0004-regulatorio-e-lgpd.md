# 0004 — Requisitos regulatórios e de LGPD para uso real

**Status:** registrada · **Fase:** 1 (exigida pela seção 11 do plano)

O MVP roda **somente com dados sintéticos**. Antes de qualquer uso com dados reais de pacientes será necessário:

1. **Enquadramento como Software como Dispositivo Médico (SaMD)** junto à ANVISA (RDC 657/2022 e RDC 751/2022),
   definindo classe de risco, regularização e responsabilidades do fabricante.
2. **RIPD/DPIA** (Relatório de Impacto à Proteção de Dados Pessoais) sob a LGPD — dados de saúde são dados
   pessoais sensíveis (art. 11).
3. **Contrato de operador de dados** com o provedor de LLM (Anthropic), incluindo retenção, localização do
   processamento e uso para treinamento.
4. **Validação clínica prospectiva** dos resumos, regras e scores (LACE + heurística social provisória),
   com métricas de segurança e de concordância com profissionais.
5. Revisão da pseudonimização (hoje: nome, CPF, telefone, endereço e nomes de familiares) para cobrir
   identificadores indiretos em texto livre.

Salvaguardas já presentes no MVP: pseudonimização antes do LLM, `LOG_PROMPTS=false` por padrão,
`audit_log` de gerações/visualizações/overrides/alertas, disclaimer visível e override humano sempre possível.
