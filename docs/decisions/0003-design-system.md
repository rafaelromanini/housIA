# 0003 — Design system

**Status:** aceita · **Fase:** 1

## Decisão
O design system foi gerado com a skill **ui-ux-pro-max** (`nextlevelbuilder/ui-ux-pro-max-skill`):
`"healthcare clinical dashboard hospital" --design-system --variance 2 --motion 2 --density 8`.
O resultado está em `design-system/housia/MASTER.md` (estilo Minimalism & Swiss, Atkinson Hyperlegible,
paleta ciano clínico + verde saúde).

Ajustes para uso clínico à beira do leito ficam em `design-system/housia/pages/app-clinico.md`, que tem
precedência sobre o MASTER: primário mais escuro para contraste com texto branco, cores de severidade fixas
(crítico vermelho, atenção âmbar, info azul — sempre com ícone + rótulo), sem animações de scroll, alvos
de toque ≥ 44 px.

Componentes no padrão shadcn/ui (Radix + Tailwind 4 + CVA) escritos em `apps/web/components/ui/`.
