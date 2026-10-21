"""Bases curadas da consulta assistida (data/reference/*.csv). Base de DEMONSTRAÇÃO, não exaustiva.

Elas alimentam a camada determinística dos Smart Apps: rodam sempre (com ou sem LLM) e são a referência de
segurança — a IA complementa, não substitui.
"""

import json
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from app.config import get_settings
from app.consult import especialidades
from app.reference import _read_csv

logger = logging.getLogger(__name__)


def _words(cell: str) -> tuple[str, ...]:
    return tuple(w for w in (cell or "").split("|") if w)


@dataclass(frozen=True)
class SymptomDef:
    key: str
    label: str
    words: tuple[str, ...]
    excludes: tuple[str, ...] = ()  # na mesma oração, anulam o sintoma ("a pressão caiu" não é queda)
    age_min: int | None = None  # anos; "febre" vale de 12 em diante e "febre na criança" até 11
    age_max: int | None = None
    sex: str = ""  # "F" ou "M": sintoma que só vale para um sexo
    group: str = ""  # variante de outro sintoma ("tontura ao levantar" agrupa em "tontura")


@dataclass(frozen=True)
class EmotionDef:
    key: str
    label: str
    words: tuple[str, ...]
    approach: str
    excludes: tuple[str, ...] = ()  # pistas que anulam esta emoção na mesma fala ("não entendi" anula confiança)


@dataclass(frozen=True)
class QuestionDef:
    id: str  # estável: a IA devolve estes ids ao dizer quais perguntas o profissional já fez
    trigger: str
    text: str
    reason: str
    asked_words: tuple[str, ...]  # "@chave" vale por todas as palavras do sintoma de mesma chave
    priority: str
    specialty: str
    source: str  # diretriz ou referência de onde vem a pergunta
    age_min: int | None = None
    age_max: int | None = None
    sex: str = ""
    discriminates: tuple[str, ...] = ()  # CID-10 das hipóteses que a resposta ajuda a separar


@dataclass(frozen=True)
class PediatricDose:
    """Faixa de dose pediátrica de um fármaco (pediatric_doses.csv): por kg, por dose e por dia, com a fonte."""

    ingredient: str
    kg_min: float  # mg/kg/dose
    kg_max: float
    interval_h: float  # intervalo mínimo entre doses
    max_dose: float | None  # mg por dose, qualquer que seja o peso
    max_kg_day: float | None  # mg/kg/dia
    max_day: float | None  # mg/dia
    age_max: int
    source: str


@dataclass(frozen=True)
class DifferentialDef:
    """Hipótese diagnóstica (consult_differentials.csv).

    Fichas de evidência: `s:` sintoma, `l:`/`v:` exame/sinal vital (chave:op:limite), `c:` medicamento em uso (princípio
    ou classe), `d:` diagnóstico ativo (prefixo de CID), `p:` procedimento já realizado, `n:` palavra na nota ou na
    conversa. Com `-` na frente, a ficha
    é evidência CONTRA (um exame ou sinal normal, um sintoma que torna a hipótese menos provável); com `!`, é
    obrigatória (a hipótese só entra se ela existir).
    """

    hypothesis: str
    icd: str
    evidence: tuple[str, ...]  # a favor, sem o "!"
    minimum: int
    next_steps: str
    red_flags: str
    against: tuple[str, ...] = ()
    required: tuple[str, ...] = ()
    specialty: str = ""
    age_min: int | None = None
    age_max: int | None = None
    sex: str = ""
    question: str = ""  # a pergunta que mais separa esta hipótese das vizinhas
    exam: str = ""  # o exame que a confirma ou afasta
    source: str = ""


@dataclass(frozen=True)
class AllergyDef:
    allergen: str
    label: str
    words: tuple[str, ...]
    cross: str
    avoid: str
    alternatives: str
    tips: str
    cross_with: tuple[
        str, ...
    ] = ()  # princípios ativos e "@classes" com risco de reação cruzada (atenção, não bloqueio)
    source: str = ""


@dataclass(frozen=True)
class DoseAlertDef:
    item: str
    criterion: str
    limit: float
    severity: str
    recommendation: str
    source: str = ""


