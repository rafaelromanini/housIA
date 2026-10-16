"""Sistemas de código e catálogos clínicos usados na geração e na leitura dos recursos FHIR."""

from dataclasses import dataclass

LOINC = "http://loinc.org"
SNOMED = "http://snomed.info/sct"
ICD10 = "http://hl7.org/fhir/sid/icd-10"
ATC = "http://www.whocc.no/atc"
CPF = "http://rnds.saude.gov.br/fhir/r4/NamingSystem/cpf"
OBS_CATEGORY = "http://terminology.hl7.org/CodeSystem/observation-category"
ACT_CODE = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
ADMIT_SOURCE = "http://terminology.hl7.org/CodeSystem/admit-source"
ROLE_CODE = "http://terminology.hl7.org/CodeSystem/v3-RoleCode"
INTERPRETATION = "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation"

# Sistemas próprios (demonstração)
INGREDIENT = "http://housia.dev/fhir/CodeSystem/principio-ativo"
DRUG_CLASS = "http://housia.dev/fhir/CodeSystem/classe-terapeutica"
LOCAL_SCALES = "http://housia.dev/fhir/CodeSystem/escalas"
TAGS = "http://housia.dev/fhir/CodeSystem/tags"

NO_KNOWN_ALLERGY = "716186003"
LIVES_ALONE_LOINC = "63512-8"  # Quantas pessoas moram neste endereço?
NOTE_MEDICAL = "11506-3"  # Progress note
NOTE_NURSING = "34746-8"  # Nurse note


@dataclass(frozen=True)
class Measure:
    key: str
    system: str
    code: str
    label: str
    unit: str
    low: float | None = None  # limite inferior da faixa esperada
    high: float | None = None  # limite superior da faixa esperada
    crit_low: float | None = None
    crit_high: float | None = None
    decimals: int = 0


VITALS: dict[str, Measure] = {
    m.key: m
    for m in [
        Measure("pas", LOINC, "8480-6", "PA sistólica", "mmHg", 90, 160, 80, 180),
        Measure("pad", LOINC, "8462-4", "PA diastólica", "mmHg", 50, 100, 40, 110),
        Measure("fc", LOINC, "8867-4", "Frequência cardíaca", "bpm", 50, 100, 40, 130),
        Measure("fr", LOINC, "9279-1", "Frequência respiratória", "irpm", 12, 22, 10, 30),
        Measure("temp", LOINC, "8310-5", "Temperatura", "°C", 35.5, 37.7, 35.0, 39.0, 1),
        Measure("spo2", LOINC, "59408-5", "SpO2", "%", 94, None, 90, None),
        Measure("glicemia", LOINC, "41653-7", "Glicemia capilar", "mg/dL", 70, 250, 60, 400),
        Measure("dor", LOINC, "72514-3", "Dor (escala 0–10)", "pontos", None, 4, None, 7),
    ]
}
BP_PANEL = ("85354-9", "Pressão arterial")

IO: dict[str, Measure] = {
    "diurese": Measure("diurese", LOINC, "9187-6", "Diurese", "mL"),
}

SCALES: dict[str, Measure] = {
    # Morse: >= 45 alto risco de queda. Braden: <= 12 alto risco de lesão por pressão.
    "morse": Measure("morse", LOINC, "59460-6", "Escala de Morse (risco de queda)", "pontos", None, 44),
    "braden": Measure("braden", LOCAL_SCALES, "braden", "Escala de Braden (lesão por pressão)", "pontos", 13, None),
}

LABS: dict[str, Measure] = {
    m.key: m
    for m in [
        Measure("potassio", LOINC, "2823-3", "Potássio", "mmol/L", 3.5, 5.0, 3.0, 6.0, 1),
        Measure("sodio", LOINC, "2951-2", "Sódio", "mmol/L", 135, 145, 125, 155),
        Measure("creatinina", LOINC, "2160-0", "Creatinina", "mg/dL", 0.6, 1.3, None, 4.0, 1),
        Measure("ureia", LOINC, "3091-6", "Ureia", "mg/dL", 15, 45, None, 150),
        Measure("hemoglobina", LOINC, "718-7", "Hemoglobina", "g/dL", 12, 17, 7, None, 1),
        Measure("leucocitos", LOINC, "6690-2", "Leucócitos", "mil/mm³", 4, 11, 2, 30, 1),
        Measure("plaquetas", LOINC, "777-3", "Plaquetas", "mil/mm³", 150, 450, 50, None),
        Measure("pcr", LOINC, "1988-5", "Proteína C reativa", "mg/L", None, 5, None, 200),
        Measure("lactato", LOINC, "2524-7", "Lactato", "mmol/L", None, 2.0, None, 4.0, 1),
        Measure("inr", LOINC, "6301-6", "INR", "", 0.8, 1.2, None, 4.0, 1),
        Measure("glicose", LOINC, "2345-7", "Glicose", "mg/dL", 70, 99, 50, 400),
        Measure("troponina", LOINC, "10839-9", "Troponina I", "ng/mL", None, 0.04, None, 1.0, 2),
        Measure("bnp", LOINC, "30934-4", "BNP", "pg/mL", None, 100, None, 1000),
        Measure("hba1c", LOINC, "4548-4", "Hemoglobina glicada", "%", None, 5.7, None, None, 1),
        Measure("ldl", LOINC, "13457-7", "Colesterol LDL", "mg/dL", None, 130, None, None),
    ]
}

ALL_MEASURES: dict[str, Measure] = {**VITALS, **IO, **SCALES, **LABS}
BY_CODE: dict[str, Measure] = {m.code: m for m in ALL_MEASURES.values()}


def classify(measure: Measure, value: float) -> str:
    """Retorna 'normal', 'atencao' ou 'critico' para um valor numérico."""
    if measure.crit_low is not None and value < measure.crit_low:
        return "critico"
    if measure.crit_high is not None and value > measure.crit_high:
        return "critico"
    if measure.low is not None and value < measure.low:
        return "atencao"
    if measure.high is not None and value > measure.high:
        return "atencao"
    return "normal"


def fmt_number(value: float, decimals: int = 0) -> str:
    text = f"{value:.{decimals}f}"
    return text.replace(".", ",")
