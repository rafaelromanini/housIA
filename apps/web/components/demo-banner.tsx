import { FlaskConical, ShieldAlert } from "lucide-react";

export function DemoBanner() {
  return (
    <div
      role="note"
      className="bg-[#1f2937] text-white text-xs sm:text-sm font-bold tracking-wide px-4 py-1.5 flex items-center justify-center gap-2 text-center"
    >
      <FlaskConical aria-hidden className="size-4 shrink-0" />
      AMBIENTE DE DEMONSTRAÇÃO — DADOS SINTÉTICOS
    </div>
  );
}

export function Disclaimer() {
  return (
    <footer className="border-t bg-card text-muted-foreground text-xs sm:text-sm px-4 py-3 flex items-center justify-center gap-2 text-center">
      <ShieldAlert aria-hidden className="size-4 shrink-0" />
      Ferramenta de apoio à decisão. Não substitui avaliação profissional.
    </footer>
  );
}