@dataclass(frozen=True)
class ProtocolDef:
    """Item de protocolo de APS (data/reference/protocols/*.yaml). `source` diz como o prontuário prova o item."""

    protocol: str
    condition: str  # primeiro prefixo de CID-10 ("" quando o item vale por idade, sexo ou conversa)
    item: str
    source: str
    interval_days: int
    guidance: str
    id: str = ""
    axis: str = ""
    conditions: tuple[str, ...] = ()
    sex: str = ""
    age_min: int | None = None
    age_max: int | None = None
    months_min: int | None = None
    months_max: int | None = None
    trimester: int | None = None
    if_said: tuple[str, ...] = ()  # entra quando a conversa cita o tema
    said: tuple[str, ...] = ()  # dito na consulta = abordado
    question: str = ""
    critical: bool = False
    critical_triggers: tuple[str, ...] = ()
    question_triggers: tuple[str, ...] = ()  # só valem numa pergunta do profissional respondida com "sim"
    citation: str = ""  # "Órgão, documento, ano, página"
    local: bool = False


@dataclass(frozen=True)
class OpmeDef:
    code: str
    description: str
    words: tuple[str, ...]
    implant: bool = False  # implantável: exige etiqueta (lote/série) e vai ao cartão do implante
    sized: bool = False  # tamanho dito na fala ("parafuso 3,5 por 40") entra na conciliação
    gtin: str = ""
    anvisa: str = ""  # registro ANVISA (no catálogo de demonstração: DEMO-ANVISA-*)


@dataclass(frozen=True)
class ProphylaxisDef:
    """Antibioticoprofilaxia cirúrgica (ASHP/IDSA/SIS/SHEA 2013): dose de adulto e pediátrica, janela e redose."""

    key: str
    name: str
    words: tuple[str, ...]
    dose_g: float | None
    dose_g_heavy: float | None  # dose a partir de `heavy_kg` (ex.: cefazolina 3 g se peso >= 120 kg)
    heavy_kg: float | None
    dose_mg_kg: float | None
    dose_mg_kg_ped: float | None  # pediatria (mg/kg), sem passar da dose de adulto
    window_min: int  # antes da incisão
    redose_h: float | None
    note: str
    source: str


def _int(cell: str | None) -> int | None:
    return int(cell) if cell else None


def fits(age_min: int | None, age_max: int | None, sex_required: str, age: int | None, sex: str) -> bool:
    """O paciente (idade em anos, sexo "F"/"M") está dentro do que a linha pede? Idade desconhecida não exclui."""
    if age is not None and ((age_min is not None and age < age_min) or (age_max is not None and age > age_max)):
        return False
    return not sex_required or sex == sex_required


@lru_cache
def symptoms() -> tuple[SymptomDef, ...]:
    return tuple(
        SymptomDef(
            r["chave"],
            r["rotulo"],
            _words(r["palavras"]),
            _words(r.get("exclui", "")),
            _int(r.get("idade_min")),
            _int(r.get("idade_max")),
            r.get("sexo", ""),
            r.get("agrupa_em", ""),
        )
        for r in _read_csv("consult_symptoms.csv")
    )


@lru_cache
def emotions() -> tuple[EmotionDef, ...]:
    return tuple(
        EmotionDef(r["emocao"], r["rotulo"], _words(r["palavras"]), r["abordagem"], _words(r.get("exclui", "")))
        for r in _read_csv("consult_emotions.csv")
    )


@lru_cache
def questions() -> tuple[QuestionDef, ...]:
    symptom_words = {d.key: d.words for d in symptoms()}
    out = []
    for r in _read_csv("consult_questions.csv"):
        asked: list[str] = []
        for w in _words(r["ja_perguntado"]):
            asked += symptom_words.get(w[1:], ()) if w.startswith("@") else (w,)
        out.append(
            QuestionDef(
                r["id"],
                r["gatilho"],
                r["pergunta"],
                r["motivo"],
                tuple(asked),
                r["prioridade"],
                r["especialidade"],
                r["fonte"],
                _int(r.get("idade_min")),
                _int(r.get("idade_max")),
                r.get("sexo", ""),
                _words(r.get("discrimina", "")),
            )
        )
    for pack in especialidades.pacotes().values():  # perguntas extras do pacote da especialidade (#27)
        for q in pack.perguntas:
            out.append(
                QuestionDef(
                    q.id,
                    "especialidade:*",
                    q.pergunta,
                    q.motivo,
                    tuple(q.ja_perguntado),
                    q.prioridade,
                    pack.id,
                    q.fonte,
                    q.idade_min,
                    q.idade_max,
                )
            )
    return tuple(out)


