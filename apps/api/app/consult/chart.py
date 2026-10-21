"""Smart Apps que leem o prontuário (FHIR): histórico, exames em linguagem simples, medicamentos, alergias,
protocolos de acompanhamento e cirurgia. Determinístico: roda sempre, com ou sem LLM.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.consult import knowledge, protocolos, scales
from app.consult.schemas import (
    AllergyInfo,
    ExamInfo,
    HistoryItem,
    MedItem,
    ProtocolItem,
    SurgeryItem,
)
from app.consult.text import norm
from app.fhir_client import codes
from app.reference import drugs, matches, norm_icd
from app.timeline.context import PatientContext
from app.timeline.events import ClinicalEvent
from app.timeline.record import MedInfo, PatientRecord, coding, parse_dt


def tz() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


def fmt_date(dt: datetime | None) -> str | None:
    return dt.astimezone(tz()).strftime("%d/%m/%Y") if dt else None


def fmt_num(value: float, decimals: int) -> str:
    return f"{value:.{decimals}f}".replace(".", ",")


def ago(dt: datetime | None, now: datetime) -> str:
    if dt is None:
        return ""
    days = (now - dt).days
    if days >= 730:
        return f"há {days // 365} anos"
    if days >= 365:
        return "há 1 ano"
    if days >= 60:
        return f"há {days // 30} meses"
    if days >= 2:
        return f"há {days} dias"
    return "hoje" if days == 0 else "ontem"


def egfr_ckd_epi_2021(creatinine: float, age: int, female: bool) -> float:
    """TFG estimada (mL/min/1,73 m²) pela equação CKD-EPI 2021, sem coeficiente racial."""
    k, a = (0.7, -0.241) if female else (0.9, -0.302)
    ratio = creatinine / k
    value = 142 * min(ratio, 1) ** a * max(ratio, 1) ** -1.200 * 0.9938**age
    return round(value * (1.012 if female else 1.0), 1)


class ChartView:
    """Leitura do prontuário pronta para os Smart Apps (cálculos uma vez por análise)."""

    def __init__(self, record: PatientRecord, events: list[ClinicalEvent], ctx: PatientContext, now: datetime):
        self.r = record
        self.events = events
        self.ctx = ctx
        self.now = now
        self.labs: dict[str, list[ClinicalEvent]] = {}
        self.vitals: dict[str, list[ClinicalEvent]] = {}
        for e in events:
            key = (e.value or {}).get("chave")
            if not key:
                continue
            if e.kind == "lab":
                self.labs.setdefault(key, []).append(e)
            elif e.kind == "vital":
                self.vitals.setdefault(key, []).append(e)
        for group in (*self.labs.values(), *self.vitals.values()):
            group.sort(key=lambda e: e.timestamp)

    # ---------- utilidades ----------
    def latest_lab(self, key: str, max_days: int = 400) -> ClinicalEvent | None:
        items = [e for e in self.labs.get(key, []) if (self.now - e.timestamp).days <= max_days]
        return items[-1] if items else None

    def latest_vital(self, key: str, max_hours: int = 48) -> ClinicalEvent | None:
        items = [e for e in self.vitals.get(key, []) if self.now - e.timestamp <= timedelta(hours=max_hours)]
        return items[-1] if items else None

    def weight_event(self) -> ClinicalEvent | None:
        """Último peso utilizável (até 1 ano): ignora valor zerado, absurdo (fora de 0,3 a 400 kg) ou em unidade
        desconhecida, que lidos como kg dariam dose errada; sem peso utilizável, a conferência por kg pede pesagem."""
        items = [
            e
            for e in self.vitals.get("peso", [])
            if (self.now - e.timestamp).days <= 365
            and not e.value.get("unidade_invalida")
            and 0.3 <= float(e.value["valor"]) <= 400
        ]
        return items[-1] if items else None

    def weight(self) -> tuple[float, str] | None:
        """Último peso registrado (kg) e a fonte, para conferir doses por peso."""
        e = self.weight_event()
        return (float(e.value["valor"]), e.source_ref) if e else None

    @property
    def conditions(self) -> list[tuple[str, str, dict]]:
        """(cid, texto, recurso) dos diagnósticos ativos."""
        out = []
        for c in self.r.active_conditions:
            icd = coding(c.get("code"), codes.ICD10) or {}
            out.append((icd.get("code", ""), c.get("code", {}).get("text") or icd.get("display", ""), c))
        return out

    @property
    def icd_codes(self) -> frozenset[str]:
        """CID dos diagnósticos que o profissional já registrou (aceitos mesmo fora da tabela oficial do DATASUS)."""
        return frozenset(
            c for c in (norm_icd(icd) for icd, _, _ in self.conditions) if c
        )  # sem "" de código malformado

    def has_condition(self, prefix: str) -> dict | None:
        for icd, _, c in self.conditions:
            if icd.startswith(prefix):
                return c
        return None

    @property
    def female(self) -> bool:
        return self.r.sex == "F"

    @property
    def egfr(self) -> tuple[float, ClinicalEvent] | None:
        cr = self.latest_lab("creatinina")
        if cr is None or self.r.age is None:
            return None
        return egfr_ckd_epi_2021(float(cr.value["valor"]), self.r.age, self.female), cr

    @property
    def active_meds(self) -> list[MedInfo]:
        return [m for m in self.r.active_meds]

    def notes_text(self, days: int = 400) -> list[ClinicalEvent]:
        return [e for e in self.events if e.kind == "note" and e.text and (self.now - e.timestamp).days <= days]

    # ---------- Histórico médico ----------
    def history(self) -> list[HistoryItem]:
        items: list[HistoryItem] = []
        for icd, text, c in self.conditions:
            onset = parse_dt(c.get("onsetDateTime"))
            when = fmt_date(onset)
            label = f"{text} ({icd})" if icd else text
            extra = f" — desde {when} ({ago(onset, self.now)})" if onset else ""
            items.append(
                HistoryItem(texto=f"{label}{extra}", quando=when, tipo="diagnostico", refs=[f"Condition/{c['id']}"])
            )
        for p in self.r.resources.get("Procedure", []):
            when = parse_dt(p.get("performedDateTime"))
            if "cirurg" not in norm((p.get("category") or {}).get("text", "")):
                continue  # acessos, sondas e afins ficam na timeline, não no histórico
            items.append(
                HistoryItem(
                    texto=f"{p.get('code', {}).get('text', 'Procedimento')} — {fmt_date(when)} ({ago(when, self.now)})",
                    quando=fmt_date(when),
                    tipo="cirurgia",
                    refs=[f"Procedure/{p['id']}"],
                )
            )
        for e in self.r.encounters.values():
            cls = e.get("class", {}).get("code")
            start = parse_dt(e.get("period", {}).get("start"))
            reason = ((e.get("reasonCode") or [{}])[0]).get("text", "")
            if cls == "EMER" and start:
                items.append(
                    HistoryItem(
                        texto=f"Pronto-socorro: {reason} — {fmt_date(start)} ({ago(start, self.now)})",
                        quando=fmt_date(start),
                        tipo="pronto_socorro",
                        refs=[f"Encounter/{e['id']}"],
                    )
                )
            elif cls == "AMB" and e.get("status") == "finished" and start:
                items.append(
                    HistoryItem(
                        texto=f"Consulta: {reason} — {fmt_date(start)} ({ago(start, self.now)})",
                        quando=fmt_date(start),
                        tipo="consulta",
                        refs=[f"Encounter/{e['id']}"],
                    )
                )
        order = {"diagnostico": 0, "cirurgia": 1, "internacao": 2, "pronto_socorro": 3, "consulta": 4, "relatado": 5}
        items.sort(key=lambda i: order.get(i.tipo, 9))
        return items

    # ---------- Exames em linguagem simples ----------
    def exams(self) -> list[ExamInfo]:
        lay = knowledge.lab_lay()
        diabetic = self.has_condition("E11") is not None or self.has_condition("E10") is not None
        out: list[ExamInfo] = []
        egfr = self.egfr
        for key, info in lay.items():
            last = self.latest_lab(key)
            if last is None:
                continue
            measure = codes.LABS[key]
            value = float(last.value["valor"])
            high = 7.0 if key == "hba1c" and diabetic else measure.high
            if key == "ldl" and diabetic:
                high = 100
            status = "normal"
            if measure.low is not None and value < measure.low:
                status = "baixo"
            elif high is not None and value > high:
                status = "alto"
            text_status = info.get(status) or info["normal"]
            value_txt = f"{fmt_num(value, measure.decimals)} {measure.unit}".strip()
            explanation = f"{measure.label} {info['o_que_e']}. O seu resultado ({value_txt}) {text_status}."
            if key == "creatinina" and egfr:
                explanation += (
                    f" Pela idade e sexo, isso corresponde a uma filtração dos rins de cerca de {round(egfr[0])} "
                    f"mL/min (o esperado é acima de 90; abaixo de 60 indica doença renal crônica)."
                )
            history = [e for e in self.labs.get(key, []) if e.timestamp < last.timestamp]
            trend = None
            refs = [last.source_ref]
            if history:
                prev = float(history[-1].value["valor"])
                prev_txt = fmt_num(prev, measure.decimals)
                if abs(value - prev) < max(0.05 * prev, 10**-measure.decimals):
                    trend = f"estável em relação a {fmt_date(history[-1].timestamp)} ({prev_txt})"
                else:
                    trend = f"{'subiu' if value > prev else 'caiu'} de {prev_txt} ({fmt_date(history[-1].timestamp)})"
                refs.append(history[-1].source_ref)
            out.append(
                ExamInfo(
                    exame=measure.label,
                    chave=key,
                    valor=value_txt,
                    data=fmt_date(last.timestamp) or "",
                    status=status,
                    explicacao=explanation,
                    tendencia=trend,
                    refs=refs,
                )
            )
        glic = self.latest_vital("glicemia", 24)
        if glic is not None:
            v = float(glic.value["valor"])
            status = "baixo" if v < 70 else ("alto" if v > 180 else "normal")
            text = {
                "baixo": "está baixa (hipoglicemia): pode causar tremor, suor frio, tontura e confusão",
                "alto": "está alta",
                "normal": "está dentro do esperado",
            }[status]
            out.insert(
                0,
                ExamInfo(
                    exame="Glicemia capilar (ponta do dedo)",
                    chave="glicemia",
                    valor=f"{fmt_num(v, 0)} mg/dL",
                    data=fmt_date(glic.timestamp) or "",
                    status=status,
                    explicacao=f"Medida do açúcar no sangue feita hoje: {fmt_num(v, 0)} mg/dL — {text}.",
                    refs=[glic.source_ref],
                ),
            )
        out.sort(key=lambda x: x.status == "normal")
        return out

    # ---------- Medicamentos ----------
    def meds(
        self,
        reported: list[tuple[str, str, str]],
        suspended: list[tuple[str, str, str]] | None = None,
        planned: list[tuple[str, str, str]] | None = None,
    ) -> list[MedItem]:
        """`reported`/`suspended`/`planned`: (princípio ativo ou @classe, termo dito, ref da fala) do que o paciente
        diz usar / diz ter parado / do que o profissional diz que vai prescrever."""
        stopped = {ing: ref for ing, _, ref in suspended or []}
        out = [
            MedItem(
                medicamento=m.name,
                posologia=m.text,
                desde=fmt_date(m.start),
                status="em_uso",
                observacao="Paciente relata que parou de tomar — confirmar adesão."
                if m.ingredient in stopped
                else self._replaced(m),
                refs=[m.ref] + ([stopped[m.ingredient]] if m.ingredient in stopped else []),
            )
            for m in self.active_meds
        ]
        for m in self.r.medications:
            if m.status == "stopped" and m.end and (self.now - m.end).days <= 365:
                out.append(
                    MedItem(
                        medicamento=m.name,
                        posologia=m.text,
                        desde=fmt_date(m.start),
                        status="suspenso",
                        observacao=f"Suspenso em {fmt_date(m.end)}: {m.reason or 'motivo não registrado'}",
                        refs=[m.ref],
                    )
                )
        for m in self.r.proposed_meds:
            out.append(
                MedItem(
                    medicamento=m.name,
                    posologia=m.text,
                    desde=fmt_date(m.authored),
                    status="proposto",
                    observacao="Prescrição proposta na consulta (rascunho assinado): ainda não emitida.",
                    refs=[m.ref],
                )
            )
        active_ings = {m.ingredient for m in self.active_meds}
        seen: set[str] = set()
        for ing, term, ref in reported:
            if ing in active_ings or ing in seen:
                continue
            seen.add(ing)
            name = drugs()[ing].name if ing in drugs() else term
            out.append(
                MedItem(
                    medicamento=name,
                    posologia="Relatado na consulta (sem prescrição no prontuário)",
                    status="relatado",
                    observacao="Uso por conta própria — confirmar dose e frequência e registrar.",
                    refs=[ref],
                )
            )
        for ing, term, ref in suspended or []:
            if ing in active_ings or ing in seen:
                continue
            seen.add(ing)
            out.append(
                MedItem(
                    medicamento=drugs()[ing].name if ing in drugs() else term,
                    posologia="Relatado na consulta (sem prescrição no prontuário)",
                    status="suspenso",
                    observacao="Paciente relata que parou — registrar no histórico de medicamentos.",
                    refs=[ref],
                )
            )
        for ing, term, ref in planned or []:
            if ing in seen:
                continue
            seen.add(ing)
            out.append(
                MedItem(
                    medicamento=drugs()[ing].name if ing in drugs() else term.capitalize(),
                    posologia="Dito pelo profissional como prescrição a fazer (ainda não está no prontuário)",
                    status="planejado",
                    observacao="Conferir alergia, dose e interações antes de prescrever; registrar a dose.",
                    refs=[ref],
                )
            )
        return out

    def _replaced(self, m: MedInfo) -> str | None:
        if not m.prior_ref:
            return None
        prior = next((x for x in self.r.medications if x.ref == m.prior_ref), None)
        return f"Substituiu {prior.text}" if prior else "Substituiu prescrição anterior"

    # ---------- Alergias ----------
    def chart_allergies(self, reported: list[tuple[str, str]] | None = None) -> list[AllergyInfo]:
        """Alergias do prontuário. `reported`: (princípio ativo, termo dito) do que o paciente diz tomar, cruzado com
        cada alergia para apontar o conflito."""
        defs = knowledge.allergy_defs()
        out = []
        for a in self.ctx.alergias:
            detail = a.detail or {}
            code = detail.get("codigo") or ""
            key = f"@{code}" if detail.get("sistema") == codes.DRUG_CLASS else code
            d = next((x for x in defs if x.allergen == key), None)
            name, _, reaction = a.summary.partition(" — ")
            out.append(
                AllergyInfo(
                    alergeno=name,
                    reacao=reaction,
                    fonte="prontuario",
                    reatividade_cruzada=d.cross if d else "Sem dados de reatividade cruzada na base de demonstração.",
                    evitar=d.avoid if d else name,
                    alternativas=d.alternatives if d else "—",
                    dicas=d.tips if d else "Informe a alergia em toda consulta e atendimento.",
                    conflitos=self.allergy_conflicts(key, reported),
                    refs=[a.source_ref],
                    chave=key,
                    criticidade={"high": "alta", "low": "baixa"}.get(detail.get("criticidade") or "", "indeterminada"),
                    fonte_regra=d.source if d else "",
                )
            )
        return out

    def allergy_conflicts(self, allergen: str, extra: list[tuple[str, str]] | None = None) -> list[str]:
        """Medicamentos em uso (ou relatados) que batem com o alérgeno (princípio ou @classe)."""
        out = []
        catalog = drugs()
        for m in self.active_meds:
            if allergen and matches(allergen, m.ingredient, m.drug_class):
                out.append(f"{m.name} em uso ({m.ref})")
        for ing, label in extra or []:
            d = catalog.get(ing)
            if d and matches(allergen, d.ingredient, d.drug_class):
                out.append(f"{label} relatado na consulta")
        return out

    # ---------- Protocolos de acompanhamento (APS) ----------
    def _protocol_applies(self, p: knowledge.ProtocolDef, segments, trimester: int | None, risks: dict) -> dict | None:
        """O item vale para este paciente? Idade e sexo sempre valem; condição ou tema dito na conversa valem como
        "ou". Devolve {"refs": [...]} (o que motivou o item) ou None."""
        age = self.r.age
        if p.sex and self.r.sex != p.sex:
            return None
        if age is not None and (
            (p.age_min is not None and age < p.age_min) or (p.age_max is not None and age > p.age_max)
        ):
            return None
        if p.months_min is not None or p.months_max is not None:
            months = protocolos.meses_de_vida(self.r.patient.get("birthDate"), self.now.date())
            if months is None or (p.months_min is not None and months < p.months_min):
                return None
            if p.months_max is not None and months > p.months_max:
                return None
        if p.trimester is not None and trimester is not None and p.trimester != trimester:
            return None
        if not p.conditions and not p.if_said:
            return {"refs": []}
        if p.id in risks:
            return {"refs": []}
        for prefix in p.conditions:
            cond = self.has_condition(prefix)
            if cond is not None:
                return {"refs": [f"Condition/{cond['id']}"]}
        if p.if_said and protocolos.falas_com(segments, p.if_said):
            return {"refs": []}
        return None

    def _protocol_record(self, p: knowledge.ProtocolDef) -> tuple[str, str | None, list[str]]:
        """(status, último, refs) do item pelo que o prontuário prova; `interval_days` 0 = vale uma vez."""
        limit = p.interval_days or 36500
        kind, _, spec = p.source.partition(":")
        if kind == "lab":
            last = self.latest_lab(spec, max_days=3650)
            if last is None:
                return "sem_registro", None, []
            age = (self.now - last.timestamp).days
            return (
                "em_dia" if age <= limit else "pendente",
                f"{fmt_date(last.timestamp)}: {last.summary.split(': ', 1)[-1]}",
                [last.source_ref],
            )
        if kind == "texto":
            words = tuple(w for w in spec.split("|") if w)
            hits = [e for e in self.notes_text(3650) if any(w in norm(e.text or "") for w in words)]
            hits += [e for e in self.events if e.kind == "procedure" and any(w in norm(e.summary) for w in words)]
            hits.sort(key=lambda e: e.timestamp)
            if not hits:
                return "sem_registro", None, []
            last = hits[-1]
            return (
                "em_dia" if (self.now - last.timestamp).days <= limit else "pendente",
                f"{fmt_date(last.timestamp)} ({'evolução' if last.kind == 'note' else 'procedimento'})",
                [last.source_ref],
            )
        if kind == "vacina":
            words = tuple(w for w in spec.split("|") if w)
            doses = []
            for imm in self.r.resources.get("Immunization", []):
                code = imm.get("vaccineCode") or {}
                label = norm(" ".join([code.get("text", ""), *(c.get("display", "") for c in code.get("coding", []))]))
                when = parse_dt(imm.get("occurrenceDateTime"))
                if imm.get("status") != "not-done" and when and any(w in label for w in words):
                    doses.append((when, f"Immunization/{imm['id']}"))
            if not doses:
                return "sem_registro", None, []
            when, ref = max(doses)
            return "em_dia" if (self.now - when).days <= limit else "pendente", f"{fmt_date(when)} (vacina)", [ref]
        if kind == "vital":  # vital:pa:140/90
            target_s, _, target_d = spec.partition(":")[2].partition("/")
            pa = [e for e in self.events if e.kind == "vital" and (e.value or {}).get("chave") == "pa"]
            if not pa:
                return "sem_registro", None, []
            last = pa[-1]
            ok = last.value["sistolica"] < float(target_s) and last.value["diastolica"] < float(target_d)
            return (
                "no_alvo" if ok else "fora_do_alvo",
                f"{fmt_date(last.timestamp)}: {last.value['sistolica']}/{last.value['diastolica']} mmHg",
                [last.source_ref],
            )
        return "sem_registro", None, []  # escala:*, nenhum: só a consulta de hoje prova o item

    def protocols(self, segments=(), escalas: dict | None = None) -> list[ProtocolItem]:
        """Itens dos protocolos de APS que valem para o paciente (diagnóstico, idade, sexo ou tema da conversa).

        Com as falas da consulta, o item tratado vira "abordado — Fala/N" (se ainda estava pendente) e o gatilho de
        risco dito (ou o item 9 do PHQ-9 positivo) vira "critico". Sem falas, é só o prontuário."""
        segments = list(segments)
        escalas = escalas or {}
        trimester = protocolos.trimestre_pela_fala(segments)
        risks = protocolos.criticos(segments)
        out = []
        for p in knowledge.protocols():
            motive = self._protocol_applies(p, segments, trimester, risks)
            if motive is None:
                continue
            status, ultimo, refs = self._protocol_record(p)
            refs = motive["refs"] + refs
            kind, _, spec = p.source.partition(":")
            if kind == "escala" and spec in escalas:
                answers = {k: str(v) for k, v in (escalas[spec].get("respostas") or {}).items()}
                res = scales.calcular(spec, answers)
                if res.completo or answers:
                    status = "abordado"
                    ultimo = f"abordado — {res.nome} respondido na consulta" + (
                        f" ({res.total} pontos)" if res.total is not None else ""
                    )
                    refs = refs or [f"Patient/{self.r.id}"]
                if p.critical and spec == "phq9" and answers.get("ideacao") not in (None, "0"):
                    status, ultimo = "critico", "item 9 do PHQ-9 positivo na consulta"
                    refs = refs or [f"Patient/{self.r.id}"]
            talked = protocolos.falas_com(segments, p.said) if p.said else []
            if p.id in risks:
                status = "critico"
                refs = protocolos.refs_das_falas(risks[p.id]) + refs
                ultimo = "risco dito na consulta — " + ", ".join(protocolos.refs_das_falas(risks[p.id]))
            elif talked and status in ("pendente", "sem_registro"):
                status = "abordado"
                said_refs = protocolos.refs_das_falas(talked)
                ultimo = "abordado — " + ", ".join(said_refs)
                refs = said_refs + refs
            out.append(
                ProtocolItem(
                    protocolo=p.protocol,
                    item=p.item,
                    status=status,
                    ultimo=ultimo,
                    orientacao=p.guidance,
                    refs=refs,
                    id=p.id,
                    eixo=p.axis,
                    fonte=p.citation,
                    intervalo_dias=p.interval_days,
                    local=p.local,
                    pergunta=p.question,
                )
            )
        return out

    # ---------- Cirurgia ----------
    def surgical_history(self) -> list[SurgeryItem]:
        out = []
        for p in self.r.resources.get("Procedure", []):
            if "cirurg" not in norm((p.get("category") or {}).get("text", "")):
                continue
            when = parse_dt(p.get("performedDateTime"))
            out.append(
                SurgeryItem(
                    texto=f"{p.get('code', {}).get('text', 'Cirurgia')} — {fmt_date(when)} ({ago(when, self.now)})",
                    tipo="historico",
                    refs=[f"Procedure/{p['id']}"],
                )
            )
        for e in self.notes_text(365):
            text = norm(e.text or "")
            if "cirurgia a programar" in text or "cirurgia programada" in text:
                sentence = next(
                    (s.strip() for s in (e.text or "").split(".") if "cirurgia" in norm(s)),
                    "Cirurgia a programar",
                )
                out.append(
                    SurgeryItem(texto=f"Planejada no prontuário: {sentence}.", tipo="planejada", refs=[e.source_ref])
                )
        return out

    def perioperative_checks(self) -> list[SurgeryItem]:
        out: list[SurgeryItem] = []
        anticoag = [m for m in self.active_meds if m.drug_class in ("anticoagulante_oral", "antiagregante", "heparina")]
        if anticoag:
            out.append(
                SurgeryItem(
                    texto="Em uso de "
                    + ", ".join(m.name for m in anticoag)
                    + ": combinar suspensão/ponte com cirurgião e anestesista.",
                    tipo="checagem",
                    refs=[m.ref for m in anticoag],
                )
            )
        else:
            out.append(
                SurgeryItem(
                    texto="Sem anticoagulante ou antiagregante na prescrição ativa.",
                    tipo="checagem",
                    refs=[m.ref for m in self.active_meds][:3],
                )
            )
        hypo = [m for m in self.active_meds if m.drug_class in ("sulfonilureia", "insulina_basal", "insulina_rapida")]
        if hypo:
            out.append(
                SurgeryItem(
                    texto="Hipoglicemiante com risco no jejum ("
                    + ", ".join(m.name for m in hypo)
                    + "): orientar ajuste no dia do procedimento com o anestesista.",
                    tipo="checagem",
                    refs=[m.ref for m in hypo],
                )
            )
        a1c = self.latest_lab("hba1c")
        if a1c is not None:
            out.append(
                SurgeryItem(
                    texto=f"Controle glicêmico recente: {a1c.summary.split(' (')[0]} em {fmt_date(a1c.timestamp)}.",
                    tipo="checagem",
                    refs=[a1c.source_ref],
                )
            )
        for a in self.ctx.alergias:
            out.append(
                SurgeryItem(
                    texto=f"Informar à equipe cirúrgica: alergia a {a.summary.split(' — ')[0]}.",
                    tipo="checagem",
                    refs=[a.source_ref],
                )
            )
        egfr = self.egfr
        if egfr and egfr[0] < 60:
            out.append(
                SurgeryItem(
                    texto=f"Função renal reduzida (TFG ~{round(egfr[0])} mL/min/1,73 m²): atenção a nefrotóxicos, "
                    "contraste e doses.",
                    tipo="checagem",
                    refs=[egfr[1].source_ref],
                )
            )
        pa = [e for e in self.events if e.kind == "vital" and (e.value or {}).get("chave") == "pa"]
        if pa:
            last = pa[-1]
            ok = last.value["sistolica"] < 160 and last.value["diastolica"] < 100
            out.append(
                SurgeryItem(
                    texto=f"Última PA {last.value['sistolica']}/{last.value['diastolica']} mmHg "
                    + ("— adequada para cirurgia eletiva." if ok else "— controlar antes de cirurgia eletiva."),
                    tipo="checagem",
                    refs=[last.source_ref],
                )
            )
        return out
