import { AppShell } from "@/components/app-shell";
import { Agenda } from "@/components/consult/agenda";

export const metadata = { title: "Agenda de consultas" };

export default function ConsultasPage() {
  return (
    <AppShell>
      <Agenda />
    </AppShell>
  );
}
