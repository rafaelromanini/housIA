import { PatientShell } from "@/components/patient/patient-shell";

export default async function PatientLayout({
  children,
  params,
}: {
  children: React.ReactNode;
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return <PatientShell patientId={decodeURIComponent(id)}>{children}</PatientShell>;
}
