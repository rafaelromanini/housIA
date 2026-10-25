"""Autorização prévia (#56): exigências do Rol da ANS (DUT) e da operadora ao vivo, e o pacote de justificativa.

Determinístico, roda com ou sem LLM. Quando o médico cita um exame, procedimento ou medicamento de alto custo (na fala
ou num rascunho de pedido), o motor avalia a regra correspondente contra a consulta e o prontuário do paciente:
`atendido` (com a evidência: Fala/N ou recurso FHIR), `faltando` (com o que perguntar ou documentar) ou
`nao_aplicavel`. Regra de ouro: sem evidência é `faltando`; nada é inferido (CID só como hipótese não vale, idade sem
nascimento não vale, tratamento só planejado não vale). A base de regras é de EXEMPLO até a instituição importar a
oficial (ver `autorizacao_regras.py`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.consult import hipoteses, knowledge, protocolos
from app.consult.autorizacao_regras import Base, Criterio, Regra, achar_operadora
from app.consult.chart import ChartView, fmt_date
from app.consult.encaminhamento import CONCORDANCIA, CONDICIONAL, FRASE
from app.consult.protocolos import OUTRA_PESSOA, TAMBEM_EU
from app.consult.schemas import (
    Autorizacao,
    DocumentoAutorizacao,
    Hypothesis,
    ItemAutorizacao,
    OperadoraAutorizacao,
    OrderDraft,
    Question,
    RegraAutorizacao,
    Segment,
    fala_ref,
)
from app.consult.text import NEG_WORDS, clauses, find, is_question, norm
from app.reference import norm_icd

MAX_FRASE = 600
MAX_FRASES = 30
MAX_PERGUNTAS = 4
FALANTES_GATILHO = ("medico", "desconhecido")  # quem pede o exame ou o procedimento é o profissional
# o pedido citado é de algo que já foi feito ou é resultado: não abre autorização
PASSADO = re.compile(
    r"(?<![a-z])(fez|fiz|fizemos|fizeram|feito|feita|realizou|realizada|realizei|ja fez|ultima|ultimo|anterior|"
    r"resultado|laudo|mostrou|mostra)(?![a-z])"
)
# "vou pedir fisioterapia" é plano, não tratamento já feito
FUTURO = re.compile(
    r"(?<![a-z])(vou|vamos|iremos|irei|farei|pretendo|quero|preciso|precisamos|posso|podemos|indico|recomendo|"
    r"sugiro|proponho)(?![a-z])"
)
NAO_CONFIRMADA = {"unconfirmed", "provisional", "differential", "refuted", "entered-in-error"}
SISTEMA_VERIFICACAO = "http://terminology.hl7.org/CodeSystem/condition-ver-status"


@dataclass
class Ctx:
    """O que a avaliação lê: prontuário, falas, hipóteses e laudos desta consulta e deste paciente (nada de outros)."""

    chart: ChartView
    segments: list[Segment]
    hyps: list[Hypothesis]
    extra: dict
    hoje: date
    patient_id: str

    @property
    def age(self) -> int | None:
        """Idade pelo nascimento do cadastro; data ausente, parcial, inválida ou futura não vale (nada é inferido)."""
        try:
            idade = self.chart.r.age
        except (ValueError, TypeError):
            return None
        return idade if idade is not None and 0 <= idade <= 130 else None


@dataclass
class Resultado:
    ok: bool
    evidencia: str = ""
    detalhe: str = ""  # para a frase da justificativa
    refs: list[str] = field(default_factory=list)


@dataclass
class Gatilho:
    regra: Regra
    tipo: str  # fala | pedido
    texto: str
    refs: list[str]


# ---------- avaliação de uma regra de critério ----------
def _cid_confirmados(ctx: Ctx) -> list[tuple[str, str, list[str]]]:
    """(CID, descrição, refs) do que está CONFIRMADO: diagnóstico ativo no prontuário e hipótese que o profissional
    confirmou nesta consulta (com fala ou recurso que a sustente). Hipótese pendente ou provisória não vale."""
    out: list[tuple[str, str, list[str]]] = []
    for icd, texto, c in ctx.chart.conditions:
        ver = {
            (cod.get("code") or "")
            for cod in ((c.get("verificationStatus") or {}).get("coding") or [])
            if cod.get("system", SISTEMA_VERIFICACAO) == SISTEMA_VERIFICACAO
        }
        if icd and not (ver & NAO_CONFIRMADA):
            out.append((icd, texto, [f"Condition/{c['id']}"]))
    decididas = ctx.extra.get("hipoteses") or {}
    for h in ctx.hyps:
        d = decididas.get(hipoteses.key(h.cid)) or {}
        if d.get("decisao") == "confirmada" and h.refs:
            out.append((h.cid, h.hipotese, list(h.refs[:3])))
    return out


def _cid_bate(code: str, prefixos: list[str]) -> bool:
    c = (norm_icd(code) or code or "").replace(".", "").upper()
    return any(c.startswith(p.replace(".", "").upper()) for p in prefixos if p)


# depois do termo, "sem melhora" / "não adiantou" é o resultado do tratamento (que foi feito), não negação do tratamento
FALHA_DO_TRATAMENTO = {
    "melhora", "melhorou", "melhorei", "melhorando", "adiantou", "adiantando", "resposta", "resultado", "resultados",
    "funcionou", "resolveu", "ajudou", "ajuda", "efeito", "alivio", "aliviou", "surtiu", "sucesso", "exito",
    "controle", "controlou", "controla", "resolve", "adianta", "funciona", "cura", "curou",
}  # fmt: skip
NEG_DEPOIS_JANELA = 5  # palavras depois do termo em que um "não"/"nunca" ainda nega ("fisioterapia eu nunca fiz")
FECHA_NEGACAO = NEG_WORDS | {
    "ainda",
    "ate",
    "hoje",
    "agora",
    "doutor",
    "doutora",
    "fiz",
    "fez",
    "tive",
    "tenho",
    "tomei",
}


def _negado_depois(clause: str, fim: int, proxima: str | None) -> bool:
    """Negação que vem DEPOIS do termo: "fisioterapia eu nunca fiz" ou "fisioterapia, não" (a oração seguinte é só o
    não). "Sem melhora"/"não adiantou" descreve o resultado do tratamento e não o nega."""
    tail = clause[fim:].split()
    for i, w in enumerate(tail[:NEG_DEPOIS_JANELA]):
        if w in NEG_WORDS:
            return not (i + 1 < len(tail) and tail[i + 1] in FALHA_DO_TRATAMENTO)
    if proxima is not None:
        palavras = proxima.split()
        return bool(palavras) and all(w in FECHA_NEGACAO for w in palavras) and any(w in NEG_WORDS for w in palavras)
    return False


def _afirma(sentence: str, termos: list[str], *, plano_vale: bool) -> bool:
    """A frase (normalizada) cita algum termo de forma afirmativa: sem pergunta, sem negação antes do termo na mesma
    oração nem logo depois dele, sem ser de outra pessoa ("minha mãe fez fisioterapia") e (se `plano_vale` for falso)
    sem intenção futura ("vou pedir fisioterapia" não é tratamento já feito)."""
    partes = clauses(sentence)
    for i, clause in enumerate(partes):
        hit = find(clause, tuple(termos))
        if hit is None:
            continue
        antes = clause[: hit[1]].split()
        if any(w in NEG_WORDS for w in antes):
            continue
        if _negado_depois(clause, hit[2], partes[i + 1] if i + 1 < len(partes) else None):
            continue
        if OUTRA_PESSOA.search(clause) and not TAMBEM_EU.search(clause):
            continue  # relato sobre a mãe, o marido, o vizinho: não é o paciente desta consulta
        if not plano_vale and FUTURO.search(clause):
            continue
        return True
    return False


def _frases(texto: str):
    for raw in FRASE.split(texto.strip()):
        sentence = norm(raw)
        if not sentence:
            continue
        if raw.rstrip().endswith("?") and not CONCORDANCIA.search(sentence):
            continue
        if is_question(raw) and "?" not in raw:
            continue
        yield sentence


def _falas_validas(ctx: Ctx, termos: list[str]) -> list[Segment]:
    """Falas (do paciente ou do profissional) que afirmam o termo: negado, perguntado ou só planejado não conta."""
    out = []
    for s in ctx.segments:
        if (
            s.marco or s.expurgada or s.falante_incerto
        ):  # falante incerto (#40): vale só depois de o profissional conferir
            continue
        if any(not protocolos.ADIADO.search(f) and _afirma(f, termos, plano_vale=False) for f in _frases(s.texto)):
            out.append(s)
    return out


def _exame(spec: str, ctx: Ctx, rotulo: str = "") -> Resultado:
    kind_spec, _, dias = spec.partition("@")
    kind, _, alvo = kind_spec.partition(":")
    limite = int(dias) if dias else 36500
    if kind == "lab":
        for laudo in ctx.extra.get("laudos") or []:  # laudos lidos do PDF/foto (#48), com Documento/N p. X
            for v in laudo.get("valores") or []:
                if v.get("chave") == alvo and v.get("ref"):
                    return Resultado(True, f"{codes_label(alvo)} no laudo {v['ref']}", refs=[v["ref"]])
    probe = knowledge.ProtocolDef(
        protocol="",
        condition="",
        item=rotulo,
        source=f"{kind}:{alvo.replace('~', '|')}",
        interval_days=limite,
        guidance="",
    )
    status, ultimo, refs = ctx.chart._protocol_record(probe)
    if status == "em_dia":
        return Resultado(True, f"No prontuário: {ultimo}", detalhe=ultimo or "", refs=refs)
    if status == "pendente":
        return Resultado(False, f"Desatualizado (mais de {limite} dias): {ultimo}", refs=[])
    return Resultado(False, "Sem registro no prontuário.")


def codes_label(key: str) -> str:
    from app.fhir_client import codes  # noqa: PLC0415

    m = codes.LABS.get(key)
    return m.label if m else key


def avaliar_regra(regra: str, ctx: Ctx, rotulo: str = "") -> Resultado:
    """Resultado de uma regra de critério. Sem evidência no prontuário ou na conversa, `ok` é falso."""
    regra = regra.strip()
    if regra == "manual":
        return Resultado(False, "Depende da documentação do profissional.")
    if regra.startswith("cid:"):
        prefixos = [p for p in regra[4:].split(",") if p]
        for code, texto, refs in _cid_confirmados(ctx):
            if _cid_bate(code, prefixos):
                return Resultado(True, f"CID {code} confirmado ({texto})", detalhe=f"CID {code}, {texto}", refs=refs)
        hip = next((h for h in ctx.hyps if _cid_bate(h.cid, prefixos) and h.decisao != "descartada"), None)
        if hip is not None:
            return Resultado(
                False, f"CID {hip.cid} só como hipótese: confirme o diagnóstico ou registre-o no prontuário."
            )
        return Resultado(False, "Nenhum diagnóstico (CID) compatível confirmado.")
    m = re.match(r"^idade(>=|<=|>|<)(\d+)$", regra)
    if m:
        if ctx.age is None:
            return Resultado(False, "Data de nascimento não cadastrada.")
        op, lim = m.group(1), int(m.group(2))
        ok = {">=": ctx.age >= lim, "<=": ctx.age <= lim, ">": ctx.age > lim, "<": ctx.age < lim}[op]
        txt = f"Idade {ctx.age} anos (exigido {op} {lim})"
        return Resultado(
            ok, txt, detalhe=f"paciente com {ctx.age} anos", refs=[f"Patient/{ctx.patient_id}"] if ok else []
        )
    if regra.startswith("fala:"):
        falas = _falas_validas(ctx, [t for t in regra[5:].split("~") if t])
        if falas:
            refs = protocolos.refs_das_falas(falas, 3)
            return Resultado(True, "Dito na consulta: " + ", ".join(refs), refs=refs)
        return Resultado(False, "Não citado na conversa até agora.")
    m = re.match(r"^lab:(\w+)([<>])(\d+(?:\.\d+)?)$", regra)
    if m:
        key, op, lim = m.group(1), m.group(2), float(m.group(3))
        validos = [  # exame com data futura não é evidência
            e
            for e in ctx.chart.labs.get(key, [])
            if timedelta(0) <= ctx.chart.now - e.timestamp <= timedelta(days=3650)
        ]
        ev = validos[-1] if validos else None
        if ev is None:
            return Resultado(False, f"Sem {codes_label(key)} no prontuário.")
        valor = float(ev.value["valor"])
        ok = valor > lim if op == ">" else valor < lim
        txt = f"{codes_label(key)} {str(valor).replace('.', ',')} em {fmt_date(ev.timestamp)}"
        return Resultado(ok, txt, detalhe=txt, refs=[ev.source_ref] if ok else [])
    if regra.startswith("exame:"):
        return _exame(regra[6:], ctx, rotulo)
    return Resultado(False, "Regra não reconhecida.")


def _item(regra: Regra, c: Criterio, ctx: Ctx) -> tuple[ItemAutorizacao, Resultado | None]:
    fonte = f"{regra.id}/{c.id} · v{regra.versao}"
    if c.aplica and not avaliar_regra(c.aplica, ctx).ok:
        return ItemAutorizacao(id=c.id, texto=c.texto, status="nao_aplicavel", fonte=fonte), None
    res = avaliar_regra(c.regra, ctx, c.texto)
    if res.ok and res.refs:
        return (
            ItemAutorizacao(
                id=c.id, texto=c.texto, status="atendido", evidencia=res.evidencia, refs=res.refs, fonte=fonte
            ),
            res,
        )
    return (  # sem evidência com fonte = faltando (nunca se infere)
        ItemAutorizacao(
            id=c.id,
            texto=c.texto,
            status="faltando",
            evidencia=res.evidencia,
            fonte=fonte,
            pergunta=c.pergunta or f"Documentar: {c.texto}",
        ),
        None,
    )


def _criterios_da_regra(regra: Regra) -> list[Criterio]:
    """Critérios derivados dos campos da linha (CID e faixa de idade) e os escritos na coluna `criterios`."""
    out: list[Criterio] = []
    if regra.cids:
        out.append(
            Criterio(
                "cid",
                "Diagnóstico (CID) confirmado compatível com a indicação: " + ", ".join(regra.cids),
                "cid:" + ",".join(regra.cids),
                "Qual o diagnóstico (CID) que justifica o pedido? Confirme a hipótese ou registre-o no prontuário.",
            )
        )
    if regra.idade_min is not None:
        out.append(Criterio("idade_min", f"Idade mínima de {regra.idade_min} anos", f"idade>={regra.idade_min}"))
    if regra.idade_max is not None:
        out.append(Criterio("idade_max", f"Idade máxima de {regra.idade_max} anos", f"idade<={regra.idade_max}"))
    return out + list(regra.criterios)


# ---------- gatilhos ----------
def detectar(base: Base, ctx: Ctx, drafts: list[OrderDraft]) -> list[Gatilho]:
    """Regras citadas: o profissional diz que vai pedir o item (fala afirmativa, sem pergunta, condição nem passado) ou
    há um rascunho de pedido dele (não recusado)."""
    out: dict[str, Gatilho] = {}
    for regra in base.regras.values():
        termos = list(regra.termos)
        refs: list[str] = []
        for s in ctx.segments:
            if s.falante not in FALANTES_GATILHO or s.marco or s.expurgada or s.falante_incerto:
                continue
            for sentence in _frases(s.texto):
                if PASSADO.search(sentence) or CONDICIONAL.search(sentence):
                    continue
                if _afirma(sentence, termos, plano_vale=True):
                    ref = fala_ref(s.n)
                    if ref not in refs:
                        refs.append(ref)
        if refs:
            out[regra.id] = Gatilho(regra, "fala", f"Pedido citado em {refs[0]}", refs[:3])
            continue
        for d in drafts:
            if d.decisao.estado == "recusado":
                continue
            if any(t in norm(d.item) for t in termos):
                out[regra.id] = Gatilho(regra, "pedido", f"Rascunho de pedido: {d.item}", list(d.refs[:3]))
                break
    return list(out.values())


# ---------- operadora e documentos ----------
def _operadora(base: Base, regra: Regra, informada: str, hoje: date):
    achada = achar_operadora(base, informada)
    nome = (informada or "").strip()
    if not nome:
        return (
            OperadoraAutorizacao(aviso="Operadora não informada: só as exigências da DUT foram conferidas."),
            [],
        )
    if achada is None:
        return (
            OperadoraAutorizacao(
                informada=nome,
                status="sem_regras",
                aviso="Operadora sem regras cadastradas: só as exigências da DUT foram conferidas. "
                "Confira com a operadora.",
            ),
            [],
        )
    linhas = [
        e
        for e in base.operadoras
        if e.operadora_id == achada[0] and e.procedimento in ("*", regra.id) and e.vigencia.vale(hoje)
    ]
    if not linhas:
        return (
            OperadoraAutorizacao(
                informada=nome,
                nome=achada[1],
                status="sem_regras",
                aviso="Exigências da operadora para este pedido estão fora de vigência ou ausentes: confira a tabela.",
            ),
            [],
        )
    linhas.sort(key=lambda e: e.procedimento == "*")  # a linha do procedimento antes da geral
    prazo = next((e.prazo_dias for e in linhas if e.prazo_dias is not None), None)
    return (
        OperadoraAutorizacao(
            informada=nome,
            nome=achada[1],
            status="com_regras",
            prazo_dias=prazo,
            justificativa_obrigatoria=any(e.justificativa_obrigatoria for e in linhas),
        ),
        linhas,
    )


def _documentos(regra: Regra, linhas: list, ctx: Ctx) -> list[DocumentoAutorizacao]:
    docs: list[DocumentoAutorizacao] = []
    vistos: set[str] = set()
    fontes = [(d, "dut") for d in regra.documentos] + [(d, "operadora") for e in linhas for d in e.documentos]
    for d, origem in fontes:
        if d.rotulo in vistos:
            continue
        vistos.add(d.rotulo)
        if d.tipo == "justificativa":
            docs.append(DocumentoAutorizacao(rotulo=d.rotulo, status="gerado", origem=origem))  # type: ignore[arg-type]
        elif d.tipo.startswith("exame:"):
            res = avaliar_regra(d.tipo, ctx, d.rotulo)
            docs.append(
                DocumentoAutorizacao(
                    rotulo=d.rotulo,
                    status="disponivel" if res.ok and res.refs else "faltando",
                    origem=origem,  # type: ignore[arg-type]
                    refs=res.refs if res.ok else [],
                )
            )
        else:
            docs.append(DocumentoAutorizacao(rotulo=d.rotulo, status="faltando", origem=origem))  # type: ignore[arg-type]
    return docs


def avaliar(base: Base, g: Gatilho, ctx: Ctx, operadora: str) -> Autorizacao:
    regra = g.regra
    fonte = RegraAutorizacao(
        id=regra.id,
        versao=regra.versao,
        fonte=regra.fonte,
        vigencia=regra.vigencia.texto(),
        origem=regra.origem,
        conferencia=regra.conferencia,
    )
    avisos = []
    if not regra.oficial:
        avisos.append("Regra de exemplo ou pendente de conferência humana: não é o texto oficial.")
    base_ = dict(
        id=regra.id,
        procedimento=regra.procedimento,
        tipo=regra.tipo,
        gatilho=g.tipo,
        gatilho_texto=g.texto,
        gatilho_refs=g.refs,
        regra=fonte,
    )
    if not regra.vigencia.vale(ctx.hoje):
        return Autorizacao(
            **base_,
            situacao="regra_vencida",
            avisos=[
                f"Regra fora de vigência ({regra.vigencia.texto()}): nenhuma checagem foi feita. "
                "Atualize a tabela do Rol/DUT antes de pedir a autorização.",
                *avisos,
            ],
        )  # type: ignore[arg-type]
    itens = [_item(regra, c, ctx)[0] for c in _criterios_da_regra(regra)]
    op, linhas = _operadora(base, regra, operadora, ctx.hoje)
    if op.aviso:
        avisos.append(op.aviso)
    docs = _documentos(regra, linhas, ctx)
    faltam = sum(i.status == "faltando" for i in itens)
    return Autorizacao(
        **base_,
        situacao="completa" if faltam == 0 else "incompleta",
        criterios=itens,
        documentos=docs,
        operadora=op,
        faltam=faltam,
        avisos=avisos,
    )  # type: ignore[arg-type]


def build(
    base: Base, chart: ChartView, segments: list[Segment], hyps: list[Hypothesis], drafts: list[OrderDraft], extra: dict
) -> list[Autorizacao]:
    """Autorizações em discussão na consulta (uma por regra citada), as incompletas primeiro."""
    if not base.regras:
        return []
    ctx = Ctx(chart, segments, hyps, extra, chart.now.date(), chart.r.id)
    operadora = (extra.get("autorizacao") or {}).get("operadora", "")
    out = [avaliar(base, g, ctx, operadora) for g in detectar(base, ctx, drafts)]
    out.sort(key=lambda a: (a.situacao == "completa", a.id))
    return out


def perguntas(auts: list[Autorizacao]) -> list[Question]:
    """O que ainda falta perguntar ou documentar, como perguntas da consulta (as mais urgentes primeiro)."""
    out: list[Question] = []
    for a in auts:
        if a.situacao != "incompleta":
            continue
        exemplo = (
            " (regra de exemplo: conferir)" if a.regra.origem == "exemplo" or a.regra.conferencia != "conferido" else ""
        )
        for i in a.criterios:
            if i.status != "faltando" or not i.pergunta:
                continue
            out.append(
                Question(
                    id=f"autorizacao:{a.id}:{i.id}",
                    pergunta=i.pergunta,
                    motivo=f"Autorização prévia de {a.procedimento}: {i.texto}{exemplo}.",
                    prioridade="media",
                    refs=a.gatilho_refs or [],
                    fonte=f"{a.regra.fonte} (v{a.regra.versao})",
                )
            )
    return out[:MAX_PERGUNTAS]


# ---------- pacote de justificativa ----------
def frases_regras(a: Autorizacao) -> list[dict]:
    """Frases da justificativa só com o que está atendido, cada uma com as refs que a sustentam."""
    frases: list[dict] = []
    if a.gatilho_refs:
        frases.append(
            {"texto": f"Solicito autorização prévia para {a.procedimento}.", "refs": list(a.gatilho_refs[:3])}
        )
    for i in a.criterios:
        if i.status != "atendido" or not i.refs:
            continue
        detalhe = ""
        if i.id == "cid" or i.id.startswith("idade"):
            detalhe = i.evidencia
        texto = i.texto if not detalhe else f"{i.texto}. {detalhe}"
        frases.append({"texto": texto.rstrip(".") + ".", "refs": list(i.refs)})
    return [{**f, "origem": "regra"} for f in frases[:MAX_FRASES]]


def problemas_frases(frases: list[dict], validas: set[str]) -> list[dict]:
    """Toda afirmação precisa citar evidência real desta consulta/paciente; devolve o que impede a aprovação."""
    out = []
    if not frases:
        out.append({"frase": 0, "problema": "a justificativa está vazia"})
    for n, f in enumerate(frases, start=1):
        texto = (f.get("texto") or "").strip()
        refs = f.get("refs") or []
        if not texto:
            out.append({"frase": n, "problema": "frase vazia"})
        elif len(texto) > MAX_FRASE:
            out.append({"frase": n, "problema": f"frase com mais de {MAX_FRASE} caracteres"})
        if not refs:
            out.append({"frase": n, "problema": "afirmação sem evidência (Fala/N ou recurso do prontuário)"})
        for r in refs:
            if r not in validas:
                out.append({"frase": n, "problema": f"a evidência '{r[:40]}' não pertence a esta consulta ou paciente"})
    return out


_NUM = re.compile(r"\d+(?:[.,]\d+)?")
_CID = re.compile(r"[A-Z]\d{2}(?:\.\d)?")
_PALAVRA = re.compile(r"[a-z]{4,}")
# palavras de redação que uma reescrita pode usar sem criar fato clínico; qualquer outra palavra de 4+ letras precisa
# existir (pelo radical de 5 letras) nas frases das regras, senão a reescrita é descartada
PALAVRAS_DE_REDACAO = frozenset(
    "paciente pacientes devido conforme portanto dessa desse nesta neste consulta registro registrado registrada "
    "documentado documentada relatado relatada informado informada apresenta apresentou encontra historia quadro "
    "clinico clinica indicacao justifica justificada necessario necessaria pedido solicito solicitamos solicitar "
    "autorizacao previa previo exame procedimento segundo desde entre sobre como para pela pelo tendo apos ainda "
    "tambem possui consta constam atualmente houve com sem anos meses dias".split()
)


def reescrita_segura(originais: list[dict], novas: list[dict]) -> list[dict] | None:
    """Aceita a reescrita da IA só se nenhuma frase ganhou ref de fora, número, CID, negação ou palavra clínica nova,
    nem a lista cresceu; senão None (a justificativa fica com as frases das regras). A IA só reescreve, não acrescenta
    fato: o texto da fala pode trazer instrução disfarçada de dado e a IA a obedecer, então o que muda de sentido
    não passa."""
    if not novas or len(novas) > min(MAX_FRASES, len(originais)):
        return None
    permitidas = {r for f in originais for r in f.get("refs", [])}
    corpus = " ".join(f["texto"] for f in originais)
    numeros = set(_NUM.findall(corpus))
    codigos = set(_CID.findall(corpus))
    base = norm(corpus)
    radicais = {w[:5] for w in _PALAVRA.findall(base)}
    negacoes = {w for w in base.split() if w in NEG_WORDS}
    out = []
    for f in novas:
        texto = (f.get("texto") or "").strip()
        refs = [r for r in (f.get("refs") or []) if r in permitidas]
        if not texto or len(texto) > MAX_FRASE or not refs or len(refs) != len(f.get("refs") or []):
            return None
        if set(_NUM.findall(texto)) - numeros or set(_CID.findall(texto)) - codigos:
            return None
        plano = norm(texto)
        if {w for w in plano.split() if w in NEG_WORDS} - negacoes:
            return None
        if any(w not in PALAVRAS_DE_REDACAO and w[:5] not in radicais for w in _PALAVRA.findall(plano)):
            return None
        out.append({"texto": texto, "refs": refs, "origem": "ia"})
    return out


def texto_justificativa(frases: list[dict]) -> str:
    return " ".join(f["texto"].strip() for f in frases)


def anexos(a: Autorizacao, extra: dict) -> list[dict]:
    """Exames do prontuário que sustentam critérios e laudos lidos (#48) desta consulta."""
    out: list[dict] = []
    vistos: set[str] = set()
    refs = [r for i in a.criterios if i.status == "atendido" for r in i.refs] + [
        r for d in a.documentos if d.status == "disponivel" for r in d.refs
    ]
    for r in refs:
        if r.startswith(("Fala/", "Patient/", "Condition/")) or r in vistos:
            continue
        vistos.add(r)
        out.append({"tipo": "exame", "ref": r, "rotulo": r})
    for laudo in extra.get("laudos") or []:
        ref = f"Documento/{laudo['n']}"
        out.append(
            {
                "tipo": "laudo",
                "ref": ref,
                "rotulo": f"Laudo {laudo['n']} ({laudo.get('tipo', 'documento')}"
                + (f", {laudo['data_laudo']}" if laudo.get("data_laudo") else "")
                + ")",
            }
        )
    return out


# ---------- saídas ----------
SISTEMA = "urn:housia:autorizacao-previa"


def _refs_fhir(pub: dict) -> list[str]:
    refs = [r for f in (pub["pacote"].get("frases") or []) for r in f.get("refs", [])]
    refs += [a["ref"] for a in pub["pacote"].get("anexos") or [] if a.get("tipo") == "exame"]
    return list(dict.fromkeys(r for r in refs if not r.startswith(("Fala/", "Documento/"))))


def fhir_bundle(pub: dict, patient_id: str, encounter_id: str | None, author: str = "") -> dict:
    """ServiceRequest + Claim (use=preauthorization) em RASCUNHO. Nada é enviado: é o que a pessoa leva à operadora."""
    from app import clock  # noqa: PLC0415

    pacote = pub["pacote"]
    frases = pacote.get("frases") or []
    quando = clock.now().isoformat()
    sr_id, claim_id = f"urn:uuid:sr-{pub['id']}", f"urn:uuid:claim-{pub['id']}"
    falas = [r for f in frases for r in f.get("refs", []) if r.startswith("Fala/")]
    sr = {
        "resourceType": "ServiceRequest",
        "identifier": [{"system": SISTEMA, "value": f"{pub['consulta']}/{pub['regra_id']}"}],
        "status": "draft",
        "intent": "proposal",
        "code": {"text": pub["procedimento"]},
        "subject": {"reference": f"Patient/{patient_id}"},
        "authoredOn": quando,
        "note": [{"text": texto_justificativa(frases)}],
        "supportingInfo": [{"reference": r} for r in _refs_fhir(pub)],
        "extension": [
            {"url": "urn:housia:fala-refs", "valueString": ", ".join(dict.fromkeys(falas))},
            {"url": "urn:housia:regra", "valueString": f"{pub['regra_id']} v{pub['regra_versao']}"},
        ],
    }
    if encounter_id:
        sr["encounter"] = {"reference": f"Encounter/{encounter_id}"}
    if author:
        sr["requester"] = {"display": author}
    claim = {
        "resourceType": "Claim",
        "status": "draft",
        "type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/claim-type", "code": "professional"}]},
        "use": "preauthorization",
        "patient": {"reference": f"Patient/{patient_id}"},
        "created": quando,
        "provider": {"display": author or "Profissional solicitante"},
        "priority": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/processpriority", "code": "normal"}]},
        "referral": {"reference": sr_id},
        "insurance": [
            {"sequence": 1, "focal": True, "coverage": {"display": pub["operadora"] or "Operadora não informada"}}
        ],
        "item": [{"sequence": 1, "productOrService": {"text": pub["procedimento"]}}],
    }
    return {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [{"fullUrl": sr_id, "resource": sr}, {"fullUrl": claim_id, "resource": claim}],
    }


