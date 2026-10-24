import { AppShell } from "@/components/app-shell";
import { VoiceProfileView } from "@/components/consult/voice-profile";

export const metadata = { title: "Minha voz" };

export default function PerfilVozPage() {
  return (
    <AppShell>
      <VoiceProfileView />
    </AppShell>
  );
}
