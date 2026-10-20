import { AppShell } from "@/components/app-shell";
import { AuditView } from "@/components/audit/audit-view";

export const metadata = { title: "Auditoria" };

export default function AuditoriaPage() {
  return (
    <AppShell>
      <AuditView />
    </AppShell>
  );
}