@lru_cache
def semiology_questions() -> dict[str, str]:
    """id da linha → campo semiológico, das perguntas que cobrem os campos vazios de uma queixa."""
    fields = semiology().fields
    return {
        q.id: q.trigger.split(":", 1)[1]
        for q in questions()
        if q.trigger.startswith("semiologia:") and q.trigger.split(":", 1)[1] in fields
    }


@lru_cache
def anamnesis_question_ids() -> frozenset[str]:
    """ids de todas as perguntas que preenchem a anamnese (semiologia da queixa e campos da especialidade)."""
    return frozenset(q.id for q in questions() if q.trigger.startswith("semiologia:"))


@lru_cache
def differentials() -> tuple[DifferentialDef, ...]:
    out = []
    for r in _read_csv("consult_differentials.csv"):
        tokens = _words(r["evidencias"])
        out.append(
            DifferentialDef(
                r["hipotese"],
                r["cid"],
                tuple(t.lstrip("!") for t in tokens if not t.startswith("-")),
                int(r["minimo"]),
                r["proximos_passos"],
                r["sinais_de_alarme"],
                against=tuple(t[1:] for t in tokens if t.startswith("-")),
                required=tuple(t[1:] for t in tokens if t.startswith("!")),
                specialty=r.get("especialidade", ""),
                age_min=_int(r.get("idade_min")),
                age_max=_int(r.get("idade_max")),
                sex=r.get("sexo", ""),
                question=r.get("pergunta", ""),
                exam=r.get("exame", ""),
                source=r.get("fonte", ""),
            )
        )
    return tuple(out)


# ---------- semiologia por queixa (data/reference/consult_semiology.yaml) ----------
@dataclass(frozen=True)
class VocabEntry:
    value: str
    words: tuple[str, ...]
    needs_cue: bool = False  # só vale com pista de piora/melhora na mesma oração


@dataclass(frozen=True)
class SemioGroup:
    id: str
    label: str
    symptoms: tuple[str, ...]
    fields: tuple[str, ...]
    source: str


@dataclass(frozen=True)
class SpecialtyField:
    key: str
    label: str
    source: str
    entries: tuple[VocabEntry, ...]


@dataclass(frozen=True)
class SemiologyDef:
    version: str
    source: str
    fields: dict[str, str]  # chave do campo -> rótulo
    groups: tuple[SemioGroup, ...]
    worsening: tuple[VocabEntry, ...]
    relief: tuple[VocabEntry, ...]
    worsening_cues: tuple[str, ...]
    relief_cues: tuple[str, ...]
    location: tuple[VocabEntry, ...]
    specialty_fields: dict[str, tuple[SpecialtyField, ...]] = field(default_factory=dict)
    specialty_max_age: dict[str, int] = field(default_factory=dict)

    def group_of(self, symptom: str) -> SemioGroup:
        for g in self.groups:
            if symptom in g.symptoms:
                return g
        return next(g for g in self.groups if g.id == "geral")


def _vocab(items: list[dict]) -> tuple[VocabEntry, ...]:
    return tuple(
        VocabEntry(i["valor"], tuple(str(w) for w in i["palavras"]), bool(i.get("exige_pista"))) for i in items
    )


