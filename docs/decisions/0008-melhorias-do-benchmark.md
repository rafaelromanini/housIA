# 0008 — Melhorias a partir do benchmark com a Lya Health

**Status:** aceita · **Fase:** pós-5 (ver `docs/benchmark-lya.md`)

1. **Conferência da passagem verbal** (`POST /patients/{id}/handoff/check`, prompt `handoff_check` v1.0.0):
   compara o relato livre de quem sai com os fatos duros (crítico/atenção) do público. Status por fato:
   mencionado, omitido ou divergente. O relato é pseudonimizado antes do LLM e **não** vai para o `audit_log`
   (só contagens). Mock: palavras-chave por tipo de fato/medida/medicamento e detecção de "estável" junto a fato
   crítico.
2. **Documentos de alta** (`POST /patients/{id}/discharge-documents`, prompt `discharge_docs` v1.0.0, tabela
   `discharge_documents`): sumário SOAP (pt-BR, para colar no prontuário) + orientações em linguagem simples
   (pt-BR, es, en) com só os medicamentos de uso domiciliar (exclui IV). Seções sem fonte válida ficam marcadas
   "sem fonte — revisar"; itens de lista sem fonte são descartados. Impressão isola o bloco de orientações.
   No modo mock a tradução não é feita (aviso na tela).
3. **Feedback da IA** (`POST /feedback`, tabela `ai_feedback`): "isto foi útil?" em passagem, conferência,
   reconciliação, sugestões de alta e documentos. "Não útil" exige comentário curto. A auditoria mostra
   utilidade percebida e taxa de aceitação de alertas (reconhecidos ÷ reconhecidos + descartados).
4. **Não adotado agora**: ditado por voz no navegador (áudio iria a terceiros sem contrato de operador — LGPD) e
   codificação automática CID/CIAP/TUSS (fora do foco de transição).
