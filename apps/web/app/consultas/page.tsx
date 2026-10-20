import { AppShell } from "@/components/app-shell";
import { ConsultasView } from "@/components/consult/consultas-view";
import { Onboarding } from "@/components/equipe/onboarding";

export const metadata = { title: "Consultas" };

export default function ConsultasPage() {
  return (
    <AppShell>
      <Onboarding />
      <ConsultasView />
    </AppShell>
  );
}
