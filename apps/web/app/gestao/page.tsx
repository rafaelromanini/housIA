import { AppShell } from "@/components/app-shell";
import { GestaoView } from "@/components/gestao/gestao-view";

export const metadata = { title: "Gestão" };

export default function GestaoPage() {
  return (
    <AppShell>
      <GestaoView />
    </AppShell>
  );
}
