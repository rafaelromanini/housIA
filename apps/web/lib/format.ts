const TZ = "America/Sao_Paulo";

const dateTime = new Intl.DateTimeFormat("pt-BR", {
  timeZone: TZ,
  day: "2-digit",
  month: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
});
const timeOnly = new Intl.DateTimeFormat("pt-BR", { timeZone: TZ, hour: "2-digit", minute: "2-digit" });
const dayLabel = new Intl.DateTimeFormat("pt-BR", { timeZone: TZ, weekday: "long", day: "2-digit", month: "long" });
const dayKey = new Intl.DateTimeFormat("en-CA", { timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit" });

export const fmtDateTime = (iso: string) => dateTime.format(new Date(iso));
export const fmtTime = (iso: string) => timeOnly.format(new Date(iso));
export const fmtDay = (iso: string) => dayLabel.format(new Date(iso));
export const dayOf = (iso: string) => dayKey.format(new Date(iso));

export function fmtRelative(iso: string, now = Date.now()): string {
  const diffMin = Math.round((now - new Date(iso).getTime()) / 60000);
  if (diffMin < 1) return "agora";
  if (diffMin < 60) return `há ${diffMin} min`;
  const h = Math.floor(diffMin / 60);
  if (h < 24) return `há ${h}h`;
  const d = Math.floor(h / 24);
  return `há ${d} dia${d > 1 ? "s" : ""}`;
}

export const ROLE_LABEL: Record<string, string> = {
  medico: "Médico(a)",
  enfermagem: "Enfermagem",
  gestor: "Gestão",
};
