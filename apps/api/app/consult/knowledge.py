"""Bases curadas da consulta assistida (data/reference/*.csv). Base de DEMONSTRAÇÃO, não exaustiva.

Elas alimentam a camada determinística dos Smart Apps: rodam sempre (com ou sem LLM) e são a referência de
segurança — a IA complementa, não substitui.
"""

import json
from dataclasses import dataclass, field
from functools import lru_cache

import yaml

from app.config import get_settings
from app.reference import _read_csv


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
    protocol: str
    condition: str
    item: str
    source: str
    interval_days: int
    guidance: str


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


@lru_cache
def protocols() -> tuple[ProtocolDef, ...]:
    return tuple(
        ProtocolDef(r["protocolo"], r["condicao"], r["item"], r["fonte"], int(r["intervalo_dias"]), r["orientacao"])
        for r in _read_csv("care_protocols.csv")
    )


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
def regression_corpus() -> dict:
    """Corpus de regressão das regras (data/consultations/regressao_falas.json)."""
    with open(get_settings().data_dir / "consultations" / "regressao_falas.json", encoding="utf-8") as fh:
        return json.load(fh)


SPECIALTIES: dict[str, str] = {
    "clinica_medica": "Clínica médica",
    "cardiologia": "Cardiologia",
    "endocrinologia": "Endocrinologia",
    "geriatria": "Geriatria",
    "urologia": "Urologia",
    "pediatria": "Pediatria",
    "enfermagem": "Consulta de enfermagem",
    "cirurgia_geral": "Cirurgia geral",
    "ortopedia": "Ortopedia e traumatologia",
    "cirurgia_cardiovascular": "Cirurgia cardiovascular",
    "cirurgia_oncologica": "Cirurgia oncológica",
    "ginecologia_obstetricia": "Ginecologia e obstetrícia",
    "cirurgia_vascular": "Cirurgia vascular",
}

SPECIALTY_FOCUS: dict[str, str] = {
    "clinica_medica": "visão integral: queixas, comorbidades, polifarmácia, prevenção e protocolos",
    "cardiologia": "dor torácica, dispneia e classe funcional, edema, palpitações, pressão e risco cardiovascular",
    "endocrinologia": "controle glicêmico, hipoglicemias, alimentação, atividade física, complicações e tireoide",
    "geriatria": "quedas, cognição, funcionalidade, polifarmácia (critérios de Beers), humor, sono e rede de apoio",
    "urologia": "sintomas do trato urinário inferior (IPSS), hematúria, incontinência, função renal e próstata",
    "pediatria": "hidratação, alimentação, febre, vacinação e desenvolvimento; doses por peso",
    "enfermagem": "autocuidado, técnica de uso de medicamentos, pele, eliminações, sono, alimentação e educação",
    "cirurgia_geral": "indicação, achados, técnica, materiais (OPME), intercorrências e documentação para faturamento",
    "ortopedia": "lateralidade, garrote, implantes e rastreabilidade, radioscopia (tempo, dose) e RX de controle",
    "cirurgia_cardiovascular": "circulação extracorpórea, pinçamento aórtico, enxertos, cardioplegia e profilaxia",
    "cirurgia_oncologica": "lateralidade, peça orientada, margens, linfonodos para estadiamento e reconstrução",
    "ginecologia_obstetricia": "indicação da cesariana, nascimento e Apgar, dequitação, uterotônico e histerorrafia",
    "cirurgia_vascular": "lateralidade, acesso e hemostasia da punção, contraste, stents e radioscopia",
}