def texto_pdf(pub: dict, nome: str, idade: int | None, aviso: str = "") -> str:
    pacote = pub["pacote"]
    regra = pacote.get("regra") or {}
    marca = {"atendido": "[x] Atendido", "faltando": "[ ] Faltando", "nao_aplicavel": "[-] Não se aplica"}
    linhas = []
    if aviso:
        linhas += [f"ATENÇÃO: {aviso}", ""]
    linhas += [
        f"Paciente: {nome}" + (f", {idade} anos" if idade is not None else ""),
        f"Operadora: {pub['operadora'] or 'não informada'}",
        f"Pedido: {pub['procedimento']}",
        f"Regra: {regra.get('id', '')} versão {regra.get('versao', '')}, vigência {regra.get('vigencia', '')}",
        f"Fonte da regra: {regra.get('fonte', '')}",
    ]
    if pacote.get("prazo_dias") is not None:
        linhas.append(f"Prazo de resposta informado pela operadora: {pacote['prazo_dias']} dias (conferir).")
    linhas += ["", "JUSTIFICATIVA CLÍNICA"]
    for n, f in enumerate(pacote.get("frases") or [], start=1):
        linhas.append(f"{n}. {f['texto']} [{', '.join(f.get('refs') or [])}]")
    linhas += ["", "CRITÉRIOS CONFERIDOS (DUT e operadora)"]
    for i in pacote.get("checklist") or []:
        linhas.append(f"{marca.get(i['status'], i['status'])}: {i['texto']}")
        if i.get("evidencia") and i["status"] == "atendido":
            linhas.append(f"    Evidência: {i['evidencia']} [{', '.join(i.get('refs') or [])}]")
    if pacote.get("pendencias"):
        linhas += ["", "PENDÊNCIAS (não afirmadas na justificativa)"]
        linhas += [f"- {p['texto']}" for p in pacote["pendencias"]]
    linhas += ["", "DOCUMENTOS E ANEXOS"]
    for d in pacote.get("documentos") or []:
        linhas.append(f"- {d['rotulo']}: {d['status'].replace('_', ' ')}")
    for a in pacote.get("anexos") or []:
        linhas.append(f"- Anexo: {a['rotulo']}")
    linhas += [
        "",
        f"Revisado e aprovado por {pub['revisado_por'] or 'médico'} em "
        f"{(pub['revisado_em'] or '')[:16].replace('T', ' ')}.",
    ]
    return "\n".join(linhas)
