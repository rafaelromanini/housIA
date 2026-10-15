# 0006 — Regras da reconciliação medicamentosa

**Status:** aceita · **Fase:** 3

## Decisões
1. **Origem = prescrição em uso na saída do setor de origem**: prescrições do sub-encounter de origem que não
   estavam suspensas e vigoravam até a transferência. Suspensões explícitas até 24h antes da transferência aparecem
   como "suspensa" (com motivo). Destino = prescrições do sub-encounter de destino.
2. **Comparação por princípio ativo** (código `principio-ativo`, depois ATC, depois texto). Status: `mantida`,
   `alterada` (dose/via/horário diferentes), `suspensa` (com registro), `omitida` (sem registro — vira alerta) e `nova`.
   Troca registrada com `priorPrescription` não é omissão.
3. **Omissão crítica** para classes cuja interrupção abrupta é perigosa (betabloqueador, clonidina,
   anticoagulantes, insulina basal, anticonvulsivantes, corticoides, antiarrítmicos, benzodiazepínicos);
   as demais omissões são "atenção".
4. **Duplicidade**: mesmo princípio ativo 2x ou dois princípios da mesma classe; analgésicos simples
   (dipirona + paracetamol, analgesia multimodal comum) são exceção.
5. **Interações** em `data/reference/interactions.csv` (54 pares curados, tokens `@classe` ou princípio ativo);
   **MPP** em `high_alert_meds.csv` (dupla checagem: "atenção" se novo na transição, "info" se mantido);
   **alergia** por princípio ativo ou classe, mais reatividade cruzada penicilina ↔ cefalosporina ("atenção").
   Todas são bases de **demonstração**, não exaustivas.
6. **Sinais novos** (48h) detectados de forma determinística (sinais vitais, exames e palavras-chave de evoluções,
   ignorando frases que só citam alergias/antecedentes). A IA recebe linhas relevantes, achados, sinais, evoluções
   e últimas administrações e só gera **hipóteses** (com `confidence` e `refs`), exibidas separadas das regras.
   O modo mock cruza os mesmos sinais com `data/reference/adverse_patterns.csv`.
7. Achados de alergia, interação, omissão e duplicidade também entram como **fatos duros da passagem de plantão**.
