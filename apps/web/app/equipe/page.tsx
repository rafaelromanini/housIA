import { AppShell } from "@/components/app-shell";
import { EquipeView } from "@/components/equipe/equipe-view";

export const metadata = { title: "Equipe" };

export default function EquipePage() {
  return (
    <AppShell>
      <EquipeView />
    </AppShell>
  );
}
