"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { Loader2, Plus, Trash2, UserPlus } from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { api, type PacienteAvulso, type PacienteAvulsoIn } from "@/lib/api";

type Alergia = PacienteAvulsoIn["alergias"][number];
type Medicamento = PacienteAvulsoIn["medicamentos"][number];

/** Cadastro de paciente avulso: cria Patient, alergias e medicamentos em uso no FHIR e abre a consulta. */
export function NovoPaciente() {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [nome, setNome] = useState("");
  const [nascimento, setNascimento] = useState("");
  const [sexo, setSexo] = useState<"M" | "F" | "O">("F");
  const [telefone, setTelefone] = useState("");
  const [alergias, setAlergias] = useState<Alergia[]>([]);
  const [medicamentos, setMedicamentos] = useState<Medicamento[]>([]);
  const [semAlergias, setSemAlergias] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function salvar(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const body: PacienteAvulsoIn = {
        nome,
        nascimento,
        sexo,
        telefone: telefone || undefined,
        alergias: alergias.filter((a) => a.substancia.trim()),
        medicamentos: medicamentos.filter((m) => m.nome.trim()),
        sem_alergias: semAlergias,
      };
      const p = await api<PacienteAvulso>("/patients", { method: "POST", json: body });
      setOpen(false);
      router.push(`/pacientes/${encodeURIComponent(p.id)}/consulta`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Não foi possível cadastrar o paciente");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button>
          <UserPlus aria-hidden /> Novo paciente
        </Button>
      </DialogTrigger>
      <DialogContent className="max-w-xl">
        <DialogTitle className="text-lg font-bold">Novo paciente</DialogTitle>
        <DialogDescription className="text-sm text-muted-foreground">
          Para atender sem prontuário integrado. Alergias e medicamentos em uso informados aqui entram no prontuário do HousIA e são
          conferidos durante a consulta.
        </DialogDescription>
        <form onSubmit={salvar} className="space-y-3">
          <div className="space-y-1.5">
            <Label htmlFor="np-nome">Nome completo</Label>
            <Input id="np-nome" value={nome} onChange={(e) => setNome(e.target.value)} required minLength={3} maxLength={120} autoComplete="off" />
          </div>
          <div className="grid gap-3 sm:grid-cols-3">
            <div className="space-y-1.5">
              <Label htmlFor="np-nasc">Nascimento</Label>
              <Input id="np-nasc" type="date" value={nascimento} onChange={(e) => setNascimento(e.target.value)} required />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="np-sexo">Sexo</Label>
              <NativeSelect id="np-sexo" value={sexo} onChange={(e) => setSexo(e.target.value as typeof sexo)}>
                <option value="F">Feminino</option>
                <option value="M">Masculino</option>
                <option value="O">Outro</option>
              </NativeSelect>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="np-tel">Telefone</Label>
              <Input id="np-tel" value={telefone} onChange={(e) => setTelefone(e.target.value)} maxLength={30} autoComplete="off" />
            </div>
          </div>

          <fieldset className="space-y-2 rounded-lg border p-3">
            <legend className="px-1 text-sm font-bold">Alergias</legend>
            {alergias.map((a, i) => (
              <div key={i} className="flex flex-wrap items-end gap-2">
                <div className="min-w-36 flex-1 space-y-1">
                  <Label htmlFor={`np-alg-${i}`}>Substância</Label>
                  <Input
                    id={`np-alg-${i}`}
                    value={a.substancia}
                    onChange={(e) => setAlergias(alergias.map((x, j) => (j === i ? { ...x, substancia: e.target.value } : x)))}
                    maxLength={80}
                  />
                </div>
                <div className="min-w-32 flex-1 space-y-1">
                  <Label htmlFor={`np-rea-${i}`}>Reação</Label>
                  <Input
                    id={`np-rea-${i}`}
                    value={a.reacao ?? ""}
                    onChange={(e) => setAlergias(alergias.map((x, j) => (j === i ? { ...x, reacao: e.target.value } : x)))}
                    maxLength={120}
                  />
                </div>
                <div className="w-36 space-y-1">
                  <Label htmlFor={`np-crit-${i}`}>Gravidade</Label>
                  <NativeSelect
                    id={`np-crit-${i}`}
                    value={a.criticidade}
                    onChange={(e) => setAlergias(alergias.map((x, j) => (j === i ? { ...x, criticidade: e.target.value as Alergia["criticidade"] } : x)))}
                  >
                    <option value="indeterminada">Não sei</option>
                    <option value="alta">Grave</option>
                    <option value="baixa">Leve</option>
                  </NativeSelect>
                </div>
                <Button type="button" variant="outline" size="icon" onClick={() => setAlergias(alergias.filter((_, j) => j !== i))}>
                  <Trash2 aria-hidden />
                  <span className="sr-only">Remover alergia {i + 1}</span>
                </Button>
              </div>
            ))}
            <div className="flex flex-wrap items-center gap-3">
              <Button type="button" variant="outline" size="sm" onClick={() => setAlergias([...alergias, { substancia: "", criticidade: "indeterminada" }])}>
                <Plus aria-hidden /> Adicionar alergia
              </Button>
              {alergias.length === 0 && (
                <div className="flex items-center gap-2">
                  <Checkbox id="np-nega" checked={semAlergias} onCheckedChange={(v) => setSemAlergias(v === true)} />
                  <Label htmlFor="np-nega">Nega alergias conhecidas</Label>
                </div>
              )}
            </div>
          </fieldset>

          <fieldset className="space-y-2 rounded-lg border p-3">
            <legend className="px-1 text-sm font-bold">Medicamentos em uso</legend>
            {medicamentos.map((m, i) => (
              <div key={i} className="flex flex-wrap items-end gap-2">
                <div className="min-w-36 flex-1 space-y-1">
                  <Label htmlFor={`np-med-${i}`}>Medicamento</Label>
                  <Input
                    id={`np-med-${i}`}
                    value={m.nome}
                    onChange={(e) => setMedicamentos(medicamentos.map((x, j) => (j === i ? { ...x, nome: e.target.value } : x)))}
                    maxLength={80}
                  />
                </div>
                <div className="min-w-32 flex-1 space-y-1">
                  <Label htmlFor={`np-pos-${i}`}>Posologia</Label>
                  <Input
                    id={`np-pos-${i}`}
                    value={m.posologia ?? ""}
                    placeholder="50 mg, 1x ao dia"
                    onChange={(e) => setMedicamentos(medicamentos.map((x, j) => (j === i ? { ...x, posologia: e.target.value } : x)))}
                    maxLength={120}
                  />
                </div>
                <Button type="button" variant="outline" size="icon" onClick={() => setMedicamentos(medicamentos.filter((_, j) => j !== i))}>
                  <Trash2 aria-hidden />
                  <span className="sr-only">Remover medicamento {i + 1}</span>
                </Button>
              </div>
            ))}
            <Button type="button" variant="outline" size="sm" onClick={() => setMedicamentos([...medicamentos, { nome: "" }])}>
              <Plus aria-hidden /> Adicionar medicamento
            </Button>
          </fieldset>

          {error && <Alert variant="critico">{error}</Alert>}
          <Button type="submit" disabled={busy}>
            {busy ? <Loader2 aria-hidden className="animate-spin" /> : <UserPlus aria-hidden />} Cadastrar e abrir a consulta
          </Button>
        </form>
      </DialogContent>
    </Dialog>
  );
}
