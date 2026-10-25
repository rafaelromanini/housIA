import { AppShell } from "@/components/app-shell";
import { ConformidadeView } from "@/components/audit/conformidade-view";

export const metadata = { title: "Conformidade" };

export default function ConformidadePage() {
  return (
    <AppShell>
      <ConformidadeView />
    </AppShell>
  );
}