@lru_cache
def semiology() -> SemiologyDef:
    with open(get_settings().data_dir / "reference" / "consult_semiology.yaml", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    extras = raw.get("especialidades", {})
    return SemiologyDef(
        version=str(raw["versao"]),
        source=" ".join(str(raw["fonte"]).split()),
        fields=dict(raw["campos"]),
        groups=tuple(
            SemioGroup(g["id"], g["rotulo"], tuple(g["sintomas"]), tuple(g["campos"]), " ".join(g["fonte"].split()))
            for g in raw["grupos"]
        ),
        worsening=_vocab(raw["piora"]),
        relief=_vocab(raw["melhora"]),
        worsening_cues=tuple(raw["pista_piora"]),
        relief_cues=tuple(raw["pista_melhora"]),
        location=_vocab(raw["localizacao"]),
        specialty_fields={
            spec: tuple(
                SpecialtyField(c["campo"], c["rotulo"], c["fonte"], _vocab(c["entradas"])) for c in body["campos"]
            )
            for spec, body in extras.items()
        },
        specialty_max_age={spec: int(body["idade_max"]) for spec, body in extras.items() if "idade_max" in body},
    )


@lru_cache
def lab_lay() -> dict[str, dict[str, str]]:
    return {r["chave"]: r for r in _read_csv("lab_lay.csv")}


@dataclass(frozen=True)
class ConditionDef:
    """Antecedente pessoal (data/reference/condition_lexicon.csv): termo popular ou técnico → CID-10."""

    key: str
    label: str
    cid: str
    terms: tuple[str, ...]
    source: str


@lru_cache
def conditions() -> tuple[ConditionDef, ...]:
    return tuple(
        ConditionDef(r["chave"], r["condicao"], r["cid"], _words(r["termos"]), r["fonte"])
        for r in _read_csv("condition_lexicon.csv")
    )


@dataclass(frozen=True)
class ProcedureDef:
    """Cirurgia prévia (data/reference/procedure_lexicon.csv): como o paciente fala e como o prontuário escreve."""

    key: str
    label: str
    terms: tuple[str, ...]
    chart_patterns: tuple[str, ...]  # expressões regulares sobre o texto normalizado de Procedure.code.text
    source: str


@lru_cache
def procedures() -> tuple[ProcedureDef, ...]:
    return tuple(
        ProcedureDef(r["chave"], r["procedimento"], _words(r["termos"]), _words(r["termos_prontuario"]), r["fonte"])
        for r in _read_csv("procedure_lexicon.csv")
    )


@dataclass(frozen=True)
class AnalyteDef:
    """Analito que o paciente pode citar (data/reference/lab_lay.csv): sinônimos e faixa de referência."""

    key: str
    label: str
    unit: str
    low: float | None
    high: float | None
    decimals: int
    synonyms: tuple[str, ...]
    qualitative: bool  # sem valor numérico (EAS): só a explicação em linguagem simples


@lru_cache
def analytes() -> tuple[AnalyteDef, ...]:
    from app.fhir_client import codes

    out = []
    for key, r in lab_lay().items():
        m = codes.LABS.get(key)  # a faixa do prontuário (codes.LABS) prevalece sobre a do CSV
        low = m.low if m else _num(r.get("ref_min", ""))
        high = m.high if m else _num(r.get("ref_max", ""))
        out.append(
            AnalyteDef(
                key,
                m.label if m else r.get("rotulo") or key,
                m.unit if m else r.get("unidade", ""),
                low,
                high,
                m.decimals if m else int(r.get("casas") or 0),
                _words(r.get("sinonimos", "")),
                not (low is not None or high is not None),
            )
        )
    return tuple(out)


@lru_cache
def allergy_defs() -> tuple[AllergyDef, ...]:
    return tuple(
        AllergyDef(
            r["alergeno"],
            r["rotulo"],
            _words(r["palavras"]),
            r["reatividade_cruzada"],
            r["evitar"],
            r["alternativas"],
            r["dicas"],
            _words(r.get("cruza_com", "")),
            r.get("fonte", ""),
        )
        for r in _read_csv("allergy_cross.csv")
    )


@lru_cache
def dose_alerts() -> tuple[DoseAlertDef, ...]:
    return tuple(
        DoseAlertDef(
            r["item"], r["criterio"], float(r["limite"]), r["severidade"], r["recomendacao"], r.get("fonte", "")
        )
        for r in _read_csv("dose_alerts.csv")
    )


def _float(cell: str | None) -> float | None:
    return float(cell) if cell not in (None, "") else None


@lru_cache
def pediatric_doses() -> dict[str, PediatricDose]:
    """Doses pediátricas por peso (data/reference/pediatric_doses.csv), por princípio ativo de drugs.csv."""
    return {
        r["principio_ativo"]: PediatricDose(
            r["principio_ativo"],
            float(r["mg_kg_dose_min"]),
            float(r["mg_kg_dose_max"]),
            float(r["intervalo_h_min"]),
            _float(r["max_mg_dose"]),
            _float(r["max_mg_kg_dia"]),
            _float(r["max_mg_dia"]),
            int(r["idade_max_anos"]),
            r["fonte"],
        )
        for r in _read_csv("pediatric_doses.csv")
    }


PROTOCOL_AXES = ("Criança", "Mulher", "Adulto crônico", "Idoso", "Saúde mental", "Telessaúde")
_PROTOCOL_KEYS = {
    "id", "protocolo", "item", "fonte", "pagina", "registro", "intervalo_dias", "orientacao", "fala", "pergunta",
    "critico", "gatilhos_criticos", "gatilhos_pergunta", "condicao", "sexo", "idade_min", "idade_max", "meses_min",
    "meses_max",
    "trimestre", "aplica_se_fala", "ativo",
}  # fmt: skip
_PROTOCOL_REQUIRED = ("id", "protocolo", "item", "fonte", "registro", "intervalo_dias", "orientacao")
_RECORD_KINDS = ("lab", "texto", "vacina", "vital", "escala", "nenhum")


def protocols_dir() -> Path:
    return get_settings().data_dir / "reference" / "protocols"


def protocols_local_dir() -> Path:
    override = get_settings().protocolos_locais_dir
    return Path(override) if override else protocols_dir() / "local"


def _yaml_files(folder: Path) -> list[Path]:
    return sorted(folder.glob("*.yaml")) if folder.is_dir() else []


def _protocol_def(row: dict, axis: str, sources: dict, where: str, local: bool) -> ProtocolDef:
    missing = [k for k in _PROTOCOL_REQUIRED if row.get(k) in (None, "")]
    if missing:
        raise ValueError(f"{where}: faltam campos {missing}")
    unknown = sorted(set(row) - _PROTOCOL_KEYS)
    if unknown:
        raise ValueError(f"{where}: campos desconhecidos {unknown}")
    src = sources.get(row["fonte"])
    if (
        not isinstance(src, dict)
        or not all(str(src.get(k) or "").strip() for k in ("documento", "orgao"))
        or not isinstance(src.get("ano"), int)
        or not 1900 <= src["ano"] <= 2100
        or not str(row.get("pagina") or src.get("pagina") or "").strip()
    ):
        raise ValueError(f"{where}: fonte '{row['fonte']}' sem documento, órgão, ano e página")
    for key in ("intervalo_dias", "idade_min", "idade_max", "meses_min", "meses_max", "trimestre"):
        value = row.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ValueError(f"{where}: {key} deve ser um número inteiro, não negativo")
    kind = str(row["registro"]).partition(":")[0]
    if kind not in _RECORD_KINDS:
        raise ValueError(f"{where}: registro '{row['registro']}' desconhecido")
    conditions = tuple(str(c) for c in row.get("condicao") or ())
    return ProtocolDef(
        protocol=row["protocolo"],
        condition=conditions[0] if conditions else "",
        item=row["item"],
        source=str(row["registro"]),
        interval_days=int(row["intervalo_dias"]),
        guidance=row["orientacao"],
        id=row["id"],
        axis=axis,
        conditions=conditions,
        sex=row.get("sexo") or "",
        age_min=row.get("idade_min"),
        age_max=row.get("idade_max"),
        months_min=row.get("meses_min"),
        months_max=row.get("meses_max"),
        trimester=row.get("trimestre"),
        if_said=tuple(str(w) for w in row.get("aplica_se_fala") or ()),
        said=tuple(str(w) for w in row.get("fala") or ()),
        question=row.get("pergunta") or "",
        critical=bool(row.get("critico")),
        critical_triggers=tuple(str(w) for w in row.get("gatilhos_criticos") or ()),
        question_triggers=tuple(str(w) for w in row.get("gatilhos_pergunta") or ()),
        citation=", ".join([src["orgao"], src["documento"], str(src["ano"]), str(row.get("pagina") or src["pagina"])]),
        local=local,
    )


# num item crítico (risco de suicídio), o arquivo local só ajusta texto e periodicidade: não desliga o item, não tira o
# crítico, nem troca os gatilhos, a idade ou o sexo
_CRITICAL_LOCAL_KEYS = {"id", "item", "protocolo", "fonte", "pagina", "orientacao", "pergunta", "intervalo_dias"}


def _read_yaml_items(path: Path) -> tuple[dict, list]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict) or not isinstance(raw.get("itens") or [], list):
        raise ValueError(f"{path.name}: esperado um mapa com eixo, fontes e itens")
    return raw, raw.get("itens") or []


