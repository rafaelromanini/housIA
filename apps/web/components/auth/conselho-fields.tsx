"use client";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";

export const CONSELHOS = ["CRM", "COREN", "CRO", "CRF", "CREFITO", "CRN", "CRP", "CREFONO"];
export const UFS = "AC AL AP AM BA CE DF ES GO MA MT MS MG PA PB PR PE PI RJ RN RS RO RR SC SP SE TO".split(" ");

export interface Conselho {
  conselho: string;
  numero: string;
  uf: string;
}

export function ConselhoFields({
  value,
  onChange,
  obrigatorio,
}: {
  value: Conselho;
  onChange: (v: Conselho) => void;
  obrigatorio: boolean;
}) {
  return (
    <fieldset className="space-y-2">
      <legend className="text-sm font-bold">Registro no conselho profissional{obrigatorio ? "" : " (opcional)"}</legend>
      <div className="grid grid-cols-[1fr_1.4fr_0.8fr] gap-2">
        <div className="space-y-1.5">
          <Label htmlFor="conselho">Conselho</Label>
          <NativeSelect
            id="conselho"
            value={value.conselho}
            required={obrigatorio}
            onChange={(e) => onChange({ ...value, conselho: e.target.value })}
          >
            <option value="">—</option>
            {CONSELHOS.map((c) => (
              <option key={c}>{c}</option>
            ))}
          </NativeSelect>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="conselho-numero">Número</Label>
          <Input
            id="conselho-numero"
            inputMode="numeric"
            pattern="[0-9]*"
            value={value.numero}
            required={obrigatorio}
            onChange={(e) => onChange({ ...value, numero: e.target.value.replace(/\D/g, "") })}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="conselho-uf">UF</Label>
          <NativeSelect
            id="conselho-uf"
            value={value.uf}
            required={obrigatorio}
            onChange={(e) => onChange({ ...value, uf: e.target.value })}
          >
            <option value="">—</option>
            {UFS.map((u) => (
              <option key={u}>{u}</option>
            ))}
          </NativeSelect>
        </div>
      </div>
      <p className="text-xs text-muted-foreground">
        O registro é conferido manualmente pela administração; só depois disso a nota exportada leva o selo “verificado”.
      </p>
    </fieldset>
  );
}
