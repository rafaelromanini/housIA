"""Pendências da consulta viram tarefas (#47): regras determinísticas sobre a fala do profissional e os protocolos.

Quatro regras, todas com a `Fala/N` (ou a referência FHIR do protocolo) como motivo:
- retorno em X ("retorno em 2 semanas", "volte daqui a 15 dias");
- solicitar um exame, parecer ou encaminhamento dito pelo profissional;
- ligar com o resultado;
- protocolo da linha de cuidado atrasado (e o gatilho de risco dito na conversa).

Só fala da equipe vira tarefa. Pergunta, negação ("não precisa retornar") e condicional ("se piorar, volte em 3
dias") não viram: são orientação ao paciente, não pendência da equipe. A IA não participa: nada aqui é removido por ela.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.consult.cronologia import TEAM, sentences
from app.consult.schemas import Insights, Segment, fala_ref
from app.consult.text import clauses, is_question, norm

REGRAS = ("consulta_retorno", "consulta_solicitar", "consulta_ligar_resultado", "consulta_protocolo")
TITULOS = {
    "consulta_retorno": "Retorno agendado na consulta",
    "consulta_solicitar": "Exame ou encaminhamento a solicitar",
    "consulta_ligar_resultado": "Ligar com o resultado",
    "consulta_protocolo": "Protocolo da linha de cuidado em atraso",
}
# papel responsável e prazo padrão (horas) por regra; o profissional edita na revisão
PAPEL = {
    "consulta_retorno": "medico",
    "consulta_solicitar": "medico",
    "consulta_ligar_resultado": "enfermagem",
    "consulta_protocolo": "medico",
}
PRAZO_HORAS = {"consulta_solicitar": 72, "consulta_ligar_resultado": 7 * 24, "consulta_protocolo": 30 * 24}
PRAZO_CRITICO_HORAS = 24

_NUM = {
    "um": 1, "uma": 1, "dois": 2, "duas": 2, "tres": 3, "quatro": 4, "cinco": 5, "seis": 6, "sete": 7, "oito": 8,
    "dez": 10, "quinze": 15, "vinte": 20, "trinta": 30,
}  # fmt: skip
_QTY = r"(?P<n>\d{1,3}|" + "|".join(_NUM) + r")\s+(?P<u>dia|dias|semana|semanas|mes|meses)"
_WHEN = re.compile(r"(?:em|daqui a|daqui|dentro de|de aqui a|apos)\s+" + _QTY)
_NEXT = re.compile(r"(?:na\s+|a\s+)?(?P<w>semana que vem|proxima semana|mes que vem|proximo mes|amanha|quinzena)")
_NEXT_DAYS = {
    "semana que vem": ("1 semana", 7), "proxima semana": ("1 semana", 7), "mes que vem": ("1 mês", 30),
    "proximo mes": ("1 mês", 30), "amanha": ("1 dia", 1), "quinzena": ("15 dias", 15),
}  # fmt: skip
_UNIT_DAYS = {"dia": 1, "semana": 7, "mes": 30}
_RETURN = re.compile(
    r"(?<![a-z])(retorn\w*|volt\w*|reavali\w*|reconsult\w*|nos vemos|me ver|ver de novo|rever|marcar? retorno)"
)
_CONDITIONAL = re.compile(
    r"^(se|caso|quando|a menos que|qualquer)\b|\b(se piorar|se nao melhorar|se persistir|se tiver)\b"
)
_NEGATION = re.compile(
    r"\b(nao (precisa|e preciso|ha necessidade|tem necessidade|vou|vamos|preciso)|nao (e |eh |ha )?necessari\w+|"
    r"sem necessidade|dispens\w+)\b"
)
_URGENT = re.compile(
    r"(?<![a-z])(urgente|urgencia|hoje mesmo|ainda hoje|imediat\w+|o quanto antes|prioridade|sem demora)"
)
# urgência negada ("isso não é urgente", "sem urgência") não marca a tarefa como crítica
_URGENT_NEGADA = re.compile(r"\b(nao|sem|nenhum\w*|nada)\s+(?:\w+\s+){0,2}?(urgente|urgencia|imediat\w+|prioridade)\b")


def _urgente(sentence: str) -> bool:
    return bool(_URGENT.search(_URGENT_NEGADA.sub(" ", sentence)))


_ASK = re.compile(
    r"(?<![a-z])(?:vou |vamos |irei |iremos |preciso |precisamos )?"
    r"(?:pedir|pedirei|pedimos|peco|solicitar|solicito|solicitarei|solicitamos|requisitar|mandar fazer|encaminhar|"
    r"encaminho|encaminharei)\s+(?:(?:a senhora|o senhor|voce|o paciente|a paciente|ele|ela)\s+)?"
    r"(?:(?:para|pra|pro|ao)\s+)?(?:um |uma |o |a |os |as |uns |umas |de |do |da |dos |das )*(?P<obj>[a-z0-9 ]{3,60})"
)
_OBJ_STOP = re.compile(
    r"\b(para|pra|pro|porque|pois|que|antes|depois|hoje|amanha|quando|mas|em|com a senhora|com o senhor)\b"
)
_NEW_VERB = re.compile(r"\be (?=(?:vou|vamos|irei|iremos|preciso|precisamos|pedir|solicit|encaminh|ligo|ligar)\b)")
_ORDERABLE = (
    "exame", "hemograma", "ecg", "eletro", "ultrassom", "usg", "raio", "rx", "tomografia", "ressonancia", "creatinina",
    "sangue", "urina", "colesterol", "hemoglobina", "glicada", "glicemia", "laboratorio", "retinografia", "mapa",
    "holter", "ecocardiograma", "eco", "cultura", "biopsia", "mamografia", "papanicolau", "colonoscopia", "endoscopia",
    "densitometria", "audiometria", "espirometria", "parecer", "encaminhamento", "avaliacao", "cardiologista",
    "oftalmologista", "endocrinologista", "nefrologista", "ortopedista", "neurologista", "psiquiatra", "fisioterapia",
    "nutricionista", "psicologo", "potassio", "tsh", "pcr", "troponina", "dimero", "perfil", "lipidograma",
)  # fmt: skip
_CALL = re.compile(
    r"(?<![a-z])(ligo|ligar|ligarei|ligamos|ligaremos|liga[mr]?os?|telefono|telefonar|telefonarei|entro em contato|"
    r"entrar em contato|entraremos em contato|entrarei em contato|aviso|avisarei|avisamos|avisaremos|"
    r"mando mensagem|retorno a ligacao)"
)
_RESULT = re.compile(r"(?:com|sobre|quando (?:sair|chegar|ficar pronto)|assim que (?:sair|chegar|ficar pronto))\s+"
                     r"(?:o |a |os |as )?(?P<r>resultados?|laudos?|retorno do exame)")  # fmt: skip
_RESULT_OF = re.compile(r"(?:resultado|laudo)s?\s+(?:d[oae]s?\s+)(?P<obj>[a-z0-9 ]{3,40})")


@dataclass
class Proposta:
    chave: str  # estável: finalizar de novo não duplica
    regra: str
    titulo: str
    detalhe: str
    papel: str
    horas: int
    critica: bool
    refs: list[str]
    assunto: str = ""
    extra: dict = field(default_factory=dict)


def _objects(raw: str) -> list[str]:
    """Os pedidos de uma oração ("um hemograma e uma tomografia" são dois), cortados nas palavras de fim de assunto."""
    obj = norm(raw)
    cut = _OBJ_STOP.search(obj)
    if cut and cut.start() > 0:
        obj = obj[: cut.start()]
    parts = [" ".join(x.split()[:6]) for x in re.split(r"\be\b", obj)]
    parts = [re.sub(r"^(um|uma|o|a|os|as|uns|umas|de|do|da|dos|das) ", "", x).strip() for x in parts]
    return [x for x in parts if x]


def _trim_obj(obj: str) -> str:
    found = _objects(obj)
    return found[0] if found else ""


def _quantity(clause: str) -> tuple[str, int] | None:
    """('2 semanas', dias) do prazo dito na oração, ou None."""
    m = _WHEN.search(clause)
    if m:
        raw = m.group("n")
        n = int(raw) if raw.isdigit() else _NUM[raw]
        unit = m.group("u")
        base = "mes" if unit.startswith("mes") else unit.rstrip("s")
        label = {"dia": "dia", "semana": "semana", "mes": "mês"}[base]
        if n != 1:
            label = "meses" if base == "mes" else label + "s"
        return f"{n if raw.isdigit() else raw} {label}", max(1, n) * _UNIT_DAYS[base]
    m = _NEXT.search(clause)
    if m:
        return _NEXT_DAYS[m.group("w")]
    return None


def _active(sentence: str) -> list[str]:
    """Orações da frase até a primeira condicional: "se piorar, volte em 3 dias" é orientação, não pendência."""
    out: list[str] = []
    for clause in clauses(sentence):
        if _CONDITIONAL.search(clause):
            break
        out.append(clause)
    return out


def _returns(sentence: str, ref: str) -> Proposta | None:
    for clause in _active(sentence):
        if _NEGATION.search(clause) or not _RETURN.search(clause):
            continue
        qty = _quantity(clause)
        if qty is None:
            continue  # "retorne se piorar" ou "volte logo": sem prazo não há tarefa
        label, days = qty
        crit = _urgente(sentence)
        return Proposta(
            chave=f"retorno:{ref}",
            regra="consulta_retorno",
            titulo=f"Retorno em {label}",
            detalhe="Agendar o retorno combinado na consulta e confirmar com o paciente.",
            papel=PAPEL["consulta_retorno"],
            horas=days * 24,
            critica=crit,
            refs=[ref],
            assunto=label,
        )
    return None


def _asks(sentence: str, ref: str) -> list[Proposta]:
    out: list[Proposta] = []
    parts = [p for clause in _active(sentence) for p in _NEW_VERB.split(clause)]
    for clause in parts:
        if _NEGATION.search(clause):
            continue
        for m in _ASK.finditer(clause):
            for obj in _objects(_URGENT.sub(" ", m.group("obj"))):  # "hemograma urgente" é o hemograma
                if not any(re.search(r"(?<![a-z])" + re.escape(k), obj) for k in _ORDERABLE):
                    continue
                out.append(
                    Proposta(
                        chave=f"solicitar:{ref}:{obj}",
                        regra="consulta_solicitar",
                        titulo=f"Solicitar {obj}",
                        detalhe="Pedido dito pelo profissional na consulta: conferir se foi solicitado e agendado.",
                        papel=PAPEL["consulta_solicitar"],
                        horas=PRAZO_HORAS["consulta_solicitar"],
                        critica=_urgente(sentence),
                        refs=[ref],
                        assunto=obj,
                    )
                )
    return out


def _calls(sentence: str, ref: str) -> Proposta | None:
    for clause in _active(sentence):
        if _NEGATION.search(clause) or not _CALL.search(clause):
            continue
        if not _RESULT.search(clause):
            continue
        of = _RESULT_OF.search(clause)
        obj = _trim_obj(of.group("obj")) if of else ""
        qty = _quantity(clause)
        horas = qty[1] * 24 if qty else PRAZO_HORAS["consulta_ligar_resultado"]
        return Proposta(
            chave=f"ligar:{ref}",
            regra="consulta_ligar_resultado",
            titulo=f"Ligar com o resultado{' de ' + obj if obj else ''}",
            detalhe="O profissional combinou avisar o paciente quando o resultado sair.",
            papel=PAPEL["consulta_ligar_resultado"],
            horas=horas,
            critica=_urgente(sentence),
            refs=[ref],
            assunto=obj,
        )
    return None


def extrair(segments: list[Segment], ins: Insights, patient_id: str) -> list[Proposta]:
    """Propostas de tarefa da consulta, sem duplicar a mesma chave. Só a fala da equipe vale."""
    out: list[Proposta] = []
    seen: set[str] = set()

    def add(p: Proposta | None, auto: bool = False) -> None:
        if p is not None and p.chave not in seen:
            seen.add(p.chave)
            if auto:  # o falante foi deduzido pelo sistema: o profissional confere na revisão
                p.extra["falante_auto"] = True
                p.detalhe += " Atenção: quem falou foi deduzido automaticamente; confirme que foi a equipe."
            out.append(p)

    for s in segments:
        if s.falante not in TEAM or s.origem == "marco" or s.expurgada:
            continue
        ref = fala_ref(s.n)
        for sentence in sentences(s.texto):
            if is_question(sentence):
                continue
            t = norm(sentence)
            add(_returns(t, ref), s.falante_auto)
            for p in _asks(t, ref):
                add(p, s.falante_auto)
            add(_calls(t, ref), s.falante_auto)
    for p in ins.protocolos:
        if p.status not in ("pendente", "critico"):  # "sem_registro" não é atraso: nunca foi registrado
            continue
        crit = p.status == "critico"
        add(
            Proposta(
                chave=f"protocolo:{p.id or p.protocolo + ':' + p.item}",
                regra="consulta_protocolo",
                titulo=f"{'Risco: ' if crit else 'Protocolo '}{p.protocolo}: {p.item}"[:300],
                detalhe=p.orientacao
                + (f" Fonte: {p.fonte}." if p.fonte else "")
                + (f" Último registro: {p.ultimo}." if p.ultimo else ""),
                papel=PAPEL["consulta_protocolo"],
                horas=PRAZO_CRITICO_HORAS if crit else PRAZO_HORAS["consulta_protocolo"],
                critica=crit,
                refs=list(p.refs) or [f"Patient/{patient_id}"],
                assunto=p.item,
            )
        )
    return out
