"use client";

import { useEffect, useState } from "react";
import { Star, Trash2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { api, type CidOption, type CidSuggestion } from "@/lib/api";

/** CID-10 da revisão: adicionar e trocar com autocomplete da tabela oficial (cid10.csv), marcar o principal e remover. */
export function CidEditor({
  cids,
  onChange,
  disabled,
  reid = (t) => t,
}: {
  cids: CidSuggestion[];
  onChange: (cids: CidSuggestion[]) => void;
  disabled: boolean;
  reid?: (t: string) => string;
}) {
  const [query, setQuery] = useState("");
  const [options, setOptions] = useState<CidOption[]>([]);
  const [replace, setReplace] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const q = query.trim();
    if (q.length < 2) {
      setOptions([]);
      return;
    }
    let cancelled = false;
    const timer = window.setTimeout(async () => {
      try {
        const found = await api<CidOption[]>(`/consult/cid10?q=${encodeURIComponent(q)}`);
        if (!cancelled) {
          setOptions(found);
          setError(null);
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : "Não foi possível buscar o CID");
      }
    }, 250);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [query]);

  function pick(option: CidOption) {
    const target = cids.find((c) => c.codigo === replace);
    if (target) {
      // trocar mantém o tipo (e o "principal") do CID substituído; a fonte passa a ser a escolha do profissional
      onChange(cids.map((c) => (c.codigo === replace ? { codigo: option.codigo, descricao: option.descricao, tipo: c.tipo, refs: [] } : c)));
    } else if (!cids.some((c) => c.codigo === option.codigo)) {
      onChange([...cids, { codigo: option.codigo, descricao: option.descricao, tipo: "secundario", refs: [] }]);
    }
    setQuery("");
    setOptions([]);
    setReplace("");
  }

  function setMain(codigo: string) {
    onChange(
      cids.map((c) => {
        if (c.codigo === codigo) return { ...c, tipo: "principal" };
        return c.tipo === "principal" ? { ...c, tipo: "secundario" } : c;
      }),
    );
  }

  return (
    <div className="space-y-2">
      <p className="text-sm font-bold">CID-10</p>
      {cids.length === 0 && <p className="text-sm text-muted-foreground">Nenhum CID na nota.</p>}
      <ul className="flex flex-wrap gap-1.5" aria-label="CID-10 da nota">
        {cids.map((cid, i) => (
          <li key={cid.codigo + i}>
            <Badge variant={cid.tipo === "hipotese" ? "ai" : cid.tipo === "principal" ? "ok" : "outline"} className="gap-1.5">
              <span className="font-mono">{cid.codigo}</span> {reid(cid.descricao)}
              <span className="text-[10px] uppercase">{cid.tipo}</span>
              {!disabled && (
                <>
                  <button
                    type="button"
                    aria-pressed={cid.tipo === "principal"}
                    aria-label={`Marcar CID ${cid.codigo} como principal`}
                    className="ml-0.5 rounded p-1 hover:bg-muted"
                    onClick={() => setMain(cid.codigo)}
                  >
                    <Star aria-hidden className={cid.tipo === "principal" ? "size-3.5 fill-current" : "size-3.5"} />
                  </button>
                  <button
                    type="button"
                    aria-label={`Remover CID ${cid.codigo}`}
                    className="rounded p-1 hover:bg-muted"
                    onClick={() => onChange(cids.filter((_, j) => j !== i))}
                  >
                    <Trash2 aria-hidden className="size-3.5" />
                  </button>
                </>
              )}
            </Badge>
          </li>
        ))}
      </ul>
      {cids.some((c) => c.frase || c.ciap2 || c.lacunas?.length || c.mais_especificos?.length) && (
        <ul className="space-y-1.5 text-xs" aria-label="Justificativa e especificidade dos CID">
          {cids.map((cid, i) => (
            <li key={cid.codigo + i} className="rounded-md border bg-muted/30 p-2">
              <p>
                <span className="font-mono font-bold">{cid.codigo}</span>
                {cid.ciap2 && <span className="ml-2 text-muted-foreground">CIAP-2: {cid.ciap2}</span>}
              </p>
              {cid.frase && <p className="break-words italic">“{reid(cid.frase)}”</p>}
              {cid.mais_especificos?.map((m) => (
                <p key={m.codigo} className="break-words">
                  Mais específico: <span className="font-mono font-bold">{m.codigo}</span> {m.descricao}
                  {m.frase && <span className="italic"> “{reid(m.frase)}”</span>}
                </p>
              ))}
              {cid.lacunas?.map((l) => (
                <p key={l} className="break-words text-atencao">
                  Falta: {l}
                </p>
              ))}
            </li>
          ))}
        </ul>
      )}
      {!disabled && (
        <div className="grid gap-2 sm:grid-cols-[1fr_14rem]">
          <div className="space-y-1">
            <Label htmlFor="cid-busca" className="text-xs">
              Buscar CID-10 (código ou nome)
            </Label>
            <Input
              id="cid-busca"
              role="combobox"
              aria-expanded={options.length > 0}
              aria-controls="cid-resultados"
              aria-autocomplete="list"
              autoComplete="off"
              value={query}
              placeholder="Ex.: E16 ou hipoglicemia"
              onChange={(e) => setQuery(e.target.value)}
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor="cid-trocar" className="text-xs">
              Ao escolher
            </Label>
            <NativeSelect id="cid-trocar" value={replace} onChange={(e) => setReplace(e.target.value)}>
              <option value="">Adicionar à nota</option>
              {cids.map((c) => (
                <option key={c.codigo} value={c.codigo}>
                  Trocar {c.codigo}
                </option>
              ))}
            </NativeSelect>
          </div>
        </div>
      )}
      {error && <p className="text-sm text-critico">{error}</p>}
      {options.length > 0 && !disabled && (
        <ul id="cid-resultados" role="listbox" aria-label="Resultados da CID-10" className="max-h-56 divide-y overflow-auto rounded-md border bg-card">
          {options.map((o) => (
            <li key={o.codigo} role="option" aria-selected={false}>
              <Button
                type="button"
                variant="ghost"
                className="h-auto min-h-11 w-full justify-start gap-2 whitespace-normal text-left font-normal"
                aria-label={`${replace ? `Trocar ${replace} por` : "Adicionar"} CID ${o.codigo}: ${o.descricao}`}
                onClick={() => pick(o)}
              >
                <span className="font-mono font-bold">{o.codigo}</span> {o.descricao}
              </Button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
