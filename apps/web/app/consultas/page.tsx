import { AppShell } from "@/components/app-shell";
import { Agenda } from "@/components/consult/agenda";
import { Onboarding } from "@/components/equipe/onboarding";

export const metadata = { title: "Agenda de consultas" };

export default function ConsultasPage() {
  return (
    <AppShell>
      <Onboarding />
      <Agenda />
    </AppShell>
  );
}
