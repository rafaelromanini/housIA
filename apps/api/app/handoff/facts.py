"""Pré-processamento determinístico da passagem de plantão ("fatos duros").

Roda antes do LLM e não depende dele: sinais vitais fora da faixa e tendência, diurese, próximos horários
de medicação, mudanças de prescrição, escalas (queda/lesão por pressão), acessos/dispositivos e exames.
"""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.fhir_client import codes
from app.handoff.schemas import Fact
from app.medrec.rules import default_transition, run_rules
from app.reference import high_alert_reason
from app.timeline.events import ClinicalEvent
from app.timeline.record import MedInfo, PatientRecord

TREND_KEYS = {
    # chave: (rótulo, delta mínimo nas 3 medidas, direção perigosa)
    "pas": ("PA sistólica", 15, "sobe"),
    "fc": ("Frequência cardíaca", 15, "sobe"),
    "fr": ("Frequência respiratória", 4, "sobe"),
    "spo2": ("SpO2", 3, "desce"),
    "temp": ("Temperatura", 0.8, "sobe"),
    "glicemia": ("Glicemia capilar", 60, "sobe"),
}
ORDER = {"normal": 0, "info": 0, "atencao": 1, "critico": 2}


def _tz() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


def hhmm(dt: datetime) -> str:
    return dt.astimezone(_tz()).strftime("%H:%M")


