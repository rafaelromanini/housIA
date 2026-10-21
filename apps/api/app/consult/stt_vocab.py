"""Transcrição que conhece o paciente (#38): vocabulário no prompt, correção por similaridade e confiança por trecho.

1. `prompt(...)`: medicamentos, diagnósticos e alergias do prontuário do paciente, o vocabulário institucional
   (`data/reference/vocab_institucional.csv`, editável pelo gestor) e o glossário clínico viram o `prompt` do STT.
2. `corrigir(...)`: palavra (ou par de palavras) que o motor escreveu quase igual a um medicamento ou marca
   ("glibem clamida") é trocada pelo nome certo, com a marca "corrigido" (o que foi ouvido fica guardado). Similaridade
   por `difflib` (sem dependência nova), com limiar alto, mesma inicial e tamanho mínimo.
3. `avaliar(...)`: confiança do trecho (a partir de `avg_logprob` ou da probabilidade por palavra) e as palavras de
   baixa confiança; as que são medicamento, dose ou alergia viram "confirmar o que foi ouvido" (o alerta crítico
   desce para atenção até o profissional confirmar; ver `engine.med_safety`).
"""

from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path

from app.config import get_settings
from app.consult import glossario
from app.consult.text import brands, norm
from app.reference import drugs

MAX_PROMPT_CHARS = 900  # o Whisper usa só os últimos ~224 tokens do prompt
MAX_TERMOS_VOCAB = 500
RAZAO_MINIMA = 0.88  # similaridade para corrigir um termo do catálogo
RAZAO_MINIMA_PACIENTE = 0.82  # o que o paciente já toma é mais provável: limiar menor
TAMANHO_MINIMO = 6
DOSE = re.compile(r"^\d+([.,]\d+)?(mg|mcg|g|ml|ui|u|%)?$")
UNIDADES = {"mg", "mcg", "g", "ml", "ui", "comprimido", "comprimidos", "gota", "gotas", "unidade", "unidades"}
CONTEXTO_ALERGIA = {"alergia", "alergica", "alergico", "alergias", "alergia."}
TOKEN = re.compile(r"[\wÀ-ÿ]+(?:[-'][\wÀ-ÿ]+)*|[^\w\s]", re.UNICODE)


# ---------- vocabulário institucional ----------
def caminho_base() -> Path:
    s = get_settings()
    return s.vocab_institucional_path or s.data_dir / "reference" / "vocab_institucional.csv"


def caminho_vocab(org_id: int | None = None) -> Path:
    """Vocabulário da organização (arquivo próprio ao lado do base) quando ela já editou; senão o base de demonstração.
    Cada organização só lê e grava o seu: o gestor de uma não altera o prompt do STT de outra."""
    base = caminho_base()
    if org_id is None:
        return base
    return base.with_name(f"{base.stem}.org{int(org_id)}{base.suffix}")


def _leitura(org_id: int | None) -> Path:
    p = caminho_vocab(org_id)
    return p if p.exists() else caminho_base()


@lru_cache
def _ler(caminho: str, mtime: float) -> tuple[tuple[str, str, str], ...]:
    p = Path(caminho)
    if not p.exists():
        return ()
    with p.open(encoding="utf-8", newline="") as fh:
        rows = [
            (r["termo"].strip(), (r.get("tipo") or "").strip(), (r.get("observacao") or "").strip())
            for r in csv.DictReader(fh)
            if (r.get("termo") or "").strip()
        ]
    return tuple(rows)


def vocab_institucional(org_id: int | None = None) -> list[dict]:
    p = _leitura(org_id)
    mtime = p.stat().st_mtime if p.exists() else 0.0
    return [{"termo": t, "tipo": tipo, "observacao": obs} for t, tipo, obs in _ler(str(p), mtime)]


MAX_TERMO = 60  # um termo é nome, sigla ou serviço, não frase: o texto cai no prompt do STT
_CONTROLE = re.compile(r"[\x00-\x1f\x7f]")


def _limpar(valor: object, limite: int) -> str:
    t = re.sub(r"\s+", " ", _CONTROLE.sub(" ", str(valor or ""))).strip()
    return t.lstrip("=+@").strip()[:limite]  # nada que vire fórmula se alguém abrir o CSV numa planilha