def load_protocols(folder: Path, local_folder: Path | None = None) -> tuple[ProtocolDef, ...]:
    """Lê os YAML de `folder` e aplica por cima os de `local_folder` (instituição ou município): o `id` repetido troca
    só os campos informados, `ativo: false` desliga e um `id` novo acrescenta o item.
    Erro de schema na base: ValueError.
    Erro no arquivo local (YAML quebrado, id sem os campos de um item novo, valor inválido): o item ou arquivo é
    ignorado com aviso e a base segue valendo; os itens críticos não podem ser desligados nem enfraquecidos."""
    rows: dict[str, tuple[dict, str, dict, str]] = {}
    for path in _yaml_files(folder):
        raw, itens = _read_yaml_items(path)
        axis = raw.get("eixo")
        if axis not in PROTOCOL_AXES:
            raise ValueError(f"{path.name}: eixo '{axis}' desconhecido")
        for row in itens:
            if row.get("id") in rows:
                raise ValueError(f"{path.name}: id repetido {row.get('id')}")
            rows[row.get("id")] = (row, axis, raw.get("fontes") or {}, path.name)
    base_defs = {
        rid: _protocol_def(row, axis, src, f"{origin}:{rid}", False) for rid, (row, axis, src, origin) in rows.items()
    }
    local_ids: set[str] = set()
    for path in _yaml_files(local_folder) if local_folder else []:
        try:
            raw, itens = _read_yaml_items(path)
        except (OSError, ValueError, yaml.YAMLError) as e:
            logger.warning("protocolos locais: arquivo %s ignorado (%s)", path.name, str(e).splitlines()[0])
            continue
        for row in itens:
            try:
                rid = row["id"]
                if not isinstance(rid, str) or not rid:
                    raise ValueError("item sem id")
                if rid in rows:
                    base, axis, sources, _ = rows[rid]
                    if base_defs[rid].critical:
                        dropped = sorted(set(row) - _CRITICAL_LOCAL_KEYS)
                        if dropped:
                            logger.warning("protocolos locais: %s é crítico, campos %s ignorados", rid, dropped)
                        row = {k: v for k, v in row.items() if k in _CRITICAL_LOCAL_KEYS}
                        if set(row) <= {"id"}:
                            continue
                    merged = (base | row, axis, sources | (raw.get("fontes") or {}), path.name)
                else:
                    axis = raw.get("eixo")
                    if axis not in PROTOCOL_AXES:
                        raise ValueError(f"eixo '{axis}' desconhecido")
                    merged = (row, axis, raw.get("fontes") or {}, path.name)
                if merged[0].get("ativo", True):
                    _protocol_def(
                        merged[0], merged[1], merged[2], f"{path.name}:{rid}", True
                    )  # valida antes de aceitar
                rows[rid] = merged
                local_ids.add(rid)
            except (KeyError, TypeError, AttributeError, ValueError) as e:
                logger.warning("protocolos locais: item de %s ignorado (%s)", path.name, e)
    return tuple(
        _protocol_def(row, axis, sources, f"{origin}:{rid}", rid in local_ids)
        for rid, (row, axis, sources, origin) in rows.items()
        if row.get("ativo", True)
    )


