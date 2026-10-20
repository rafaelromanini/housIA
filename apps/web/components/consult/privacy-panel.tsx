import { ShieldCheck } from "lucide-react";

import type { PrivacyPanel as Panel } from "@/lib/api";

/** "Privacidade desta consulta": o que fica guardado, por quanto tempo e quem processa, lido da configuração em vigor. */
export function PrivacyPanel({ panel, expurgadaEm }: { panel: Panel; expurgadaEm?: string | null }) {
  const rows: [string, string][] = [
    ["O que fica guardado", panel.guardado],
    ["Por quanto tempo", panel.retencao],
    ["Quem processa a voz", panel.voz],
    ["Quem processa o texto", panel.ia],
    ["Proteção no banco", panel.criptografia],
    ["Direitos do titular", panel.exclusao],
  ];
  return (
    <details className="rounded-lg border bg-card text-sm" data-testid="painel-privacidade">
      <summary className="flex min-h-11 cursor-pointer items-center gap-2 px-3 font-medium">
        <ShieldCheck aria-hidden className="size-4 text-primary" /> Privacidade desta consulta
      </summary>
      <dl className="space-y-2 border-t px-3 py-3">
        {expurgadaEm && (
          <p className="text-xs font-medium text-atencao" role="status">
            O texto das falas já foi apagado pelo prazo de retenção.
          </p>
        )}
        {rows.map(([label, text]) => (
          <div key={label}>
            <dt className="text-xs font-medium text-muted-foreground">{label}</dt>
            <dd>{text}</dd>
          </div>
        ))}
      </dl>
    </details>
  );
}
