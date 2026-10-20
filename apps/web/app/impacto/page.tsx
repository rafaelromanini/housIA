import { AppShell } from "@/components/app-shell";
import { ImpactView } from "@/components/impact/impact-view";

export const metadata = { title: "Impacto" };

export default function ImpactoPage() {
  return (
    <AppShell>
      <ImpactView />
    </AppShell>
  );
}