@lru_cache
def protocols() -> tuple[ProtocolDef, ...]:
    return load_protocols(protocols_dir(), protocols_local_dir())


@lru_cache
def opme_catalog() -> tuple[OpmeDef, ...]:
    return tuple(
        OpmeDef(
            r["codigo"],
            r["descricao"],
            _words(r["palavras"]),
            implant=r.get("implantavel") == "sim",
            sized=r.get("tamanho") == "sim",
            gtin=r.get("gtin", ""),
            anvisa=r.get("registro_anvisa", ""),
        )
        for r in _read_csv("opme_demo.csv")
    )


def _num(cell: str) -> float | None:
    return float(cell) if cell else None


@lru_cache
def prophylaxis() -> dict[str, ProphylaxisDef]:
    return {
        r["principio_ativo"]: ProphylaxisDef(
            r["principio_ativo"],
            r["farmaco"],
            _words(r["sinonimos"]),
            _num(r["dose_g"]),
            _num(r["dose_g_peso_alto"]),
            _num(r["peso_alto_kg"]),
            _num(r["dose_mg_kg"]),
            _num(r["dose_mg_kg_pediatrica"]),
            int(r["janela_min"]),
            _num(r["redose_h"]),
            r["observacao"],
            r["fonte"],
        )
        for r in _read_csv("surgical_prophylaxis.csv")
    }


