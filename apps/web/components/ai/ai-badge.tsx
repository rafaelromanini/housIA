import { Bot } from "lucide-react";

import { Badge } from "@/components/ui/badge";

export function AiBadge({ label = "Gerado por IA — confira" }: { label?: string }) {
  return (
    <Badge variant="ai">
      <Bot aria-hidden />
      {label}
    </Badge>
  );
}

/** Mostra o nome do paciente no lugar do token de pseudonimização usado com o LLM. */
export function reidentify(text: string, name: string) {
  return text.replaceAll("[PACIENTE]", name);
}
