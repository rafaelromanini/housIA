"""Smart Apps que leem o prontuário (FHIR): histórico, exames em linguagem simples, medicamentos, alergias,
protocolos de acompanhamento e cirurgia. Determinístico: roda sempre, com ou sem LLM.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.consult import knowledge
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
from app.reference import drugs, matches
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

    @property
    def conditions(self) -> list[tuple[str, str, dict]]:
        """(cid, texto, recurso) dos diagnósticos ativos."""
        out = []
        for c in self.r.active_conditions:
            icd = coding(c.get("code"), codes.ICD10) or {}
            out.append((icd.get("code", ""), c.get("code", {}).get("text") or icd.get("display", ""), c))
        return out

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
    def meds(self, reported: list[tuple[str, str, str]]) -> list[MedItem]:
        """`reported`: (princípio ativo ou @classe, termo dito, ref da fala)."""
        out = [
            MedItem(
                medicamento=m.name,
                posologia=m.text,
                desde=fmt_date(m.start),
                status="em_uso",
                observacao=self._replaced(m),
                refs=[m.ref],
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
        return out

    def _replaced(self, m: MedInfo) -> str | None:
        if not m.prior_ref:
            return None
        prior = next((x for x in self.r.medications if x.ref == m.prior_ref), None)
        return f"Substituiu {prior.text}" if prior else "Substituiu prescrição anterior"

    # ---------- Alergias ----------
    def chart_allergies(self) -> list[AllergyInfo]:
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
                    conflitos=self.allergy_conflicts(key),
                    refs=[a.source_ref],
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
    def protocols(self) -> list[ProtocolItem]:
        out = []
        for p in knowledge.protocols():
            cond = self.has_condition(p.condition)
            if cond is None:
                continue
            refs = [f"Condition/{cond['id']}"]
            kind, _, spec = p.source.partition(":")
            if kind == "lab":
                last = self.latest_lab(spec, max_days=3650)
                if last is None:
                    status, ultimo = "sem_registro", None
                else:
                    age = (self.now - last.timestamp).days
                    status = "em_dia" if age <= p.interval_days else "pendente"
                    ultimo = f"{fmt_date(last.timestamp)}: {last.summary.split(': ', 1)[-1]}"
                    refs.append(last.source_ref)
            elif kind == "texto":
                words = tuple(w for w in spec.split("|") if w)
                hits = [e for e in self.notes_text(3650) if any(w in norm(e.text or "") for w in words)]
                hits += [e for e in self.events if e.kind == "procedure" and any(w in norm(e.summary) for w in words)]
                hits.sort(key=lambda e: e.timestamp)
                if not hits:
                    status, ultimo = "sem_registro", None
                else:
                    last = hits[-1]
                    status = "em_dia" if (self.now - last.timestamp).days <= p.interval_days else "pendente"
                    ultimo = f"{fmt_date(last.timestamp)} ({'evolução' if last.kind == 'note' else 'procedimento'})"
                    refs.append(last.source_ref)
            else:  # vital:pa:140/90
                target_s, _, target_d = spec.partition(":")[2].partition("/")
                pa = [e for e in self.events if e.kind == "vital" and (e.value or {}).get("chave") == "pa"]
                if not pa:
                    status, ultimo = "sem_registro", None
                else:
                    last = pa[-1]
                    ok = last.value["sistolica"] < float(target_s) and last.value["diastolica"] < float(target_d)
                    status = "no_alvo" if ok else "fora_do_alvo"
                    ultimo = f"{fmt_date(last.timestamp)}: {last.value['sistolica']}/{last.value['diastolica']} mmHg"
                    refs.append(last.source_ref)
            out.append(
                ProtocolItem(
                    protocolo=p.protocol, item=p.item, status=status, ultimo=ultimo, orientacao=p.guidance, refs=refs
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
