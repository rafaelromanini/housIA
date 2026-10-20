import { CircleCheck, Info, OctagonAlert, TriangleAlert } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import type { AlertCounts, Risk, Severity } from "@/lib/api";

const SEV = {
  critico: { label: "Crítico", Icon: OctagonAlert },
  atencao: { label: "Atenção", Icon: TriangleAlert },
  info: { label: "Info", Icon: Info },
} as const;

export function SeverityBadge({ severity, className }: { severity: Severity; className?: string }) {
  const { label, Icon } = SEV[severity];
  return (
    <Badge variant={severity} className={className}>
      <Icon aria-hidden />
      {label}
    </Badge>
  );
}

export function SeverityIcon({ severity, className }: { severity: Severity; className?: string }) {
  const { Icon, label } = SEV[severity];
  const color = { critico: "text-critico", atencao: "text-atencao", info: "text-info" }[severity];
  return <Icon aria-label={label} className={`${color} ${className ?? "size-5"}`} />;
}

const RISK = {
  alto: { label: "Risco alto", variant: "critico", Icon: OctagonAlert },
  moderado: { label: "Risco moderado", variant: "atencao", Icon: TriangleAlert },
  baixo: { label: "Risco baixo", variant: "ok", Icon: CircleCheck },
} as const;

export function RiskBadge({ risk, prefix }: { risk: Risk; prefix?: string }) {
  const { label, variant, Icon } = RISK[risk];
  return (
    <Badge variant={variant}>
      <Icon aria-hidden />
      {prefix ? `${prefix}: ${label.replace("Risco ", "")}` : label}
    </Badge>
  );
}

export function AlertCountBadges({ counts }: { counts: AlertCounts }) {
  const items = (["critico", "atencao", "info"] as const).filter((s) => counts[s] > 0);
  if (items.length === 0) return <span className="text-xs text-muted-foreground">Sem alertas abertos</span>;
  return (
    <span className="flex flex-wrap gap-1">
      {items.map((s) => {
        const { label, Icon } = SEV[s];
        return (
          <Badge key={s} variant={s} aria-label={`${counts[s]} alerta(s) ${label}`}>
            <Icon aria-hidden />
            {counts[s]} {label.toLowerCase()}
          </Badge>
        );
      })}
    </span>
  );
}
