"""Pilar 3 — camada determinística da reconciliação medicamentosa (roda sempre, antes do LLM).

Regras: omissão na transição, duplicidade terapêutica, interações (tabela curada de demonstração),
medicamentos potencialmente perigosos (dupla checagem) e alergia x prescrição.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import combinations
from typing import Literal

from pydantic import BaseModel, model_validator

from app.fhir_client import codes
from app.reference import class_label, high_alert_reason, interactions, matches
from app.timeline.record import MedInfo, PatientRecord, Sector, coding, normalize

RowStatus = Literal["mantida", "alterada", "suspensa", "omitida", "nova"]
FindingType = Literal["omissao", "duplicidade", "interacao", "alto_alerta", "alergia", "alergia_cruzada"]
Severity = Literal["info", "atencao", "critico"]

# Omissão destas classes na transição é crítica (retirada abrupta ou perda de proteção)
CRITICAL_OMISSION = {
    "betabloqueador": "Suspensão abrupta de betabloqueador pode causar taquicardia e hipertensão rebote.",
    "agonista_alfa2": "Suspensão abrupta de clonidina pode causar crise hipertensiva rebote.",
    "anticoagulante_oral": "Interrupção de anticoagulação aumenta o risco tromboembólico.",
    "heparina": "Interrupção de anticoagulação/profilaxia aumenta o risco tromboembólico.",
    "insulina_basal": "Interrupção de insulina basal pode causar hiperglicemia e cetoacidose.",
    "anticonvulsivante": "Interrupção de anticonvulsivante pode precipitar crise convulsiva.",
    "corticoide": "Suspensão abrupta de corticoide pode causar insuficiência adrenal.",
    "antiarritmico": "Interrupção de antiarrítmico pode descompensar o ritmo cardíaco.",
    "benzodiazepinico": "Suspensão abrupta de benzodiazepínico pode causar abstinência.",
}
DUPLICITY_EXEMPT = {"analgesico_simples", "desconhecida"}
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
CROSS_REACTIVITY = {"penicilina": {"cefalosporina"}, "cefalosporina": {"penicilina"}}


class MedSummary(BaseModel):
    ref: str
    texto: str
    dose: str
    via: str
    horarios: list[str]
    se_necessario: bool
    status: str
    motivo: str | None = None


class RecRow(BaseModel):
    principio_ativo: str
    nome: str
    classe: str
    classe_rotulo: str = ""
    origem: MedSummary | None
    destino: MedSummary | None
    status: RowStatus
    observacao: str | None = None
    refs: list[str]

    @model_validator(mode="after")
    def _fill_label(self) -> "RecRow":
        self.classe_rotulo = self.classe_rotulo or class_label(self.classe)
        return self


class Finding(BaseModel):
    tipo: FindingType
    severidade: Severity
    titulo: str
    detalhe: str
    recomendacao: str
    refs: list[str]
    medicamentos: list[str]


def _summary(m: MedInfo) -> MedSummary:
    return MedSummary(
        ref=m.ref,
        texto=m.text,
        dose=m.dose,
        via=m.route,
        horarios=list(m.times),
        se_necessario=m.prn,
        status=m.status,
        motivo=m.reason,
    )


def _by_ingredient(meds: list[MedInfo]) -> dict[str, MedInfo]:
    out: dict[str, MedInfo] = {}
    for m in sorted(meds, key=lambda m: m.start or m.authored or _EPOCH):  # o mais recente prevalece
        out[m.ingredient] = m
    return out


def reconcile(record: PatientRecord, origin: Sector, dest: Sector) -> list[RecRow]:
    """Compara a prescrição em uso na saída da origem com a prescrição do destino."""
    origin_meds = record.meds_of_encounter(origin.encounter_id)
    dest_meds = record.meds_of_encounter(dest.encounter_id)
    cutoff = origin.end or dest.start
    in_use = [
        m
        for m in origin_meds
        if m.status != "stopped" and (m.end is None or cutoff is None or m.end >= cutoff - timedelta(minutes=5))
    ]
    recently_stopped = [
        m for m in origin_meds if m.status == "stopped" and m.end and cutoff and m.end >= cutoff - timedelta(hours=24)
    ]
    dest_active = _by_ingredient([m for m in dest_meds if m.status == "active"])
    dest_stopped = _by_ingredient([m for m in dest_meds if m.status == "stopped"])
    origin_use = _by_ingredient(in_use)
    origin_stop = _by_ingredient(recently_stopped)
    replacements = {m.prior_ref: m for m in dest_meds if m.prior_ref}

    rows: list[RecRow] = []
    for ing, o in origin_use.items():
        d = dest_active.get(ing)
        if d:
            same = o.dose_signature == d.dose_signature
            rows.append(
                RecRow(
                    principio_ativo=ing,
                    nome=o.name,
                    classe=o.drug_class,
                    origem=_summary(o),
                    destino=_summary(d),
                    status="mantida" if same else "alterada",
                    observacao=None if same else "Dose, via ou horário diferente na transição.",
                    refs=[o.ref, d.ref],
                )
            )
        elif ing in dest_stopped:
            s = dest_stopped[ing]
            rows.append(
                RecRow(
                    principio_ativo=ing,
                    nome=o.name,
                    classe=o.drug_class,
                    origem=_summary(o),
                    destino=_summary(s),
                    status="suspensa",
                    observacao=f"Suspenso no destino: {s.reason or 'motivo não registrado'}",
                    refs=[o.ref, s.ref],
                )
            )
        elif o.ref in replacements:
            new = replacements[o.ref]
            rows.append(
                RecRow(
                    principio_ativo=ing,
                    nome=o.name,
                    classe=o.drug_class,
                    origem=_summary(o),
                    destino=None,
                    status="suspensa",
                    observacao=f"Substituído por {new.name}.",
                    refs=[o.ref, new.ref],
                )
            )
        else:
            rows.append(
                RecRow(
                    principio_ativo=ing,
                    nome=o.name,
                    classe=o.drug_class,
                    origem=_summary(o),
                    destino=None,
                    status="omitida",
                    observacao="Em uso na origem, ausente no destino e sem registro de suspensão.",
                    refs=[o.ref],
                )
            )
    for ing, o in origin_stop.items():
        if ing in origin_use or ing in dest_active:
            continue
        rows.append(
            RecRow(
                principio_ativo=ing,
                nome=o.name,
                classe=o.drug_class,
                origem=_summary(o),
                destino=None,
                status="suspensa",
                observacao=f"Suspenso na origem: {o.reason or 'motivo não registrado'}",
                refs=[o.ref],
            )
        )
    covered = {r.principio_ativo for r in rows}
    by_ref = {m.ref: m for m in record.medications}
    for ing, d in dest_active.items():
        if ing in covered:
            continue
        note = None
        if d.prior_ref and d.prior_ref in by_ref:
            note = f"Substitui {by_ref[d.prior_ref].name}."
        rows.append(
            RecRow(
                principio_ativo=ing,
                nome=d.name,
                classe=d.drug_class,
                origem=None,
                destino=_summary(d),
                status="nova",
                observacao=note,
                refs=[d.ref],
            )
        )
    order = {"omitida": 0, "alterada": 1, "nova": 2, "suspensa": 3, "mantida": 4}
    rows.sort(key=lambda r: (order[r.status], r.nome))
    return rows


def omission_findings(rows: list[RecRow], origin: Sector, dest: Sector) -> list[Finding]:
    out = []
    for r in rows:
        if r.status != "omitida" or r.origem is None:
            continue
        risk = CRITICAL_OMISSION.get(r.classe)
        out.append(
            Finding(
                tipo="omissao",
                severidade="critico" if risk else "atencao",
                titulo=f"Possível omissão: {r.nome} ({r.origem.dose} {r.origem.via}) não consta no destino",
                detalhe=f"{r.origem.texto} estava em uso em {origin.ward} e não aparece na prescrição de {dest.ward}, "
                "sem registro de suspensão." + (f" {risk}" if risk else ""),
                recomendacao="Confirmar com a equipe se a suspensão foi intencional; se não, represcrever e registrar.",
                refs=r.refs,
                medicamentos=[r.principio_ativo],
            )
        )
    return out


def duplicity_findings(meds: list[MedInfo]) -> list[Finding]:
    out = []
    by_ing: dict[str, list[MedInfo]] = {}
    by_class: dict[str, list[MedInfo]] = {}
    for m in meds:
        by_ing.setdefault(m.ingredient, []).append(m)
        by_class.setdefault(m.drug_class, []).append(m)
    for ing, group in by_ing.items():
        if len(group) > 1:
            out.append(
                Finding(
                    tipo="duplicidade",
                    severidade="atencao",
                    titulo=f"Duplicidade: {group[0].name} prescrito {len(group)} vezes",
                    detalhe="; ".join(m.text for m in group),
                    recomendacao="Manter apenas uma prescrição ativa do princípio ativo.",
                    refs=[m.ref for m in group],
                    medicamentos=[ing],
                )
            )
    for cls, group in by_class.items():
        ingredients = {m.ingredient for m in group}
        if cls in DUPLICITY_EXEMPT or len(ingredients) < 2:
            continue
        out.append(
            Finding(
                tipo="duplicidade",
                severidade="atencao",
                titulo=f"Duplicidade terapêutica ({class_label(cls)}): " + " + ".join(sorted({m.name for m in group})),
                detalhe="; ".join(m.text for m in group),
                recomendacao="Confirmar se a associação de dois fármacos da mesma classe é intencional.",
                refs=[m.ref for m in group],
                medicamentos=sorted(ingredients),
            )
        )
    return out


def interaction_findings(meds: list[MedInfo]) -> list[Finding]:
    out = []
    unique = list(_by_ingredient(meds).values())
    for a, b in combinations(unique, 2):
        for row in interactions():
            hit = (matches(row["a"], a.ingredient, a.drug_class) and matches(row["b"], b.ingredient, b.drug_class)) or (
                matches(row["a"], b.ingredient, b.drug_class) and matches(row["b"], a.ingredient, a.drug_class)
            )
            if hit:
                out.append(
                    Finding(
                        tipo="interacao",
                        severidade=row["severidade"],
                        titulo=f"Interação: {a.name} + {b.name}",
                        detalhe=row["descricao"],
                        recomendacao=row["recomendacao"],
                        refs=[a.ref, b.ref],
                        medicamentos=[a.ingredient, b.ingredient],
                    )
                )
                break
    return out


def high_alert_findings(meds: list[MedInfo], new_ingredients: set[str]) -> list[Finding]:
    out = []
    for m in _by_ingredient(meds).values():
        reason = high_alert_reason(m.ingredient, m.drug_class)
        if reason:
            is_new = m.ingredient in new_ingredients
            out.append(
                Finding(
                    tipo="alto_alerta",
                    severidade="atencao" if is_new else "info",
                    titulo=f"Dupla checagem: {m.name}" + (" (novo na transição)" if is_new else ""),
                    detalhe=reason,
                    recomendacao="Realizar dupla checagem independente antes de administrar.",
                    refs=[m.ref],
                    medicamentos=[m.ingredient],
                )
            )
    return out


def allergy_findings(record: PatientRecord, meds: list[MedInfo]) -> list[Finding]:
    out = []
    for allergy in record.real_allergies:
        cc = allergy.get("code", {})
        ing = (coding(cc, codes.INGREDIENT) or {}).get("code")
        cls = (coding(cc, codes.DRUG_CLASS) or {}).get("code")
        label = cc.get("text", "alergia registrada")
        reaction = "; ".join(m.get("text", "") for r in allergy.get("reaction", []) for m in r.get("manifestation", []))
        for m in _by_ingredient(meds).values():
            direct = (
                (ing and ing == m.ingredient)
                or (cls and cls == m.drug_class)
                or (not ing and not cls and normalize(m.name) in normalize(label))
            )
            cross = cls in CROSS_REACTIVITY and m.drug_class in CROSS_REACTIVITY[cls]
            if direct:
                out.append(
                    Finding(
                        tipo="alergia",
                        severidade="critico",
                        titulo=f"Alergia: {m.name} prescrito para paciente alérgico a {label}",
                        detalhe=f"Reação registrada: {reaction or 'não descrita'}. Prescrição: {m.text}.",
                        recomendacao="Suspender a prescrição e escolher alternativa; confirmar a alergia com "
                        "paciente/família.",
                        refs=[f"AllergyIntolerance/{allergy['id']}", m.ref],
                        medicamentos=[m.ingredient],
                    )
                )
            elif cross:
                out.append(
                    Finding(
                        tipo="alergia_cruzada",
                        severidade="atencao",
                        titulo=f"Possível reação cruzada: {m.name} em paciente alérgico a {label}",
                        detalhe=f"Reação registrada: {reaction or 'não descrita'}.",
                        recomendacao="Avaliar risco de reatividade cruzada antes de administrar.",
                        refs=[f"AllergyIntolerance/{allergy['id']}", m.ref],
                        medicamentos=[m.ingredient],
                    )
                )
    return out


def proposal_review(record: PatientRecord, dest_active: list[MedInfo]) -> tuple[list[dict], list[Finding]]:
    """Prescrições propostas e assinadas na consulta com IA (MedicationRequest draft/proposal) entram na reconciliação:
    cada uma é conferida contra alergias e contra o que já está prescrito, como se estivesse ativa."""
    raw = {f"MedicationRequest/{r['id']}": r for r in record.resources.get("MedicationRequest", [])}
    rows: list[dict] = []
    findings: list[Finding] = []
    for p in record.proposed_meds:
        r = raw.get(p.ref, {})
        trial = replace(p, status="active")
        others = [m for m in dest_active if m.ref != p.prior_ref]  # a prescrição que a proposta substitui não conta
        mine = allergy_findings(record, [trial])
        mine += [f for f in interaction_findings([*others, trial]) if p.ref in f.refs]
        mine += [f for f in duplicity_findings([*others, trial]) if p.ref in f.refs]
        mine += high_alert_findings([trial], {p.ingredient})
        findings += [f.model_copy(update={"titulo": f"Prescrição proposta — {f.titulo}"}) for f in mine]
        rows.append(
            {
                "ref": p.ref,
                "nome": p.name,
                "classe": p.drug_class,
                "classe_rotulo": class_label(p.drug_class),
                "texto": p.text,
                "acao": "substitui" if p.prior_ref else "nova",
                "substitui": p.prior_ref,
                "prescritor": (r.get("requester") or {}).get("display"),
                "em": r.get("authoredOn"),
                "justificativa": ((r.get("reasonCode") or [{}])[0]).get("text"),
                "alertas": [f.titulo for f in mine if f.severidade == "critico"],
            }
        )
    return rows, findings


def default_transition(record: PatientRecord) -> tuple[Sector | None, Sector | None]:
    """Origem = setor anterior ao atual; destino = setor atual."""
    sectors = record.sectors
    if not sectors:
        return None, None
    dest = record.current_sector or sectors[-1]
    idx = sectors.index(dest)
    return (sectors[idx - 1] if idx > 0 else None), dest


def resolve_sector(record: PatientRecord, key: str | None) -> Sector | None:
    """Aceita id do encounter do setor ou o nome do setor (location)."""
    if not key:
        return None
    for s in record.sectors:
        if key in (s.encounter_id, f"Encounter/{s.encounter_id}") or normalize(key) == normalize(s.ward):
            return s
    return None


SEV_ORDER = {"critico": 0, "atencao": 1, "info": 2}


def run_rules(record: PatientRecord, origin: Sector | None, dest: Sector) -> tuple[list[RecRow], list[Finding]]:
    dest_active = [m for m in record.meds_of_encounter(dest.encounter_id) if m.status == "active"]
    rows: list[RecRow] = []
    findings: list[Finding] = []
    if origin is not None:
        rows = reconcile(record, origin, dest)
        findings += omission_findings(rows, origin, dest)
    new_ingredients = {r.principio_ativo for r in rows if r.status in ("nova", "alterada")}
    findings += allergy_findings(record, dest_active)
    findings += interaction_findings(dest_active)
    findings += duplicity_findings(dest_active)
    findings += high_alert_findings(dest_active, new_ingredients)
    findings.sort(key=lambda f: (SEV_ORDER[f.severidade], f.tipo))
    return rows, findings
