"""Confirmar ou descartar uma hipótese (#5): a decisão é do profissional e vale para o resto da consulta.

Confirmada ou provisória gera uma proposta de Condition (verificationStatus confirmed/provisional) no `note_json`, e o
CID entra nos CID da nota; descartada vira negativa pertinente na Avaliação e sai dos CID. A decisão fica em
`extra_json["hipoteses"]` (CID → decisão), como a queixa principal fixada, e é reaplicada a cada análise.
"""

from app.consult.schemas import Insights
from app.reference import icd_entry, norm_icd

DECISIONS = ("pendente", "confirmada", "provisoria", "descartada")
VERIFICATION = {"confirmada": "confirmed", "provisoria": "provisional"}


def key(code: str | None) -> str:
    return norm_icd(code) or (code or "").strip().upper()


def apply(ins: Insights, extra: dict | None) -> Insights:
    """Marca em cada hipótese a decisão do profissional (pelo CID)."""
    decided = (extra or {}).get("hipoteses") or {}
    for h in ins.hipoteses:
        d = decided.get(key(h.cid))
        if d:
            h.decisao, h.decisao_motivo = d["decisao"], d.get("motivo", "")
    return ins


def decide(extra: dict, code: str, decision: str, reason: str, who: str, when: str) -> dict:
    decided = dict(extra.get("hipoteses") or {})
    if decision == "pendente":
        decided.pop(key(code), None)
    else:
        decided[key(code)] = {"decisao": decision, "motivo": reason.strip(), "por": who, "em": when}
    return extra | {"hipoteses": decided}


def proposed_conditions(ins: Insights) -> list[dict]:
    """Propostas de Condition (nunca gravadas sozinhas: o envio ao prontuário é do profissional)."""
    out = []
    for h in ins.hipoteses:
        if h.decisao in VERIFICATION:
            entry = icd_entry(h.cid)
            out.append(
                {
                    "codigo": entry.code if entry else h.cid,
                    "descricao": entry.description if entry else h.hipotese,
                    "hipotese": h.hipotese,
                    "verificacao": VERIFICATION[h.decisao],
                    "motivo": h.decisao_motivo,
                    "refs": h.refs[:6],
                }
            )
    return out


def apply_to_note(note: dict, ins: Insights) -> dict:
    """Reflete as decisões na nota: propostas de Condition, CID confirmado/provisório presentes, CID descartado fora e
    a negativa pertinente na Avaliação."""
    kept = {c["codigo"]: c for c in note.get("cids", [])}
    decided = {h.cid: h for h in ins.hipoteses if h.decisao != "pendente"}
    for code in decided:
        if decided[code].decisao == "descartada":
            kept.pop(key(code), None)
            kept.pop(code, None)
    has_main = any(c.get("tipo") == "principal" for c in kept.values())
    for cond in proposed_conditions(ins):
        existing = kept.get(cond["codigo"])
        if existing and (existing["tipo"] != "hipotese" or cond["verificacao"] == "provisional"):
            continue  # já está na nota com um tipo que vale (ou continua hipótese, por ser provisória)
        main = cond["verificacao"] == "confirmed" and not has_main
        has_main = has_main or main
        tipo = "principal" if main else ("secundario" if cond["verificacao"] == "confirmed" else "hipotese")
        kept[cond["codigo"]] = {
            "codigo": cond["codigo"],
            "descricao": (existing or cond)["descricao"],
            "tipo": tipo,
            "refs": (existing or cond)["refs"],
        }
    note = note | {"cids": list(kept.values()), "condicoes_propostas": proposed_conditions(ins)}
    ruled_out = [h for h in ins.hipoteses if h.decisao == "descartada"]
    if ruled_out:
        avaliacao = dict(note.get("avaliacao") or {"texto": "", "refs": []})
        negatives = "; ".join(
            f"{h.hipotese} ({h.cid})" + (f" — {h.decisao_motivo}" if h.decisao_motivo else "") for h in ruled_out
        )
        text = f"Hipóteses afastadas pelo profissional: {negatives}."
        avaliacao["texto"] = (avaliacao.get("texto", "").rstrip() + " " + text).strip()
        avaliacao["refs"] = list(
            dict.fromkeys([*avaliacao.get("refs", []), *[r for h in ruled_out for r in h.refs[:2]]])
        )
        note["avaliacao"] = avaliacao
    return note