@dataclass(frozen=True)
class LabDef:
    """Exame pedível (data/reference/lab_catalog.csv): sinônimos, LOINC, preparo e intervalo mínimo entre repetições."""

    key: str
    name: str
    category: str  # "laboratorio" | "imagem"
    words: tuple[str, ...]
    loinc: str
    loinc_name: str
    result_keys: tuple[str, ...]  # chaves de Observation (codes.LABS) que trazem o resultado desse exame
    interval_days: int  # repetir antes disso pede justificativa (0 = sem aviso)
    prep: str
    source: str


@lru_cache
def lab_catalog() -> tuple[LabDef, ...]:
    return tuple(
        LabDef(
            r["chave"],
            r["exame"],
            r["categoria"],
            _words(r["sinonimos"]),
            r["loinc"],
            r["loinc_nome"],
            _words(r["chaves_resultado"]),
            int(r["intervalo_dias"]),
            r["preparo"],
            r["fonte"],
        )
        for r in _read_csv("lab_catalog.csv")
    )


@lru_cache
def demo_scripts() -> dict[str, dict]:
    """Roteiros de demonstração (data/consultations/*.json), por id do arquivo.

    Só arquivos com `falas`: o corpus de regressão (`regressao_falas.json`) mora na mesma pasta e não é roteiro.
    """
    folder = get_settings().data_dir / "consultations"
    out: dict[str, dict] = {}
    for path in sorted(folder.glob("*.json")):
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if "falas" in data:
            out[path.stem] = data | {"id": path.stem}
    return out


@lru_cache
def recorded_translations() -> tuple[tuple[str, str, str, str], ...]:
    """Traduções pré-gravadas dos roteiros (fala `traducao` + `idioma`), que o modo mock usa no lugar do LLM.

    Linhas (fala, idioma de destino, tradução). Fala em outro idioma traduz para pt-BR; fala em pt-BR do
    profissional traduz para o `idioma_paciente` do roteiro. Ficam no texto original: quem usa pseudonimiza.
    """
    out: list[tuple[str, str, str, str]] = []
    for script in demo_scripts().values():
        patient_lang = script.get("idioma_paciente", "pt-BR")
        for f in script["falas"]:
            if f.get("traducao"):
                src = f.get("idioma", "pt-BR")
                out.append(
                    (script["patient_id"], f["texto"], "pt-BR" if src != "pt-BR" else patient_lang, f["traducao"])
                )
    return tuple(out)


@lru_cache
def regression_corpus() -> dict:
    """Corpus de regressão das regras (data/consultations/regressao_falas.json)."""
    with open(get_settings().data_dir / "consultations" / "regressao_falas.json", encoding="utf-8") as fh:
        return json.load(fh)


SPECIALTIES: dict[str, str] = {k: p.rotulo for k, p in especialidades.pacotes().items()}
SPECIALTY_FOCUS: dict[str, str] = {k: p.foco for k, p in especialidades.pacotes().items()}


def specialty_pack(specialty: str) -> especialidades.Pacote:
    """Pacote da especialidade (KeyError se não existir: quem chama já validou contra SPECIALTIES)."""
    return especialidades.pacotes()[specialty]
