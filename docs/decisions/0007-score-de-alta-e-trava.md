# 0007 — Score de reinternação e trava suave da alta

**Status:** aceita · **Fase:** 4

## Decisões
1. **LACE calculado do FHIR** (`app/discharge/scoring.py`): L = dias da internação atual; A = `Encounter.priority`
   EM / `admitSource` emd; C = Charlson **simplificado** por prefixo de CID-10 (`data/reference/charlson_icd10.csv`,
   dentro do mesmo grupo vale o maior peso, ex.: DM com lesão de órgão-alvo substitui DM sem complicação);
   E = `Encounter` class EMER nos 182 dias antes da admissão.
2. **Score social** = soma ponderada do checklist (`data/reference/social_weights.json`), marcado na tela e na API
   como **heurística provisória, não validada**. Perguntas condicionais (`so_se`): "cuidador capacitado" só conta se
   há cuidador; "já tem dispositivos" só se precisa; "mora sozinho" só pontua quando não há cuidador designado
   (a rede de apoio mitiga o risco de morar só).
3. **Combinação** (o plano dava "ex.: LACE ≥ 10 ou social ≥ X → alto"; adotamos uma variante para que
   "aplicar sugestões e reavaliar" possa de fato destravar a alta):
   - ALTO: social alto, ou LACE alto com social moderado/alto
   - MODERADO: LACE alto com social baixo, ou LACE moderado, ou social moderado
   - BAIXO: LACE baixo e social baixo
4. **Trava suave**: risco ALTO bloqueia `POST /discharge-assessments/{id}/confirm` (HTTP 409, auditado como
   `discharge.confirmacao_bloqueada`) até (a) nova avaliação com risco menor ou (b) override com justificativa
   ≥ 20 caracteres (perfil médico), gravado em `discharge_overrides` e `audit_log`. O HousIA não escreve a alta no
   prontuário: "confirmar" registra a decisão no próprio banco.
5. **Pré-preenchimento** do checklist a partir do FHIR (moradores do domicílio, cuidador registrado, Morse ≥ 45),
   sempre exibido como sugestão com fonte; o profissional confirma cada resposta.
6. **Sugestões** do LLM (ou mock) citam recursos FHIR ou respostas do checklist (`Checklist/<campo>`);
   sem fonte válida, a sugestão é descartada.
7. **Relógio injetável** (`app/clock.py`): toda lógica clínica dependente de "agora" usa `clock.now()`; os testes
   congelam o relógio às 15:00 de São Paulo e ficam determinísticos.