def _hours(delta: timedelta) -> int:
    return int(delta.total_seconds() // 3600)


def _fmt(value: float, key: str) -> str:
    measure = codes.ALL_MEASURES.get(key)
    return codes.fmt_number(value, measure.decimals if measure else 0)


class FactBuilder:
    def __init__(self, record: PatientRecord, events: list[ClinicalEvent], now: datetime, window_hours: int):
        self.r = record
        self.events = events
        self.now = now
        self.window_start = now - timedelta(hours=window_hours)
        self.facts: list[Fact] = []

    def build(self) -> list[Fact]:
        self._transfers()
        self._vitals()
        self._trends()
        self._labs()
        self._urine()
        self._scales()
        self._devices()
        self._prescription_changes()
        self._next_doses()
        self._medication_safety()
        self._pending_exams()
        self.facts.sort(key=lambda f: -ORDER[f.severidade])
        return self.facts

    # ---------- utilidades ----------
    def _series(self, key: str, hours: int = 24) -> list[tuple[datetime, float, str]]:
        """(horário, valor, ref) de uma medida; 'pas'/'pad' vêm do painel de PA."""
        since = self.now - timedelta(hours=hours)
        out = []
        for e in self.events:
            if e.timestamp < since or e.timestamp > self.now or not e.value:
                continue
            if key in ("pas", "pad") and e.value.get("chave") == "pa":
                out.append((e.timestamp, float(e.value["sistolica" if key == "pas" else "diastolica"]), e.source_ref))
            elif e.value.get("chave") == key:
                out.append((e.timestamp, float(e.value["valor"]), e.source_ref))
        return out

    def _in_window(self, e: ClinicalEvent) -> bool:
        return self.window_start <= e.timestamp <= self.now

    # ---------- fatos ----------
    def _transfers(self) -> None:
        for e in self.events:
            if e.kind == "transfer" and self._in_window(e) and e.value and e.value.get("de"):
                self.facts.append(
                    Fact(
                        tipo="transferencia",
                        texto=f"Transferido(a) de {e.value['de']} para {e.value['para']} "
                        f"há {_hours(self.now - e.timestamp)}h ({hhmm(e.timestamp)}).",
                        refs=[e.source_ref],
                    )
                )

    def _vitals(self) -> None:
        latest: dict[str, ClinicalEvent] = {}
        for e in self.events:
            if e.kind == "vital" and self._in_window(e) and e.value and e.value.get("chave") not in ("morse", "braden"):
                latest[e.value["chave"]] = e
        for e in latest.values():
            if e.severity in ("atencao", "critico"):
                horizon = 2 if e.severity == "critico" else 4
                self.facts.append(
                    Fact(
                        tipo="sinal_vital_fora_da_faixa",
                        texto=f"Última medida às {hhmm(e.timestamp)}: {e.summary}.",
                        severidade=e.severity,
                        refs=[e.source_ref],
                        horizonte_h=horizon,
                    )
                )

    def _trends(self) -> None:
        for key, (label, min_delta, direction) in TREND_KEYS.items():
            series = self._series(key, hours=24)[-3:]
            if len(series) < 3 or series[-1][0] < self.window_start:
                continue
            values = [v for _, v, _ in series]
            rising = values[0] < values[1] < values[2]
            falling = values[0] > values[1] > values[2]
            delta = abs(values[2] - values[0])
            if delta < min_delta or not ((direction == "sobe" and rising) or (direction == "desce" and falling)):
                continue
            measure = codes.VITALS[key]
            level = codes.classify(measure, values[2])
            sev = "critico" if level == "critico" else "atencao"
            arrow = " → ".join(_fmt(v, key) for v in values)
            verb = "em ascensão" if direction == "sobe" else "em queda"
            self.facts.append(
                Fact(
                    tipo="tendencia",
                    texto=f"{label} {verb} nas últimas 3 medidas: {arrow} {measure.unit} "
                    f"({hhmm(series[0][0])} → {hhmm(series[2][0])}).",
                    severidade=sev,
                    refs=[ref for _, _, ref in series],
                    horizonte_h=2,
                )
            )

    def _labs(self) -> None:
        latest: dict[str, list[ClinicalEvent]] = {}
        for e in self.events:
            if e.kind == "lab" and e.value and e.timestamp >= self.now - timedelta(hours=36):
                latest.setdefault(e.value["chave"], []).append(e)
        for key, items in latest.items():
            last = items[-1]
            if last.severity not in ("atencao", "critico"):
                continue
            trend = ""
            refs = [last.source_ref]
            severity = last.severity
            if len(items) >= 2:
                prev = items[-2]
                diff = last.value["valor"] - prev.value["valor"]
                if diff:
                    trend = f"; {'subindo' if diff > 0 else 'em queda'} (antes {_fmt(prev.value['valor'], key)})"
                    refs.insert(0, prev.source_ref)
                    # exame alterado mas melhorando (aproximando-se da faixa) não é risco, é evolução
                    if severity == "atencao" and self._distance(key, last.value["valor"]) < self._distance(
                        key, prev.value["valor"]
                    ):
                        severity = "info"
            self.facts.append(
                Fact(
                    tipo="exame_alterado",
                    texto=f"{last.summary.replace(' (fora da faixa)', '').replace(' (CRÍTICO)', '')} "
                    f"em {hhmm(last.timestamp)}{trend}.",
                    severidade=severity,
                    refs=refs,
                    publico=["medico"],
                    horizonte_h=12,
                )
            )

    @staticmethod
    def _distance(key: str, value: float) -> float:
        """Distância até a faixa de referência (0 dentro da faixa)."""
        m = codes.LABS[key]
        if m.low is not None and value < m.low:
            return m.low - value
        if m.high is not None and value > m.high:
            return value - m.high
        return 0.0

    def _urine(self) -> None:
        urine = [
            e
            for e in self.events
            if e.kind == "io" and e.value and e.value.get("chave") == "diurese" and e.timestamp <= self.now
        ]
        catheter_removed = [
            e
            for e in self.events
            if e.kind == "procedure" and "retirada de sonda vesical" in e.summary.lower() and e.timestamp <= self.now
        ]
        if not urine:
            self.facts.append(
                Fact(tipo="diurese", texto="Sem registro de diurese nesta internação.", refs=[], publico=["enfermagem"])
            )
            return
        last = urine[-1]
        hours = _hours(self.now - last.timestamp)
        detail = last.summary.split(": ", 1)[-1]
        if catheter_removed and catheter_removed[-1].timestamp > last.timestamp:
            removed = catheter_removed[-1]
            h_removed = _hours(self.now - removed.timestamp)
            self.facts.append(
                Fact(
                    tipo="diurese",
                    texto=f"Sem registro de diurese espontânea desde a retirada da SVD há {h_removed}h "
                    f"(último registro: {detail}, há {hours}h).",
                    severidade="atencao" if h_removed >= 4 else "info",
                    refs=[last.source_ref, removed.source_ref],
                    publico=["enfermagem", "medico"],
                    horizonte_h=2,
                )
            )
        elif hours >= 6:
            self.facts.append(
                Fact(
                    tipo="diurese",
                    texto=f"Último registro de diurese há {hours}h ({detail}).",
                    severidade="atencao",
                    refs=[last.source_ref],
                    publico=["enfermagem"],
                    horizonte_h=2,
                )
            )
        else:
            self.facts.append(
                Fact(
                    tipo="diurese",
                    texto=f"Último registro de diurese às {hhmm(last.timestamp)}: {detail}.",
                    refs=[last.source_ref],
                    publico=["enfermagem"],
                )
            )

    def _scales(self) -> None:
        for key in ("morse", "braden"):
            series = [e for e in self.events if e.value and e.value.get("chave") == key and e.timestamp <= self.now]
            if not series:
                continue
            last = series[-1]
            value = int(last.value["valor"])
            if key == "morse" and value >= 45:
                self.facts.append(
                    Fact(
                        tipo="risco_queda",
                        texto=f"Risco de queda ALTO (Morse {value}, {hhmm(last.timestamp)}).",
                        severidade="atencao",
                        refs=[last.source_ref],
                        publico=["enfermagem"],
                        horizonte_h=12,
                    )
                )
            elif key == "braden" and value <= 14:
                sev = "atencao" if value <= 12 else "info"
                self.facts.append(
                    Fact(
                        tipo="lesao_por_pressao",
                        texto=f"Risco de lesão por pressão (Braden {value}, {hhmm(last.timestamp)}).",
                        severidade=sev,
                        refs=[last.source_ref],
                        publico=["enfermagem"],
                        horizonte_h=24,
                    )
                )

    def _devices(self) -> None:
        procedures = [e for e in self.events if e.kind == "procedure" and e.timestamp <= self.now]
        for e in procedures:
            category = (e.value or {}).get("categoria") or ""
            text = e.summary.split(": ", 1)[-1]
            hours = _hours(self.now - e.timestamp)
            if category == "acesso vascular":
                long_dwell = hours >= 72
                self.facts.append(
                    Fact(
                        tipo="acesso",
                        texto=f"{text} há {hours}h ({hours // 24} dia(s))"
                        + (" — avaliar troca conforme protocolo institucional." if long_dwell else "."),
                        severidade="atencao" if long_dwell else "info",
                        refs=[e.source_ref],
                        publico=["enfermagem"],
                        horizonte_h=12 if long_dwell else None,
                    )
                )
            elif category == "dispositivo":
                self.facts.append(
                    Fact(
                        tipo="dispositivo",
                        texto=f"{text} ({hhmm(e.timestamp)}, há {hours}h).",
                        refs=[e.source_ref],
                        publico=["enfermagem", "medico"],
                    )
                )
            elif category == "cirurgia" and hours <= 72:
                self.facts.append(Fact(tipo="cirurgia", texto=f"{text} há {hours}h.", refs=[e.source_ref]))

    def _prescription_changes(self) -> None:
        meds = self.r.medications
        by_ref = {m.ref: m for m in meds}
        replaced = {m.prior_ref for m in meds if m.prior_ref}
        for m in meds:
            # suspensões explícitas na janela (trocas aparecem como "Troca", não duplicadas aqui)
            if m.status == "stopped" and m.end and self.window_start <= m.end <= self.now and m.ref not in replaced:
                reason = f" Motivo: {m.reason}." if m.reason else ""
                self.facts.append(
                    Fact(
                        tipo="mudanca_prescricao",
                        texto=f"Suspenso às {hhmm(m.end)}: {m.name} {m.dose}.{reason}",
                        severidade="info",
                        refs=[m.ref],
                    )
                )
            authored = m.authored or m.start
            if not authored or not (self.window_start <= authored <= self.now):
                continue
            previous = self._previous_same_ingredient(m, meds)
            if m.prior_ref and m.prior_ref in by_ref:
                old = by_ref[m.prior_ref]
                if old.ingredient != m.ingredient:
                    self.facts.append(
                        Fact(
                            tipo="mudanca_prescricao",
                            texto=f"Troca às {hhmm(authored)}: {old.name} {old.dose} → {m.text}.",
                            severidade="atencao" if old.drug_class != m.drug_class else "info",
                            refs=[m.ref, old.ref],
                        )
                    )
                    continue
            if previous is None:
                self.facts.append(
                    Fact(tipo="mudanca_prescricao", texto=f"Iniciado às {hhmm(authored)}: {m.text}.", refs=[m.ref])
                )
            elif previous.dose_signature != m.dose_signature:
                self.facts.append(
                    Fact(
                        tipo="mudanca_prescricao",
                        texto=f"Alterado às {hhmm(authored)}: {previous.text} → {m.text}.",
                        refs=[m.ref, previous.ref],
                    )
                )

    def _medication_safety(self) -> None:
        """Achados da reconciliação (alergia, interação, omissão) entram como fatos duros da passagem."""
        origin, dest = default_transition(self.r)
        if dest is None:
            return
        _, findings = run_rules(self.r, origin, dest)
        kind = {
            "alergia": "alergia",
            "alergia_cruzada": "alergia",
            "interacao": "interacao",
            "omissao": "omissao",
            "duplicidade": "duplicidade",
        }
        for f in findings:
            if f.tipo in kind and f.severidade in ("critico", "atencao"):
                self.facts.append(
                    Fact(
                        tipo=kind[f.tipo],
                        texto=f"{f.titulo}. {f.detalhe}",
                        severidade=f.severidade,
                        refs=f.refs,
                        horizonte_h=2 if f.severidade == "critico" else 12,
                    )
                )

    @staticmethod
    def _previous_same_ingredient(m: MedInfo, meds: list[MedInfo]) -> MedInfo | None:
        authored = m.authored or m.start
        candidates = [
            o
            for o in meds
            if o.ref != m.ref
            and o.ingredient == m.ingredient
            and o.start
            and authored
            and o.start < authored
            and (o.end is None or o.end >= authored - timedelta(minutes=5))
        ]
        return max(candidates, key=lambda o: o.start) if candidates else None

    def _next_doses(self, horizon_h: int = 12) -> None:
        # "primeira dose" = nenhum registro de administração do mesmo princípio ativo nesta internação
        ingredient_of = {f"MedicationRequest/{m.ref.split('/', 1)[1]}": m.ingredient for m in self.r.medications}
        administered = {
            ingredient_of.get((e.value or {}).get("pedido") or "")
            for e in self.events
            if e.kind == "med_admin" and e.timestamp <= self.now
        }
        tz = _tz()
        local_now = self.now.astimezone(tz)
        limit = self.now + timedelta(hours=horizon_h)
        for m in self.r.active_meds:
            if m.prn or not m.times:
                continue
            next_dt = None
            for day_offset in (0, 1):
                day = (local_now + timedelta(days=day_offset)).date()
                for t in sorted(m.times):
                    hh, mm = (int(x) for x in t.split(":"))
                    candidate = datetime.combine(day, time(hh, mm), tzinfo=tz)
                    if candidate > local_now and (next_dt is None or candidate < next_dt):
                        next_dt = candidate
            if next_dt is None or next_dt > limit:
                continue
            first = m.ingredient not in administered
            mpp = high_alert_reason(m.ingredient, m.drug_class)
            notes = []
            if first:
                notes.append("primeira dose desta prescrição")
            if mpp:
                notes.append("MPP: dupla checagem")
            suffix = f" ({'; '.join(notes)})" if notes else ""
            self.facts.append(
                Fact(
                    tipo="proxima_dose",
                    texto=f"{next_dt.strftime('%H:%M')} — {m.name} {m.dose} {m.route}{suffix}.",
                    severidade="atencao" if (first or mpp) else "info",
                    refs=[m.ref],
                    publico=["enfermagem", "medico"] if (first or mpp) else ["enfermagem"],
                    horario=next_dt.strftime("%H:%M"),
                    horizonte_h=max(1, int((next_dt - local_now).total_seconds() // 3600) + 1),
                )
            )

    def _pending_exams(self) -> None:
        """Exame pedido/proposto sem resultado (ex.: RX de controle do relatório cirúrgico): pendência do turno."""
        for e in self.r.pending_exams:
            origin = "proposto no relatório cirúrgico" if e.proposto else "solicitado"
            when = f" às {hhmm(e.pedido_em)}" if e.pedido_em else ""
            self.facts.append(
                Fact(
                    tipo="exame_pendente",
                    texto=f"{e.texto} — {origin}{when}, ainda sem resultado: confirmar realização e laudo.",
                    refs=[e.ref],
                )
            )


def compute_facts(record: PatientRecord, events: list[ClinicalEvent], now: datetime, window_hours: int) -> list[Fact]:
    return FactBuilder(record, events, now, window_hours).build()