def salvar_vocab(itens: list[dict], org_id: int | None = None) -> int:
    """Troca o vocabulário institucional inteiro (o gestor edita a lista toda). Devolve quantos termos ficaram."""
    vistos: dict[str, dict] = {}
    for it in itens:
        termo = _limpar(it.get("termo", ""), MAX_TERMO)
        if termo and norm(termo) not in vistos:
            vistos[norm(termo)] = {
                "termo": termo,
                "tipo": _limpar(it.get("tipo", ""), 30),
                "observacao": _limpar(it.get("observacao", ""), 120),
            }
    if len(vistos) > MAX_TERMOS_VOCAB:
        raise ValueError(f"Vocabulário institucional limitado a {MAX_TERMOS_VOCAB} termos.")
    p = caminho_vocab(org_id) if org_id is not None else caminho_base()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, ["termo", "tipo", "observacao"])
        w.writeheader()
        w.writerows(vistos.values())
    return len(vistos)


# ---------- prompt ----------
def termos_paciente(record) -> list[str]:
    """Medicamentos ativos, diagnósticos e alergias do prontuário (nomes como o profissional diria)."""
    out: list[str] = []
    for m in record.active_meds:
        out.append(m.name)
    for c in record.active_conditions:
        out.append(str((c.get("code") or {}).get("text") or "").strip())
    for a in record.real_allergies:
        out.append(str((a.get("code") or {}).get("text") or "").strip())
    return [t for t in dict.fromkeys(out) if t]


def prompt(record=None, specialty: str = "", org_id: int | None = None) -> str:
    """Prompt do STT: paciente primeiro (o que mais importa), depois a instituição e o glossário, sem repetir."""
    termos: list[str] = []
    if record is not None:
        termos += termos_paciente(record)
    termos += [v["termo"] for v in vocab_institucional(org_id)]
    base = glossario.prompt_stt(specialty)
    vistos: set[str] = set()
    partes: list[str] = []
    tamanho = 0
    for t in termos:
        k = norm(t)
        if k in vistos or tamanho + len(t) + 2 > MAX_PROMPT_CHARS:
            continue
        vistos.add(k)
        partes.append(t)
        tamanho += len(t) + 2
    cabeca = ", ".join(partes)
    if not base:
        return cabeca
    resto = max(0, MAX_PROMPT_CHARS - len(cabeca) - 2)
    return f"{cabeca}. {base[:resto]}".strip(". ") if cabeca and resto > 20 else (cabeca or base[:MAX_PROMPT_CHARS])


# ---------- correção por similaridade ----------
@lru_cache
def _catalogo() -> dict[str, str]:
    """Forma normalizada (sem acento, minúscula) → nome canônico, de drugs.csv e drug_brands.csv."""
    out = {norm(d.name): d.name for d in drugs().values()}
    out |= {norm(d.ingredient): d.name for d in drugs().values()}
    for marca, ing in brands().items():
        out.setdefault(marca, marca.capitalize() if ing else marca)
    return out


def _alvos(extra: list[str]) -> dict[str, str]:
    out = dict(_catalogo())
    for t in extra:
        for palavra in [t, *t.split()]:
            if len(palavra) >= TAMANHO_MINIMO:
                out.setdefault(norm(palavra), palavra)
    return out


@dataclass
class Correcao:
    de: str
    para: str
    razao: float

    def dump(self) -> dict:
        return {"de": self.de, "para": self.para, "razao": round(self.razao, 2)}


def _distancia(a: str, b: str) -> int:
    """Distância de edição (Levenshtein)."""
    ant = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        atual = [i]
        for j, y in enumerate(b, 1):
            atual.append(min(ant[j] + 1, atual[j - 1] + 1, ant[j - 1] + (x != y)))
        ant = atual
    return ant[-1]


