import { PatientOverview } from "@/components/patient/overview";

export const metadata = { title: "Paciente" };

export default async function PatientPage({ searchParams }: { searchParams: Promise<{ ref?: string }> }) {
  const { ref } = await searchParams;
  return <PatientOverview highlightRef={ref ?? null} />;
}
