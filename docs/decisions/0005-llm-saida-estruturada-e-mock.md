# 0005 — LLM: saída estruturada, esforço em vez de temperatura, fallback e modo mock

**Status:** aceita · **Fase:** 2

## Decisões
1. **Modelo padrão `claude-opus-5-5`** (configurável em `LLM_MODEL`).
2. **Sem `temperature`**: os modelos Claude atuais rejeitam parâmetros de amostragem. O equivalente a
   "temperatura baixa" do plano é controlar profundidade/custo com `output_config.effort` (`LLM_EFFORT`,
   padrão `medium`), mais instruções estritas no prompt e validação no backend.
3. **Saída estruturada nativa** (`output_config.format` com JSON Schema gerado do modelo Pydantic e fechado
   com `additionalProperties: false`). A resposta ainda é validada com Pydantic; se falhar, há **1 tentativa de
   reparo** e depois erro amigável (HTTP 502 com mensagem em PT-BR).
4. **Recusas**: `stop_reason == "refusal"` vira erro amigável. Para reduzir falsos positivos de classificadores
   de segurança, as chamadas usam `fallbacks: "default"` (beta `server-side-fallback-2026-07-01`).
5. **Timeout e 1 retry** no cliente do SDK (`LLM_TIMEOUT_S`, `max_retries=1`).
6. **Modo mock determinístico** (`LLM_MODE=mock`): em vez de fixtures estáticas (que quebrariam com a âncora de
   horário móvel), cada pilar tem um gerador que monta a saída a partir dos fatos determinísticos. Mesmo schema,
   mesma validação de refs — roda sem chave e no CI.
7. **Citações obrigatórias**: itens de lista sem `refs` ou com qualquer ref inexistente são descartados e o
   descarte vai para o `audit_log` (`handoff.refs_invalidas`). Extensão do schema do plano: `situacao_refs`
   (a situação também é uma afirmação e passa a ter fonte; refs inválidas são removidas).
8. **Cache** por `input_hash` = SHA-256 de (público, janela, destino, versão do prompt, modelo, eventos da janela,
   fatos e a hora corrente). Mesmos dados na mesma hora → não chama o LLM de novo. "Regenerar" ignora o cache.
9. **Pseudonimização**: o conteúdo gravado fica pseudonimizado (`[PACIENTE]`); o nome só é reinserido na tela,
   para quem já está autorizado a ver o paciente.
