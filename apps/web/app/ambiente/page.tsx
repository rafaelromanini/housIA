import { AppShell } from "@/components/app-shell";
import { AmbienteView } from "@/components/ambiente/ambiente-view";

export const metadata = { title: "Ambiente" };

export default function AmbientePage() {
  return (
    <AppShell>
      <AmbienteView />
    </AppShell>
  );
}
