"""Edição da nota por instrução (#46): reescreve UMA seção, com diff, refs preservadas e travas de segurança.

O modelo (ou o mock, em `LLM_MODE=mock`) devolve a seção como frases com refs. O servidor decide o que entra:
- a frase igual a uma existente volta como estava (refs e origem);
- frase com refs válidas é "ia"; sem refs, herda as da frase parecida; sem parecida só entra se for o que a instrução
  pediu para escrever (origem "profissional"); fora disso é descartada como "sem fonte";
- número ou medicamento que não estava na seção nem na instrução derruba a frase (fato novo sem fonte);
- **itens de segurança** (alerta de medicamento crítico ou de atenção, alergia) precisam continuar literalmente na
  seção; se a reescrita os tira, ela é bloqueada, sem exceção. A seção de pendências não é reescrevível.
Nada é gravado sem o profissional pedir (`aplicar`), e aplicar tira a atestação: a nota volta à revisão.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

from app.consult import fidelidade, notas
from app.consult.edicao import SECTION_LABELS, _word_diff
from app.consult.text import norm

REWRITABLE = notas.SECTION_KEYS  # as nove seções clássicas; as do modelo (`secoes`) entram pela key do modelo
MAX_INSTRUCAO = 500
_STOP = {
    "para",
    "com",
    "sem",
    "mais",
    "menos",
    "esta",
    "esse",
    "essa",
    "seção",
    "secao",
    "nota",
    "frase",
    "texto",
    "deixe",
    "deixar",
    "faça",
    "faca",
    "pode",
    "sobre",
    "dose",
    "doses",
    "favor",
}
_VERBO_REMOVER = re.compile(r"\b(remov\w*|retir\w*|tire|apag\w*|exclu\w*|delet\w*|elimin\w*|omit\w*)\b")
_VERBO_RESUMIR = re.compile(r"\b(resum\w*|encurt\w*|sucint\w*|conciso\w*|enxug\w*|curt[oa]s?|reduz\w*)\b")
_VERBO_ADD = re.compile(r"\b(acrescent\w*|adicion\w*|inclu\w*|escrev\w*|insira|inserir)\b")
_GENERICAS = {"alerta", "alertas", "seguranca", "interacao", "interacoes", "alergia", "alergias"}
SIGLAS = {
    "hipertensão arterial sistêmica": "HAS",
    "diabetes mellitus tipo 2": "DM2",
    "insuficiência cardíaca": "IC",
    "infarto agudo do miocárdio": "IAM",
    "doença renal crônica": "DRC",
    "acidente vascular cerebral": "AVC",
}


class Frase(BaseModel):
    texto: str
    refs: list[str] = []


class Saida(BaseModel):
    frases: list[Frase]


class Bloqueio(Exception):
    def __init__(self, itens: list[dict]):
        super().__init__("reescrita bloqueada")
        self.itens = itens


# ---------- a seção ----------
def secao(note: dict, key: str) -> dict | None:
    """A seção reescrevível `key` (clássica ou do modelo) como dict {texto, refs, frases?}; None se não existe."""
    if key in REWRITABLE:
        sec = note.get(key)
        return dict(sec) if isinstance(sec, dict) else None
    for extra in note.get("secoes") or []:
        if isinstance(extra, dict) and extra.get("key") == key:
            return dict(extra)
    return None


def rotulo(note: dict, key: str) -> str:
    if key in SECTION_LABELS:
        return SECTION_LABELS[key]
    for s in (note.get("modelo") or {}).get("secoes", []):
        if s.get("key") == key:
            return s.get("rotulo", key)
    return key


def frases_de(sec: dict) -> list[dict]:
    """Frases com proveniência; seção sem elas (nota antiga, do modelo) é dividida por sentença com as refs dela."""
    texto = sec.get("texto", "")
    if notas.is_placeholder(texto):
        return []
    if sec.get("frases"):
        return [dict(f) for f in sec["frases"]]
    refs = sec.get("refs", [])
    return [notas.frase(p, refs, "regra") for p in notas.split_sentences(texto)]


def protegidas(key: str, frases: list[dict], alertas: list[str], alergenos: list[str]) -> list[dict]:
    """Frases que a reescrita não pode tirar nem mudar: a seção de alergias inteira, as que trazem o texto de um alerta
    de medicamento (crítico ou de atenção) e as que citam um alérgeno relatado ou registrado."""
    out = []
    alertas_n = [norm(a) for a in alertas if a.strip()]
    alergenos_n = [norm(a) for a in alergenos if len(a.strip()) >= 3]
    for f in frases:
        texto = norm(f["texto"])
        if key == "alergias" or any(a in texto for a in alertas_n) or any(a in texto for a in alergenos_n):
            out.append(f)
    return out


# ---------- o mock (regras) ----------
def _palavras_chave(instrucao: str) -> set[str]:
    return {w for w in re.findall(r"\w{5,}", norm(instrucao)) if w not in _STOP}


def _primeira_oracao(texto: str) -> str:
    texto = re.sub(r"\s*\((aceito|aceita) pelo profissional\)", "", texto)
    palavras = texto.split()
    if len(palavras) <= 6:
        return texto
    for sep in (",", ";", " — ", " (", " que ", " pois ", " porém "):
        i = texto.find(sep)
        if i > 0 and len(texto[:i].split()) >= 4:
            return texto[:i].rstrip(" ,;—") + ("." if not texto[:i].rstrip().endswith(".") else "")
    return texto


def mock(v: dict[str, Any]) -> dict:
    """O que um reescritor obediente faria, por regras: resumir, tirar o que a instrução cita, acrescentar o texto entre
    aspas ou depois de dois-pontos, e abreviar ou expandir siglas. Instrução não reconhecida deixa a seção como está."""
    frases = [dict(f) for f in v["frases"]]
    ins = v["instrucao"]
    n = norm(ins)
    estilo = " ".join(v.get("estilo") or [])
    seu_estilo = "meu estilo" in n or "meu padrao" in n
    protegidas_ = {notas._key(t) for t in v.get("protegidas", [])}
    if _VERBO_REMOVER.search(n):
        alvo = _palavras_chave(_VERBO_REMOVER.sub(" ", n))
        if alvo & _GENERICAS:  # "tire o alerta": o reescritor obediente tira os itens de segurança da seção
            frases = [f for f in frases if notas._key(f["texto"]) not in protegidas_]
        alvo -= _GENERICAS
        if alvo:
            frases = [f for f in frases if not alvo & set(re.findall(r"\w{5,}", norm(f["texto"])))]
    if _VERBO_RESUMIR.search(n) or (seu_estilo and "mais curtas" in estilo):
        antes = sum(len(f["texto"].split()) for f in frases)
        encurtadas = [
            f if notas._key(f["texto"]) in protegidas_ else f | {"texto": _primeira_oracao(f["texto"])} for f in frases
        ]
        while (
            len(encurtadas) > 1
            and sum(len(f["texto"].split()) for f in encurtadas) >= antes
            and any(notas._key(f["texto"]) not in protegidas_ for f in encurtadas)
        ):
            ultimo = max(i for i, f in enumerate(encurtadas) if notas._key(f["texto"]) not in protegidas_)
            encurtadas.pop(ultimo)
        frases = encurtadas
    if re.search(r"\b(sem sigla\w*|por extenso|extenso)\b", n) or (seu_estilo and "por extenso" in estilo):
        for longo, sigla in SIGLAS.items():
            frases = [f | {"texto": re.sub(rf"\b{sigla}\b", longo, f["texto"])} for f in frases]
    elif re.search(r"\b(sigla\w*|abrevi\w*)\b", n) or (seu_estilo and "siglas médicas" in estilo):
        for longo, sigla in SIGLAS.items():
            frases = [f | {"texto": re.sub(longo, sigla, f["texto"], flags=re.IGNORECASE)} for f in frases]
    if _VERBO_ADD.search(n):
        m = re.search(r"[“\"]([^”\"]{3,300})[”\"]", ins) or re.search(r":\s*(.{3,300})$", ins)
        if m:
            frases.append({"texto": m.group(1).strip(), "refs": []})
    return {"frases": [{"texto": f["texto"], "refs": list(f.get("refs", []))} for f in frases]}


# ---------- validação e montagem ----------
def _numeros(texto: str) -> set[str]:
    return {v for v, _ in fidelidade.numeros(texto)}


def montar(antigas: list[dict], novas: list[Frase], instrucao: str, valid: set[str]) -> tuple[list[dict], list[dict]]:
    """(frases da seção nova, descartadas). Ver a regra no cabeçalho do módulo."""
    contexto = " ".join(f["texto"] for f in antigas) + " " + instrucao
    nums_ok, meds_ok = _numeros(contexto), fidelidade.medicamentos(contexto)
    chave_instrucao = _palavras_chave(instrucao)
    out: list[dict] = []
    descartadas: list[dict] = []
    for nf in novas:
        texto = nf.texto.strip()
        if not texto:
            continue
        igual = next((o for o in antigas if notas._key(o["texto"]) == notas._key(texto)), None)
        if igual is not None:
            out.append(dict(igual))
            continue
        if _numeros(texto) - nums_ok or fidelidade.medicamentos(texto) - meds_ok:
            descartadas.append(
                {"texto": texto, "motivo": "número ou medicamento que não estava na seção nem na instrução"}
            )
            continue
        refs = [r for r in dict.fromkeys(nf.refs) if r in valid]
        literal = norm(texto).strip(" .") in norm(instrucao)  # o profissional mandou escrever exatamente isto
        parecida = notas._best_match(texto, antigas, threshold=0.5)
        palavras = set(re.findall(r"\w{5,}", norm(texto)))
        pedido = bool(palavras) and len(palavras & chave_instrucao) / len(palavras) >= 0.5
        if refs:
            out.append(notas.frase(texto, refs, "ia"))
        elif literal:
            out.append(notas.frase(texto, [], "profissional"))
        elif parecida is not None:
            out.append(notas.frase(texto, parecida["refs"], "ia"))
        elif pedido:
            out.append(notas.frase(texto, [], "profissional"))  # o que a instrução mandou escrever
        else:
            descartadas.append({"texto": texto, "motivo": "sem fonte na conversa nem na instrução"})
    return out, descartadas


def bloqueios(antigas: list[dict], novas: list[dict], protegidas_: list[dict]) -> list[dict]:
    """Itens de segurança que a seção nova não traz mais, literalmente."""
    existentes = {notas._key(f["texto"]) for f in novas}
    return [
        {"codigo": "item_de_seguranca", "texto": p["texto"][:160]}
        for p in protegidas_
        if notas._key(p["texto"]) not in existentes
    ]


def juntar(antigas_texto: str, frases: list[dict]) -> str:
    antigo = antigas_texto.strip()
    sep = "\n" if "\n" in antigo else "; " if "; " in antigo else " "
    return sep.join(f["texto"].strip() for f in frases)


def diff(antes: str, depois: str) -> dict:
    a, b = re.findall(r"\w+", antes.lower()), re.findall(r"\w+", depois.lower())
    pct, _, ops = _word_diff(a, b)
    return {
        "pct_editado": pct,
        "palavras_antes": len(a),
        "palavras_depois": len(b),
        "operacoes": [o for o in ops if o["op"] != "equal"],
    }
