# Override — aplicativo clínico (todas as telas do HousIA)

> Gerado a partir do MASTER.md (skill ui-ux-pro-max) e ajustado para uso clínico à beira do leito.
> As regras abaixo **substituem** o MASTER.md onde houver conflito.

## O que muda em relação ao MASTER

| Tema | MASTER | HousIA (override) | Motivo |
|---|---|---|---|
| Padrão de página | Hero + Testimonials + CTA | App shell: barra superior + faixa de demo + conteúdo + rodapé com disclaimer | Não é landing page; é ferramenta de trabalho |
| Botão primário | `#059669` com texto preto | `--primary: #0E7490` (cyan-700) com texto branco (5,4:1) | Contraste ≥ 4,5:1 com texto branco, mais legível em tablet |
| Ação de confirmação | — | `--accent: #047857` (emerald-700) com texto branco (5,5:1) | "Confirmar alta", "Reconhecer" |
| Borda | `#A5F3FC` | `--border: #CFE0E6` | Ciano saturado cansa a vista em telas densas |
| Hover de card | `translateY(-2px)` | Só borda/sombra, sem deslocamento | Evita layout shift em grids densos (anti-padrão do próprio MASTER) |
| Motion | Scroll reveal com GSAP | Sem GSAP; transições de 150–200 ms; `prefers-reduced-motion` zera transições | Dado clínico não deve "aparecer animado" |

## Cores de severidade (inegociáveis — definidas no plano do MVP)

| Severidade | Texto | Fundo | Borda | Ícone (lucide) |
|---|---|---|---|---|
| crítico | `#B91C1C` | `#FEF2F2` | `#FCA5A5` | `OctagonAlert` |
| atenção | `#92400E` | `#FFFBEB` | `#FCD34D` | `TriangleAlert` |
| info | `#1D4ED8` | `#EFF6FF` | `#93C5FD` | `Info` |

Severidade **nunca** é comunicada só por cor: sempre ícone + rótulo textual ("Crítico", "Atenção", "Info").

Risco de alta usa a mesma escala: alto = crítico, moderado = atenção, baixo = verde (`#047857` / `#ECFDF5`).

## Regras de interação

- Alvos de toque ≥ 44×44 px (uso em tablet).
- Fonte base 16 px; números clínicos com `tabular-nums`.
- Todo conteúdo gerado por IA mostra: selo "Gerado por IA — confira", link "ver fonte" por item e o disclaimer global.
- Hipóteses da IA ficam visualmente separadas de achados de regra (borda tracejada + selo "Hipótese da IA — confirmar").
- Ações destrutivas/irreversíveis (override de alta) pedem justificativa com contador de caracteres e erro junto ao campo.
- Foco visível em todos os elementos interativos (`ring-2 ring-ring ring-offset-2`).
- Tabelas largas ficam dentro de `overflow-x-auto`; sem scroll horizontal da página em 375 px.
