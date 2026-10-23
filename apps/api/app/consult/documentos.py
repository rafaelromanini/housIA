"""Documentos derivados da consulta e resumo ao paciente (#45).

Cada documento sai da nota revisada, dos pedidos assinados, dos encaminhamentos decididos e do prontuário (FHIR), por
regras. Toda linha leva `refs` (referência FHIR e/ou `Fala/N`); linha sem fonte é descartada e contada em
`descartados`, a não ser o texto-padrão do próprio documento (`origem="modelo"`, sem afirmação clínica) e o que o
profissional escreve (`origem="profissional"`). O CID só entra no atestado e no relatório de convênio com a autorização
do paciente. Itens `obrigatorio` (alergia, sinais de alarme) não saem na edição. Nada vai ao prontuário sem aprovação do
profissional sobre o documento, e a nota de origem precisa estar revisada e atestada.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from app.consult import fidelidade, idiomas
from app.consult.chart import fmt_date, fmt_num
from app.consult.orders import OrderDraft
from app.discharge.documents import PURPOSE, ROUTE_LABEL, home_meds
from app.fhir_client import codes
from app.timeline.events import ClinicalEvent
from app.timeline.record import PatientRecord

TIPOS: dict[str, dict] = {
    "resumo_paciente": {"titulo": "Resumo para o paciente", "loinc": ("69730-0", "Instructions")},
    "atestado": {"titulo": "Atestado", "loinc": ("51852-2", "Letter")},
    "encaminhamento": {"titulo": "Encaminhamento", "loinc": ("57133-1", "Referral note")},
    "contrarreferencia": {"titulo": "Contrarreferência", "loinc": ("18761-7", "Transfer summary note")},
    "relatorio_convenio": {"titulo": "Relatório para convênio", "loinc": ("34133-9", "Summary of episode note")},
    "receita": {"titulo": "Receita", "loinc": ("57833-6", "Prescription for medication")},
    "solicitacao_exames": {"titulo": "Solicitação de exames", "loinc": ("46209-3", "Provider orders")},
}
FHIR_TYPES = {
    "Patient",
    "Encounter",
    "Condition",
    "Observation",
    "MedicationRequest",
    "ServiceRequest",
    "AllergyIntolerance",
    "Procedure",
    "DocumentReference",
    "Device",
}
SYSTEM = "http://housia.dev/fhir/NamingSystem/consulta-documento"
FOOTER = "Documento preparado com apoio de IA (HousIA); vale só depois de revisado e assinado pelo profissional."
MIN_LEGIVEL = 50  # abaixo disso (faixa "difícil") o resumo ganha aviso


class DocumentoIndisponivel(Exception):
    """O documento não pode ser montado com o que a consulta tem (ex.: nenhum encaminhamento decidido)."""


@dataclass
class Fontes:
    """Tudo o que os documentos leem, já carregado pelo serviço."""

    consulta_id: int
    paciente_id: str
    encontro_id: str | None
    nome: str
    quando: datetime
    nota: dict
    record: PatientRecord
    events: list[ClinicalEvent]
    pedidos: list[OrderDraft]
    encaminhamentos: list[dict]
    alergias: list[tuple[str, str]]  # (texto, ref)
    faturamento: dict = field(default_factory=dict)
    alta: dict | None = None  # avaliação de alta do paciente (assessment_out), só com o módulo
    profissional: str = ""
    selo: str | None = None
    idioma: str = "pt-BR"


# ---------- legibilidade em português (Flesch adaptado por Martins et al., 1996) ----------
_PALAVRA = re.compile(r"[A-Za-zÀ-ÿ]+")
_VOGAL = re.compile(r"[aeiouáàâãéêíóôõúü]+", re.IGNORECASE)
_FAIXAS = (
    (75, "muito fácil", "até o 5º ano"),
    (50, "fácil", "6º ao 9º ano"),
    (25, "difícil", "ensino médio"),
    (0, "muito difícil", "ensino superior"),
)


def silabas(palavra: str) -> int:
    """Sílabas por grupos de vogais; o hiato comum (ia, ua, io...) conta como um só, o que superestima a leitura."""
    return max(1, len(_VOGAL.findall(palavra)))


def legibilidade(texto: str) -> dict:
    """Índice de Flesch adaptado ao português: 248,835 − 1,015 × (palavras/frases) − 84,6 × (sílabas/palavras)."""
    palavras = _PALAVRA.findall(texto)
    frases = [f for f in re.split(r"[.!?;\n]+", texto) if _PALAVRA.search(f)]
    if not palavras or not frases:
        return {"indice": None, "faixa": "sem texto", "escolaridade": "", "palavras": 0, "frases": 0}
    indice = (
        248.835 - 1.015 * (len(palavras) / len(frases)) - 84.6 * (sum(silabas(p) for p in palavras) / len(palavras))
    )
    indice = round(max(0.0, min(100.0, indice)), 1)
    faixa, nivel = next((f, n) for corte, f, n in _FAIXAS if indice >= corte)
    return {
        "indice": indice,
        "faixa": faixa,
        "escolaridade": nivel,
        "palavras": len(palavras),
        "frases": len(frases),
    }


# ---------- itens ----------
def _item(texto: str, refs: list[str], origem: str = "regra", obrigatorio: bool = False) -> dict:
    return {
        "texto": texto.strip(),
        "refs": list(dict.fromkeys(refs)),
        "origem": origem,
        "obrigatorio": obrigatorio,
    }


class _Montador:
    """Junta seções e descarta o item sem fonte (a não ser o texto-padrão do modelo e o do profissional)."""

    def __init__(self) -> None:
        self.secoes: list[dict] = []
        self.descartados: list[dict] = []

    def secao(self, titulo: str, itens: list[dict]) -> None:
        kept = []
        for it in itens:
            if not it["texto"]:
                continue
            if not it["refs"] and it["origem"] not in ("modelo", "profissional"):
                self.descartados.append({"secao": titulo, "texto": it["texto"], "motivo": "sem fonte"})
                continue
            kept.append(it)
        if kept:
            self.secoes.append({"titulo": titulo, "itens": kept})

    def numerar(self) -> list[dict]:
        for i, s in enumerate(self.secoes):
            for j, it in enumerate(s["itens"]):
                it["id"] = f"s{i}.{j}"
        return self.secoes


def texto(secoes: list[dict]) -> str:
    out: list[str] = []
    for s in secoes:
        out.append(s["titulo"].upper())
        out += [f"- {it['texto']}" for it in s["itens"]]
        out.append("")
    return "\n".join(out).strip()


# ---------- peças reaproveitadas ----------
def _ref_base(f: Fontes) -> list[str]:
    return [f"Encounter/{f.encontro_id}"] if f.encontro_id else [f"Patient/{f.paciente_id}"]


def _sec(f: Fontes, key: str) -> dict:
    return f.nota.get(key) or {}


def _frases(f: Fontes, key: str) -> list[dict]:
    """Frases da seção da nota com as suas refs; seção sem frases usa o texto inteiro com as refs da seção."""
    sec = _sec(f, key)
    base = (sec.get("texto") or "").strip()
    if not base or base.lower().startswith("sem registro"):
        return []
    parts = sec.get("frases") or [{"texto": base, "refs": sec.get("refs", []), "origem": "regra"}]
    return [_item(p["texto"], p.get("refs", []), p.get("origem", "regra")) for p in parts if p.get("texto")]


def _cid_principal(f: Fontes) -> dict | None:
    cids = f.nota.get("cids") or []
    return next((c for c in cids if c.get("tipo") == "principal"), None) or (cids[0] if cids else None)


def _cid_item(f: Fontes) -> dict | None:
    cid = _cid_principal(f)
    if not cid:
        return None
    return _item(f"CID-10: {cid.get('codigo', '')} {cid.get('descricao', '')}".strip(), cid.get("refs", []) or [])


def _meds_em_uso(f: Fontes) -> list[dict]:
    return [_item(f"{m.name}: {m.text}", [m.ref]) for m in f.record.active_meds]


def _tendencias(f: Fontes, limite: int | None = None) -> list[dict]:
    """Última dosagem de cada exame com a variação em relação à anterior (as duas fontes citadas)."""
    por_chave: dict[str, list[ClinicalEvent]] = {}
    for e in f.events:
        if e.kind == "lab" and e.value and e.value.get("chave") in codes.LABS:
            por_chave.setdefault(e.value["chave"], []).append(e)
    out = []
    for chave, itens in por_chave.items():
        itens.sort(key=lambda e: e.timestamp)
        m = codes.LABS[chave]
        un = f" {m.unit}" if m.unit else ""
        atual = itens[-1]
        valor = fmt_num(float(atual.value["valor"]), m.decimals)
        if len(itens) == 1:
            out.append(_item(f"{m.label}: {valor}{un} ({fmt_date(atual.timestamp)}).", [atual.source_ref]))
            continue
        a, b = float(itens[-2].value["valor"]), float(atual.value["valor"])
        quando = "estável" if abs(b - a) < max(0.05 * abs(a), 10**-m.decimals) else ("subiu" if b > a else "caiu")
        out.append(
            _item(
                f"{m.label}: {valor}{un} ({fmt_date(atual.timestamp)}); {quando} em relação a "
                f"{fmt_num(a, m.decimals)}{un} ({fmt_date(itens[-2].timestamp)}).",
                [itens[-2].source_ref, atual.source_ref],
            )
        )
    out.sort(key=lambda it: "estável em relação" in it["texto"])  # o que mudou vem primeiro
    return out[:limite]


# ---------- resumo ao paciente ----------
def _sinais_alarme(f: Fontes) -> list[dict]:
    """Sinais de alarme por regra: os gerais, os do diagnóstico e do medicamento do paciente e os do encaminhamento."""
    conds = f.record.active_conditions
    ref_c = lambda *prefixos: [  # noqa: E731
        f"Condition/{c['id']}"
        for c in conds
        if any(((c.get("code", {}).get("coding") or [{}])[0].get("code", "")).startswith(p) for p in prefixos)
    ]
    meds = f.record.active_meds
    classe = lambda *cls: [m.ref for m in meds if m.drug_class in cls]  # noqa: E731
    base = _ref_base(f)
    itens = [
        _item(
            "Procure um pronto atendimento se tiver falta de ar, dor forte no peito, desmaio, febre alta ou uma dor "
            "forte que não passa com o remédio.",
            base,
            obrigatorio=True,
        )
    ]
    regras = [
        (
            ref_c("I50"),
            "Inchaço nas pernas, falta de ar ao deitar ou ganho de mais de 2 kg em 3 dias: procure atendimento.",
        ),
        (
            ref_c("I10", "I11", "I12", "I13"),
            "Dor de cabeça muito forte, visão turva ou fraqueza de um lado do corpo: procure atendimento.",
        ),
        (
            ref_c("E10", "E11", "E13", "E14") + classe("insulina_basal", "insulina_rapida", "sulfonilureia"),
            "Tremor, suor frio, tontura ou confusão podem ser açúcar baixo: tome algo com açúcar, meça a glicemia e "
            "procure atendimento se não melhorar.",
        ),
        (
            ref_c("N18"),
            "Urinar muito pouco, inchaço no rosto ou nas pernas ou muita sonolência: procure atendimento.",
        ),
        (
            classe("anticoagulante_oral", "heparina", "antiagregante"),
            "Sangramento (gengiva, urina escura, fezes pretas) ou manchas roxas grandes: procure atendimento.",
        ),
        (
            classe("aine"),
            "Fezes pretas, vômito com sangue ou dor forte no estômago: pare o remédio e procure atendimento.",
        ),
    ]
    itens += [_item(txt, refs, obrigatorio=True) for refs, txt in regras if refs]
    for e in f.encaminhamentos:  # sinais de urgência do protocolo da especialidade encaminhada
        if (e.get("decisao") or {}).get("escolha") == "encaminhar" and e.get("sinais_urgencia"):
            itens.append(_item(f"{e['sinais_urgencia'].rstrip('.')}.", e.get("refs") or [], obrigatorio=True))
    for d in f.pedidos:
        if d.tipo == "medicamento" and d.acao in ("iniciar", "alterar_dose"):
            itens.append(
                _item(
                    f"Se aparecer coceira, inchaço no rosto ou falta de ar depois de tomar {d.item}, pare e procure "
                    "atendimento.",
                    d.refs,
                    obrigatorio=True,
                )
            )
    return itens


def _remedios_paciente(f: Fontes) -> list[dict]:
    itens = []
    parados = {d.prescricao_ref for d in f.pedidos if d.acao == "suspender" and d.prescricao_ref}
    for m in home_meds(f.record):
        if m.ref in parados:
            continue
        horarios = ", ".join(m.times) if m.times else ("quando precisar" if m.prn else "conforme a receita")
        itens.append(
            _item(
                f"{m.name}, {m.dose}, {ROUTE_LABEL.get(m.route, m.route)}. Horários: {horarios}. "
                f"Serve para {PURPOSE.get(m.drug_class, 'o tratamento combinado com seu médico')}.",
                [m.ref],
            )
        )
    for d in f.pedidos:
        if d.tipo != "medicamento":
            continue
        partes = [p for p in (d.dose, d.via, d.frequencia, d.duracao) if p]
        detalhe = ", ".join(partes)
        if d.acao == "iniciar":
            itens.append(_item(f"Remédio novo: {d.item}" + (f", {detalhe}" if detalhe else "") + ".", d.refs))
        elif d.acao == "alterar_dose":
            itens.append(_item(f"Mudou a dose de {d.item}: agora {detalhe or d.dose}.", d.refs))
        elif d.acao == "suspender":
            itens.append(_item(f"Pare de tomar {d.item}.", d.refs))
    return itens


def _retorno(f: Fontes) -> list[dict]:
    plano = _sec(f, "plano")
    achados = [
        fr
        for fr in (plano.get("frases") or [{"texto": plano.get("texto", ""), "refs": plano.get("refs", [])}])
        if re.search(r"\b(retorn\w+|volt\w+|reavali\w+)\b", fr.get("texto", ""), re.IGNORECASE)
    ]
    return [_item(fr["texto"], fr.get("refs", []), fr.get("origem", "regra")) for fr in achados]


def _exames_paciente(f: Fontes) -> list[dict]:
    pedidos = [
        _item(f"Exame pedido: {d.item}" + (f". Preparo: {d.preparo}" if d.preparo else "") + ".", d.refs)
        for d in f.pedidos
        if d.tipo == "exame"
    ]
    pendentes = [_item(f"Aguardando resultado: {e.texto}.", [e.ref]) for e in f.record.pending_exams]
    return pedidos + pendentes + _tendencias(f, 8)


def resumo_paciente(f: Fontes, opcoes: dict) -> _Montador:
    m = _Montador()
    base = _ref_base(f)
    queixa = _sec(f, "queixa_principal")
    if (queixa.get("texto") or "").strip() and not queixa["texto"].lower().startswith("sem registro"):
        m.secao("Por que você veio", [_item(queixa["texto"], queixa.get("refs") or [])])
    m.secao("Seus remédios", _remedios_paciente(f))
    m.secao(
        "Alergias",
        [
            _item(f"Você tem alergia a: {texto_}. Avise sempre que for atendido.", [ref], obrigatorio=True)
            for texto_, ref in f.alergias
        ],
    )
    m.secao("Sinais de alarme", _sinais_alarme(f))
    m.secao("Seus exames", _exames_paciente(f))
    retorno = _retorno(f) or [_item("Combine com a equipe a data da próxima consulta.", base, "modelo")]
    m.secao("Retorno", retorno)
    orientacoes = [o for o in f.nota.get("orientacoes_paciente") or [] if str(o).strip()]
    for o in orientacoes:  # texto solto da nota, sem fonte: não entra no resumo
        m.descartados.append({"secao": "Orientações da nota", "texto": str(o), "motivo": "sem fonte"})
    return m


# ---------- demais documentos ----------
def atestado(f: Fontes, opcoes: dict) -> _Montador:
    m = _Montador()
    base = _ref_base(f)
    itens = [
        _item(f"Atesto que {f.nome} esteve em consulta em {fmt_date(f.quando)}.", base, "modelo"),
    ]
    dias = opcoes.get("dias")
    if isinstance(dias, int) and dias > 0:
        itens.append(
            _item(
                f"Necessita de afastamento das atividades por {dias} dia{'s' if dias > 1 else ''}, "
                f"a contar de {fmt_date(f.quando)}.",
                base,
                "profissional",
            )
        )
    m.secao("Atestado", itens)
    if opcoes.get("incluir_cid"):
        if opcoes.get("autorizacao_cid") is True:
            cid = _cid_item(f)
            if cid:
                cid["texto"] += " (informado com autorização do paciente)"
                m.secao("Diagnóstico", [cid])
            else:
                m.descartados.append({"secao": "Diagnóstico", "texto": "CID", "motivo": "a nota não tem CID"})
        else:
            m.descartados.append(
                {"secao": "Diagnóstico", "texto": "CID", "motivo": "sem autorização do paciente para informar o CID"}
            )
    return m


def _resumo_clinico(f: Fontes) -> list[tuple[str, list[dict]]]:
    return [
        ("Motivo e história", _frases(f, "queixa_principal") + _frases(f, "hda")),
        ("Avaliação", _frases(f, "avaliacao")),
        ("Conduta", _frases(f, "plano")),
        ("Exames", _frases(f, "exames_complementares") + _tendencias(f)),
        ("Medicamentos em uso", _meds_em_uso(f)),
        ("Alergias", [_item(t, [r], obrigatorio=True) for t, r in f.alergias]),
    ]


def encaminhamento(f: Fontes, opcoes: dict) -> _Montador:
    decididos = [e for e in f.encaminhamentos if (e.get("decisao") or {}).get("escolha") == "encaminhar"]
    if not decididos:
        raise DocumentoIndisponivel("Nenhum encaminhamento foi decidido na consulta.")
    m = _Montador()
    for e in decididos:
        itens = [_item(f"Encaminhar para {e['especialidade']}: {e['condicao']}.", e.get("refs") or [])]
        itens += [
            _item(f"{c['texto']}: {c['status']}", c.get("refs") or [])
            for c in e.get("criterios", [])
            if c.get("texto") and c.get("refs")
        ]
        if e.get("manejo_aps"):
            itens.append(_item(f"Manejo já feito na atenção primária: {e['manejo_aps']}", e.get("refs") or []))
        if (e.get("decisao") or {}).get("motivo"):
            itens.append(_item(e["decisao"]["motivo"], e.get("refs") or [], "profissional"))
        m.secao(f"Encaminhamento: {e['especialidade']}", itens)
    for titulo, itens in _resumo_clinico(f):
        m.secao(titulo, itens)
    return m


def contrarreferencia(f: Fontes, opcoes: dict) -> _Montador:
    m = _Montador()
    for titulo, itens in _resumo_clinico(f):
        m.secao(titulo, itens)
    m.secao("Pendências da consulta", [_item(p["texto"], p.get("refs") or []) for p in f.nota.get("pendencias") or []])
    m.secao("Exames aguardando resultado", [_item(f"{e.texto}.", [e.ref]) for e in f.record.pending_exams])
    if f.alta:  # paciente de alta: pendências e reconciliação do Waze (avaliação de alta)
        achados = [_item(s["texto"], s.get("refs") or []) for s in f.alta.get("sugestoes", [])]
        achados.append(
            _item(
                f"Risco de reinternação {f.alta['final_risk']} (LACE {f.alta['lace']['score']}, social "
                f"{f.alta['social']['score']}).",
                [f"DischargeAssessment/{f.alta['id']}"],
            )
        )
        m.secao("Transição de cuidado (alta)", achados)
        m.secao("Reconciliação medicamentosa", _meds_em_uso(f))
    return m


def relatorio_convenio(f: Fontes, opcoes: dict) -> _Montador:
    m = _Montador()
    guia = []
    if f.faturamento.get("convenio"):
        guia.append(_item(f"Convênio: {f.faturamento['convenio']}.", _ref_base(f), "profissional"))
    if f.faturamento.get("numero_guia"):
        guia.append(_item(f"Guia nº {f.faturamento['numero_guia']}.", _ref_base(f), "profissional"))
    m.secao("Identificação", guia)
    for titulo, itens in _resumo_clinico(f)[:4]:
        m.secao(titulo, itens)
    if opcoes.get("autorizacao_cid") is True:
        cid = _cid_item(f)
        m.secao("Diagnóstico", [cid] if cid else [])
    else:
        m.descartados.append({"secao": "Diagnóstico", "texto": "CID", "motivo": "sem autorização do paciente"})
    m.secao(
        "Procedimentos e exames solicitados",
        [
            _item(f"{d.item}" + (f" ({d.loinc_nome})" if d.loinc_nome else ""), d.refs)
            for d in f.pedidos
            if d.tipo == "exame"
        ],
    )
    return m


def receita(f: Fontes, opcoes: dict) -> _Montador:
    remedios = [d for d in f.pedidos if d.tipo == "medicamento"]
    if not remedios:
        raise DocumentoIndisponivel("Nenhuma receita foi assinada na consulta.")
    m = _Montador()
    itens = []
    for d in remedios:
        verbo = {"iniciar": "", "alterar_dose": "Alterar dose: ", "suspender": "Suspender: "}.get(d.acao, "")
        partes = " · ".join(p for p in (d.item, d.dose, d.via, d.frequencia, d.duracao) if p)
        extra = f" (dose atual: {d.dose_anterior})" if d.acao == "alterar_dose" and d.dose_anterior else ""
        itens.append(_item(f"{verbo}{partes}{extra}", d.refs))
    m.secao("Prescrição", itens)
    return m


def solicitacao_exames(f: Fontes, opcoes: dict) -> _Montador:
    exames = [d for d in f.pedidos if d.tipo == "exame"]
    if not exames:
        raise DocumentoIndisponivel("Nenhum exame foi assinado na consulta.")
    m = _Montador()
    m.secao(
        "Exames solicitados",
        [
            _item(
                d.item
                + (f" ({d.loinc_nome})" if d.loinc_nome else "")
                + (f". Preparo: {d.preparo}" if d.preparo else ""),
                d.refs,
            )
            for d in exames
        ],
    )
    cid = _cid_item(f) if opcoes.get("autorizacao_cid") is True else None
    m.secao("Hipótese diagnóstica", [cid] if cid else [])
    return m


BUILDERS = {
    "resumo_paciente": resumo_paciente,
    "atestado": atestado,
    "encaminhamento": encaminhamento,
    "contrarreferencia": contrarreferencia,
    "relatorio_convenio": relatorio_convenio,
    "receita": receita,
    "solicitacao_exames": solicitacao_exames,
}


def disponibilidade(f: Fontes) -> dict[str, str | None]:
    """Por tipo: None se pode ser gerado agora, senão o motivo (para a aba Documentos)."""
    out: dict[str, str | None] = {}
    for tipo, fn in BUILDERS.items():
        try:
            fn(f, {})
            out[tipo] = None
        except DocumentoIndisponivel as exc:
            out[tipo] = str(exc)
    return out


def hash_nota(nota: dict) -> str:
    return hashlib.sha256(json.dumps(nota, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:16]


def gerar(tipo: str, f: Fontes, opcoes: dict | None = None) -> dict:
    """Rascunho do documento: seções com itens (texto, refs, origem), descartados, avisos e a legibilidade (resumo)."""
    opcoes = dict(opcoes or {})
    m = BUILDERS[tipo](f, opcoes)
    secoes = m.numerar()
    spec = TIPOS[tipo]
    doc = {
        "tipo": tipo,
        "titulo": spec["titulo"],
        "loinc": {"codigo": spec["loinc"][0], "display": spec["loinc"][1]},
        "idioma": "pt-BR",
        "gerado_em": f.quando.isoformat(),
        "nota_hash": hash_nota(f.nota),
        "opcoes": {k: v for k, v in opcoes.items() if k in ("dias", "incluir_cid", "autorizacao_cid")},
        "secoes": secoes,
        "descartados": m.descartados,
        "avisos": [],
        "aprovado": None,
        "fhir_ref": None,
    }
    if tipo == "resumo_paciente":
        doc["legibilidade"] = legibilidade(" ".join(it["texto"] for s in secoes for it in s["itens"]))
        if doc["legibilidade"]["indice"] is not None and doc["legibilidade"]["indice"] < MIN_LEGIVEL:
            doc["avisos"].append("Texto difícil para o paciente: simplifique antes de aprovar.")
        if m.descartados:
            doc["avisos"].append(f"{len(m.descartados)} item(ns) ficaram de fora por não terem fonte.")
    if opcoes.get("incluir_cid") and opcoes.get("autorizacao_cid") is not True:
        doc["avisos"].append("CID omitido: o paciente não autorizou informá-lo.")
    return doc


# ---------- idioma do paciente (versão adaptada, com a conferência por regra) ----------
def adaptar(doc: dict, idioma: str, traduzir) -> dict:
    """Versão no idioma do paciente: `traduzir({id: texto}, idioma) -> {id: texto}`. Cada item é conferido por
    `fidelidade.divergencias` (números, medicamentos, período, negação); o que diverge fica em português e vai para
    `divergentes`. Sem tradutor (mock, IA recusada) o documento fica em português com o aviso."""
    base = idiomas.resolver(idioma) or idioma
    if base == "pt-BR" or traduzir is None:
        if base != "pt-BR":
            doc["avisos"].append(f"Versão em {idiomas.rotulo(idioma)} indisponível: documento em português.")
        return doc
    originais = {it["id"]: it["texto"] for s in doc["secoes"] for it in s["itens"]}
    novos = traduzir(originais, idioma) or {}
    if not novos:
        doc["avisos"].append(f"Versão em {idiomas.rotulo(idioma)} indisponível: documento em português.")
        return doc
    divergentes = []
    for s in doc["secoes"]:
        for it in s["itens"]:
            novo = (novos.get(it["id"]) or "").strip()
            if not novo or idiomas.placeholder(novo):
                continue
            problemas = fidelidade.divergencias(it["texto"], novo, "pt-BR", base)
            if problemas:
                divergentes.append({"id": it["id"], "problemas": problemas})
                continue
            it["texto_original"], it["texto"] = it["texto"], novo
    doc["idioma"] = base if len(divergentes) < len(originais) else "pt-BR"
    doc["divergentes"] = divergentes
    if divergentes:
        doc["avisos"].append(f"{len(divergentes)} item(ns) ficaram em português por divergirem na conferência.")
    return doc


# ---------- edição pelo profissional ----------
def editar(atual: dict, secoes: list[dict]) -> tuple[dict, list[str]]:
    """Aplica a edição de texto item a item (por `id`). Item alterado vira origem "profissional" e mantém as refs;
    item novo (sem `id` conhecido) exige origem "profissional"; item `obrigatorio` não pode ser removido nem esvaziado.
    Devolve (documento novo, erros)."""
    erros: list[str] = []
    antigos = {it["id"]: it for s in atual["secoes"] for it in s["itens"]}
    vistos: set[str] = set()
    novas = []
    for s in secoes:
        itens = []
        for it in s.get("itens", []):
            texto_ = str(it.get("texto", "")).strip()
            velho = antigos.get(it.get("id", ""))
            if velho is None:
                if texto_:
                    itens.append(_item(texto_, [], "profissional") | {"id": f"n{len(vistos)}-{len(itens)}"})
                continue
            if not texto_:
                continue  # item removido (a checagem de obrigatório vem abaixo)
            vistos.add(velho["id"])
            mudou = texto_ != velho["texto"]
            itens.append(
                velho | {"texto": texto_, "origem": "profissional" if mudou else velho["origem"]},
            )
        if itens:
            novas.append({"titulo": s.get("titulo") or "", "itens": itens})
    for ident, velho in antigos.items():
        if velho.get("obrigatorio") and ident not in vistos:
            erros.append(f"O item obrigatório de segurança não pode ser removido: {velho['texto'][:60]}")
    doc = atual | {"secoes": novas, "aprovado": None}
    if atual["tipo"] == "resumo_paciente":
        doc["legibilidade"] = legibilidade(" ".join(it["texto"] for s in novas for it in s["itens"]))
    return doc, erros


# ---------- FHIR ----------
def document_reference(doc: dict, f: Fontes, texto_: str) -> dict:
    """DocumentReference próprio do documento (LOINC do tipo, identificador `{consulta}:{tipo}`), ligado ao encontro."""
    import base64

    code, display = doc["loinc"]["codigo"], doc["loinc"]["display"]
    label = f"{doc['titulo']} (HousIA)"
    resource = {
        "resourceType": "DocumentReference",
        "status": "current",
        "docStatus": "final" if doc.get("aprovado") else "preliminary",
        "identifier": [{"system": SYSTEM, "value": f"{f.consulta_id}:{doc['tipo']}"}],
        "type": {"coding": [{"system": codes.LOINC, "code": code, "display": display}], "text": label},
        "subject": {"reference": f"Patient/{f.paciente_id}", "display": f.nome},
        "date": f.quando.isoformat(),
        "author": [{"display": f.profissional}],
        "description": label,
        "content": [
            {
                "attachment": {
                    "contentType": "text/plain; charset=utf-8",
                    "data": base64.b64encode(texto_.encode("utf-8")).decode("ascii"),
                    "title": label,
                    "language": doc.get("idioma") or "pt-BR",
                }
            }
        ],
    }
    if f.encontro_id:
        resource["context"] = {"encounter": [{"reference": f"Encounter/{f.encontro_id}"}]}
    fontes = sorted({r for s in doc["secoes"] for it in s["itens"] for r in it["refs"] if not r.startswith("Fala/")})
    related = [r for r in fontes if r.split("/")[0] in FHIR_TYPES]
    if related:
        resource["context"] = (resource.get("context") or {}) | {"related": [{"reference": r} for r in related]}
    return resource


def texto_final(doc: dict, f: Fontes) -> str:
    cab = [f"{doc['titulo']} — {f.nome}", f"Data: {fmt_date(f.quando)}", ""]
    rodape = ["", FOOTER, f.profissional] + ([f.selo] if f.selo else [])
    codigo = f"Código de verificação: {doc['nota_hash']}"
    return "\n".join(cab) + texto(doc["secoes"]) + "\n" + "\n".join(rodape) + "\n" + codigo
