import { AppShell } from "@/components/app-shell";
import { BedPanel } from "@/components/bed-panel";

export const metadata = { title: "Painel de leitos" };

export default function LeitosPage() {
  return (
    <AppShell>
      <BedPanel />
    </AppShell>
  );
}
