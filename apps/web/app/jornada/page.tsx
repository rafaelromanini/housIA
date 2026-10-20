import { AppShell } from "@/components/app-shell";
import { JourneyView } from "@/components/journey/journey-view";

export const metadata = { title: "Jornada do paciente" };

export default function JornadaPage() {
  return (
    <AppShell>
      <JourneyView />
    </AppShell>
  );
}
