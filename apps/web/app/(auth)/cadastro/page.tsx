import { RegisterForm } from "@/components/auth/register-form";

export const metadata = { title: "Criar conta" };

export default async function CadastroPage({ searchParams }: { searchParams: Promise<{ tipo?: string }> }) {
  const { tipo } = await searchParams;
  return <RegisterForm tipoInicial={tipo === "hospital" ? "hospital" : "consultorio"} />;
}