def _razao(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _melhor(chave: str, alvos: dict[str, str], minimo: float, preferidos: set[str]) -> tuple[str, float] | None:
    if len(chave) < TAMANHO_MINIMO or chave in alvos or any(c.isdigit() for c in chave):
        return None  # dose e número nunca são "corrigidos"
    candidatos: list[tuple[str, float]] = []
    for k in alvos:
        if k[0] != chave[0] or abs(len(k) - len(chave)) > 1:
            continue
        r = _razao(chave, k)
        limiar = RAZAO_MINIMA_PACIENTE if k in preferidos else minimo
        # uma letra de diferença no máximo: "cefaleia"→"cefalexina", "prednisolona"→"prednisona" (outros remédios
        # ou palavras comuns) ficam como foram ouvidos; quem confere é o profissional
        if r >= limiar and _distancia(chave, k) <= 1:
            candidatos.append((k, r))
    if not candidatos:
        return None
    candidatos.sort(key=lambda c: -c[1])
    alvos_distintos = {alvos[k] for k, _ in candidatos}
    if len(alvos_distintos) > 1:
        return None  # ambíguo entre dois medicamentos: não escolhe pelo profissional
    return candidatos[0]


def corrigir(texto: str, termos_extra: list[str] | None = None) -> tuple[str, list[Correcao]]:
    """Troca por similaridade o que parece medicamento ou marca. `termos_extra`: vocabulário do paciente/instituição.
    Pares de palavras são tentados primeiro ("glibem clamida" → "glibenclamida")."""
    extra = termos_extra or []
    alvos = _alvos(extra)
    preferidos = {norm(w) for t in extra for w in [t, *t.split()] if len(w) >= TAMANHO_MINIMO}
    toks = [(m.group(), m.start(), m.end()) for m in TOKEN.finditer(texto)]
    palavras = [i for i, (t, _, _) in enumerate(toks) if t[0].isalnum()]
    trocas: list[tuple[int, int, str, Correcao]] = []  # (início, fim, novo, correção)
    usados: set[int] = set()
    for pos, i in enumerate(palavras):
        if i in usados:
            continue
        if norm(toks[i][0]) in alvos:
            continue  # já é um nome conhecido: nunca junta com a palavra seguinte
        cand: list[tuple[list[int], str]] = []
        if pos + 1 < len(palavras):
            j = palavras[pos + 1]
            if (
                j not in usados
                and len(toks[j][0]) >= 3
                and norm(toks[j][0]) not in alvos
                and texto[toks[i][2] : toks[j][1]].strip() == ""
            ):
                cand.append(([i, j], toks[i][0] + toks[j][0]))
        cand.append(([i], toks[i][0]))
        for idxs, bruto in cand:
            chave = norm(bruto)
            hit = _melhor(chave, alvos, RAZAO_MINIMA, preferidos)
            if hit:
                ini, fim = toks[idxs[0]][1], toks[idxs[-1]][2]
                original = texto[ini:fim]
                novo = alvos[hit[0]]
                if original[:1].islower():
                    novo = novo[:1].lower() + novo[1:]
                trocas.append((ini, fim, novo, Correcao(original, novo, hit[1])))
                usados.update(idxs)
                break
    for ini, fim, novo, _ in sorted(trocas, reverse=True):
        texto = texto[:ini] + novo + texto[fim:]
    return texto, [c for _, _, _, c in sorted(trocas)]


# ---------- confiança ----------
def confianca_de_logprob(avg_logprob: float | None) -> float | None:
    return None if avg_logprob is None else round(max(0.0, min(1.0, math.exp(avg_logprob))), 3)


@dataclass
class Avaliacao:
    confianca: float | None = None
    palavras_baixas: list[str] = field(default_factory=list)
    confirmar: list[str] = field(default_factory=list)


def _critica(palavra: str, vizinhas_alergia: bool, alvos: dict[str, str]) -> bool:
    k = norm(palavra)
    return bool(DOSE.match(k) or k in UNIDADES or k in alvos or vizinhas_alergia)


def avaliar(
    texto: str,
    palavras: list[tuple[str, float | None]],
    avg_logprob: float | None = None,
    termos_extra: list[str] | None = None,
) -> Avaliacao:
    """`palavras`: (texto, probabilidade) por palavra do motor. Baixa confiança: probabilidade < limiar; sem
    probabilidades por palavra mas com trecho de baixa confiança, vale para as palavras críticas do trecho."""
    s = get_settings()
    limiar = s.stt_confianca_minima
    probs = [p for _, p in palavras if p is not None]
    conf = confianca_de_logprob(avg_logprob)
    if conf is None and probs:
        conf = round(sum(probs) / len(probs), 3)
    alvos = _alvos(termos_extra or [])
    baixas: list[str] = []
    if probs:
        baixas = [w for w, p in palavras if p is not None and p < limiar]
    if not baixas and conf is not None and conf < limiar:  # trecho incerto sem palavra individualmente incerta
        baixas = [m.group() for m in TOKEN.finditer(texto) if m.group()[0].isalnum()]
    toks = [m.group() for m in TOKEN.finditer(texto) if m.group()[0].isalnum()]
    normais = [norm(t) for t in toks]
    confirmar: list[str] = []
    for b in dict.fromkeys(baixas):
        if _critica(b, False, alvos) or _perto_de_alergia(norm(b), normais):
            confirmar.append(b)
    return Avaliacao(conf, list(dict.fromkeys(baixas)), confirmar)


def _perto_de_alergia(palavra: str, normais: list[str]) -> bool:
    """Palavra dentro de 3 palavras depois de "alergia/alérgico" (o alérgeno dito)."""
    for i, t in enumerate(normais):
        if t in CONTEXTO_ALERGIA or t.startswith("alerg"):
            if palavra in normais[i + 1 : i + 4]:
                return True
    return False
