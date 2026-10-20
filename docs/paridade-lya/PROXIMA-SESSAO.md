# Prompt para a próxima sessão

Copie o bloco abaixo numa sessão nova do Claude Code (repositório rafaelromanini/housIA).

```text
Continue o projeto HousIA no branch claude/dreamy-pascal-x45x7r (repositório rafaelromanini/housIA).
Leia primeiro docs/paridade-lya/STATUS.md: ele diz o que já foi feito, o que falta (na ordem), como subir a stack
(scripts/sandbox-stack.sh) e como rodar cada onda com o workflow .claude/workflows/housia-onda.js. O backlog completo
está em docs/paridade-lya/backlog.json (plano.backlog, itens 0 a 57).

Objetivo: o HousIA tem que fazer TUDO o que a Lya Health faz, só que melhor, mantendo nossos diferenciais (passagem
de plantão, reconciliação, Waze da alta, fonte em cada item). Vá finalizando sem me perguntar, na ordem do STATUS.md:
1. Feche as verificações interrompidas (#4+#9 com o patch em docs/paridade-lya/wip/, e #30), suba a stack, rode
   pytest, lint, typecheck e e2e, corrija o que quebrar e faça push.
2. Termine a Onda 1 (frente A: #5+#6, #7+#8, #10) e depois as Ondas 2, 3 e 4. Ao fim de cada onda: merge da frente B,
   testes, e2e, push e STATUS.md atualizado.
3. Refaça o vídeo pitch em português com a Consulta com IA como foco (transcrição ao vivo, pergunta sugerida saindo
   da lista, alerta com a fala de origem, nota enviada ao prontuário). Scripts em brag-output/scripts/.
4. Se sobrar cota, melhore o front com as 5 skills de UI mais usadas.

Regras: nunca commitar chaves, só dados sintéticos, push só neste branch, sem abrir PR.

GESTÃO DE TOKENS E DE COTA (a sessão anterior estourou o limite com agentes demais em paralelo):
- Você é só o orquestrador: esforço baixo, respostas curtas, nada de ler arquivos grandes inteiros (use grep/sed -n),
  nada de colar relatórios longos dos agentes. Delegue implementação e verificação a subagentes Sonnet
  (Agent com model "sonnet") ou ao Workflow "housia-onda".
- Modelos e esforço: orquestrador (esta sessão) em esforço baixo; implementação e verificação em agentes Sonnet
  com effort "medium"; effort "high" só na verificação de itens de segurança clínica (#5+#6, #14+#15, #19, #24);
  tarefas mecânicas (rodar testes e resumir, atualizar STATUS.md, ajustar seletor de e2e) em agente Haiku.
- No máximo 2 agentes ao mesmo tempo (frente A e frente B). Se bater o limite de uso, pare de lançar agentes, salve o
  estado (commit + push do que estiver verde, STATUS.md atualizado) e espere o reset; retome o agente interrompido
  com SendMessage em vez de começar outro do zero.
- Prompts dos agentes curtos: aponte para o STATUS.md, o backlog (plano.backlog[N]) e as regras de
  .claude/workflows/housia-onda.js (função common) em vez de repetir contexto. Peça relatório final de até 25 linhas.
- Uma verificação cética por entrega, com escopo fechado (critérios de aceite + 12 entradas adversariais), sem
  reescrever o que já passa.
- Commit e push depois de CADA entrega verificada (não só no fim da onda), para nada se perder se a sessão cair.
  O trabalho da frente B fica num worktree local que some com o container: junte no branch principal (merge em um
  worktree temporário destacado, testes, push HEAD:claude/dreamy-pascal-x45x7r) assim que cada entrega for
  verificada.
- Rode e2e e docker só no fim de cada onda, não a cada entrega.
- Prefixos de migração Alembic: Onda 1 usou c0 (frente A) e d0 (frente B, d001 já existe). Use c1/d1 na Onda 2,
  c2/d2 na Onda 3, c3/d3 na Onda 4.
```
