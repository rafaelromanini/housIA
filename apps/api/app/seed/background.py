"""População de fundo determinística (seed fixa) para o painel de leitos parecer real.

Os pacientes são fictícios e simples; os 5 cenários roteirizados (data/scenarios) são os da demo.
Gera a mesma DSL dos cenários, consumida por app.seed.builder.
"""

import random

MALE = [
    "José",
    "João",
    "Francisco",
    "Carlos",
    "Paulo",
    "Pedro",
    "Luiz",
    "Marcos",
    "Rafael",
    "Daniel",
    "Raimundo",
    "Eduardo",
    "Fernando",
    "Gilberto",
    "Roberto",
    "Sérgio",
    "Vicente",
    "Wagner",
]
FEMALE = [
    "Ana",
    "Francisca",
    "Antônia",
    "Adriana",
    "Juliana",
    "Márcia",
    "Fernanda",
    "Patrícia",
    "Aline",
    "Sandra",
    "Luzia",
    "Rosângela",
    "Terezinha",
    "Vera",
    "Cecília",
    "Denise",
    "Helena",
    "Neusa",
]
SURNAMES = [
    "Silva",
    "Santos",
    "Oliveira",
    "Pereira",
    "Lima",
    "Carvalho",
    "Gomes",
    "Rocha",
    "Almeida",
    "Barbosa",
    "Cardoso",
    "Nascimento",
    "Araújo",
    "Mendes",
    "Freitas",
    "Cavalcanti",
    "Dias",
    "Campos",
]

# (chave, setor, faixa_idade, urgência?, diagnósticos, prescrições, sinais base, exames base, procedimento)
TEMPLATES = [
    {
        "key": "pneumonia",
        "setor": "Enfermaria 3B",
        "idade": (55, 88),
        "los": (2, 7),
        "motivo": "Pneumonia comunitária",
        "dx": [("J18.9", "Pneumonia comunitária", True), ("I10", "Hipertensão arterial sistêmica", False)],
        "rx": [
            ("ceftriaxona", "1 g", "IV", ["08:00", "20:00"]),
            ("azitromicina", "500 mg", "IV", ["08:00"]),
            ("enoxaparina", "40 mg", "SC", ["18:00"]),
            ("losartana", "50 mg", "VO", ["08:00"]),
            ("dipirona", "1 g", "IV", None),
        ],
        "sv": {"pa": (132, 80), "fc": 88, "fr": 20, "temp": 37.3, "spo2": 94},
        "lab": {"leucocitos": 12.5, "pcr": 70, "creatinina": 1.0, "potassio": 4.2},
        "nota": (
            "Pneumonia em tratamento, aceitando dieta, sem sinais de desconforto respiratório. "
            "Mantido esquema antimicrobiano."
        ),
    },
    {
        "key": "itu",
        "setor": "Enfermaria 3B",
        "idade": (60, 92),
        "los": (2, 6),
        "motivo": "Pielonefrite aguda",
        "dx": [("N10", "Pielonefrite aguda", True), ("E11.9", "Diabetes mellitus tipo 2 sem complicações", False)],
        "rx": [
            ("ceftriaxona", "1 g", "IV", ["08:00"]),
            ("metformina", "850 mg", "VO", ["08:00", "20:00"]),
            ("insulina_regular", "conforme esquema", "SC", None),
            ("paracetamol", "750 mg", "VO", None),
        ],
        "sv": {"pa": (126, 76), "fc": 84, "fr": 18, "temp": 37.2, "spo2": 96, "glicemia": 168},
        "lab": {"leucocitos": 11.8, "pcr": 64, "creatinina": 1.2, "glicose": 172},
        "nota": "Afebril há 24h, melhora da dor lombar. Urocultura em andamento. Glicemias controladas.",
    },
    {
        "key": "icc",
        "setor": "Enfermaria 3B",
        "idade": (62, 90),
        "los": (3, 9),
        "motivo": "Insuficiência cardíaca descompensada",
        "dx": [("I50.0", "Insuficiência cardíaca congestiva", True), ("I10", "Hipertensão arterial sistêmica", False)],
        "rx": [
            ("furosemida", "40 mg", "IV", ["08:00", "16:00"]),
            ("carvedilol", "12,5 mg", "VO", ["08:00", "20:00"]),
            ("enalapril", "10 mg", "VO", ["08:00", "20:00"]),
            ("enoxaparina", "40 mg", "SC", ["18:00"]),
        ],
        "sv": {"pa": (128, 78), "fc": 76, "fr": 19, "temp": 36.5, "spo2": 94},
        "lab": {"bnp": 680, "creatinina": 1.3, "potassio": 4.5, "sodio": 136},
        "nota": "Melhora progressiva da congestão, balanço hídrico negativo. Mantida diureticoterapia venosa.",
    },
    {
        "key": "avc",
        "setor": "Enfermaria 3B",
        "idade": (58, 86),
        "los": (3, 9),
        "motivo": "Acidente vascular cerebral isquêmico",
        "dx": [
            ("I63.9", "Acidente vascular cerebral isquêmico", True),
            ("I10", "Hipertensão arterial sistêmica", False),
        ],
        "rx": [
            ("aas", "100 mg", "VO", ["12:00"]),
            ("atorvastatina", "40 mg", "VO", ["20:00"]),
            ("losartana", "50 mg", "VO", ["08:00", "20:00"]),
            ("omeprazol", "20 mg", "VO", ["06:00"]),
        ],
        "sv": {"pa": (142, 84), "fc": 74, "fr": 16, "temp": 36.4, "spo2": 96},
        "lab": {"glicose": 118, "creatinina": 0.9, "potassio": 4.1},
        "nota": (
            "Déficit motor em dimídio direito estável. Em reabilitação com fisioterapia. Disfagia leve: dieta pastosa."
        ),
    },
    {
        "key": "dpoc",
        "setor": "Enfermaria 3B",
        "idade": (60, 85),
        "los": (2, 6),
        "motivo": "DPOC exacerbada",
        "dx": [("J44.1", "DPOC com exacerbação aguda", True)],
        "rx": [
            ("prednisona", "40 mg", "VO", ["08:00"]),
            ("amoxicilina_clavulanato", "875 mg", "VO", ["08:00", "20:00"]),
            ("ipratropio", "40 gotas", "inalatória", ["06:00", "12:00", "18:00", "00:00"]),
            ("salbutamol", "100 mcg (2 jatos)", "inalatória", None),
        ],
        "sv": {"pa": (134, 82), "fc": 92, "fr": 22, "temp": 36.7, "spo2": 91},
        "lab": {"leucocitos": 10.8, "pcr": 32, "glicose": 154},
        "nota": "Melhora da dispneia, sibilos esparsos. Em desmame de oxigênio suplementar.",
    },
    {
        "key": "celulite",
        "setor": "Enfermaria 3B",
        "idade": (35, 75),
        "los": (2, 6),
        "motivo": "Celulite de membro inferior",
        "dx": [("L03.1", "Celulite de membro inferior", True)],
        "rx": [
            ("cefazolina", "1 g", "IV", ["06:00", "14:00", "22:00"]),
            ("dipirona", "1 g", "IV", None),
            ("enoxaparina", "40 mg", "SC", ["18:00"]),
        ],
        "sv": {"pa": (124, 78), "fc": 82, "fr": 16, "temp": 37.4, "spo2": 98},
        "lab": {"leucocitos": 12.1, "pcr": 88},
        "nota": "Redução da área de hiperemia em relação à demarcação. Mantido antibiótico venoso.",
    },
    {
        "key": "hernia",
        "setor": "Clínica Cirúrgica 4A",
        "idade": (30, 70),
        "los": (1, 2),
        "eletiva": True,
        "motivo": "Hérnia inguinal — cirurgia eletiva",
        "dx": [("K40.9", "Hérnia inguinal", True)],
        "rx": [("dipirona", "1 g", "IV", ["00:00", "06:00", "12:00", "18:00"]), ("ondansetrona", "4 mg", "IV", None)],
        "sv": {"pa": (122, 76), "fc": 74, "fr": 15, "temp": 36.5, "spo2": 98, "dor": 2},
        "lab": {"hemoglobina": 13.8},
        "proc": "Herniorrafia inguinal com tela",
        "nota": "Pós-operatório sem intercorrências, deambulando, dor controlada.",
    },
    {
        "key": "colelitiase",
        "setor": "Clínica Cirúrgica 4A",
        "idade": (28, 68),
        "los": (1, 3),
        "eletiva": True,
        "motivo": "Colelitíase sintomática — cirurgia eletiva",
        "dx": [("K80.2", "Colelitíase sem colecistite", True)],
        "rx": [
            ("dipirona", "1 g", "IV", ["00:00", "06:00", "12:00", "18:00"]),
            ("ondansetrona", "4 mg", "IV", None),
            ("enoxaparina", "40 mg", "SC", ["18:00"]),
        ],
        "sv": {"pa": (118, 74), "fc": 76, "fr": 15, "temp": 36.6, "spo2": 98, "dor": 2},
        "lab": {"hemoglobina": 13.2, "leucocitos": 8.4},
        "proc": "Colecistectomia videolaparoscópica",
        "nota": "Pós-operatório de colecistectomia videolaparoscópica, aceitando dieta líquida, sem náuseas.",
    },
    {
        "key": "fratura",
        "setor": "Clínica Cirúrgica 4A",
        "idade": (20, 60),
        "los": (1, 4),
        "motivo": "Fratura de rádio distal",
        "dx": [("S52.5", "Fratura da extremidade distal do rádio", True)],
        "rx": [
            ("tramadol", "50 mg", "VO", ["06:00", "14:00", "22:00"]),
            ("dipirona", "1 g", "IV", ["00:00", "06:00", "12:00", "18:00"]),
        ],
        "sv": {"pa": (126, 78), "fc": 78, "fr": 16, "temp": 36.6, "spo2": 98, "dor": 3},
        "lab": {"hemoglobina": 13.5},
        "proc": "Osteossíntese de rádio distal com placa",
        "nota": "Pós-operatório de osteossíntese, perfusão distal preservada, membro elevado.",
    },
    {
        "key": "sepse",
        "setor": "UTI Adulto",
        "idade": (55, 85),
        "los": (2, 8),
        "motivo": "Sepse de foco pulmonar",
        "dx": [("A41.9", "Sepse não especificada", True), ("J18.9", "Pneumonia", False)],
        "rx": [
            ("piperacilina_tazobactam", "4,5 g", "IV", ["00:00", "06:00", "12:00", "18:00"]),
            ("noradrenalina", "0,08 mcg/kg/min", "IV", "continuo"),
            ("enoxaparina", "40 mg", "SC", ["18:00"]),
            ("pantoprazol", "40 mg", "IV", ["06:00"]),
        ],
        "sv": {"pa": (104, 62), "fc": 102, "fr": 22, "temp": 37.9, "spo2": 94},
        "lab": {"leucocitos": 17.5, "pcr": 190, "lactato": 2.6, "creatinina": 1.6},
        "nota": "Em uso de noradrenalina em dose baixa, lactato em queda. Mantido antimicrobiano de amplo espectro.",
    },
    {
        "key": "iam",
        "setor": "UTI Adulto",
        "idade": (48, 80),
        "los": (1, 4),
        "motivo": "Infarto agudo do miocárdio sem supra de ST",
        "dx": [
            ("I21.4", "Infarto agudo do miocárdio sem supradesnivelamento de ST", True),
            ("I10", "Hipertensão arterial sistêmica", False),
        ],
        "rx": [
            ("aas", "100 mg", "VO", ["12:00"]),
            ("clopidogrel", "75 mg", "VO", ["12:00"]),
            ("atorvastatina", "80 mg", "VO", ["20:00"]),
            ("metoprolol", "25 mg", "VO", ["08:00", "20:00"]),
            ("pantoprazol", "40 mg", "VO", ["06:00"]),
        ],
        "sv": {"pa": (124, 76), "fc": 68, "fr": 16, "temp": 36.4, "spo2": 97},
        "lab": {"troponina": 2.4, "creatinina": 1.0, "potassio": 4.3},
        "nota": "Sem dor torácica nas últimas 24h. Aguardando cateterismo eletivo.",
    },
    {
        "key": "cad",
        "setor": "UTI Adulto",
        "idade": (18, 45),
        "los": (1, 3),
        "motivo": "Cetoacidose diabética",
        "dx": [("E10.1", "Diabetes mellitus tipo 1 com cetoacidose", True)],
        "rx": [
            ("insulina_regular", "0,1 UI/kg/h", "IV", "continuo"),
            ("cloreto_potassio", "19,1% 10 mL", "IV", ["06:00", "18:00"]),
        ],
        "sv": {"pa": (118, 72), "fc": 96, "fr": 20, "temp": 36.8, "spo2": 98, "glicemia": 220},
        "lab": {"glicose": 240, "potassio": 3.8, "sodio": 138},
        "nota": "Acidose em resolução, gap aniônico em queda. Programada transição para insulina subcutânea.",
    },
    {
        "key": "dor_toracica",
        "setor": "Pronto-Socorro",
        "idade": (40, 75),
        "los": (0, 0),
        "motivo": "Dor torácica a esclarecer",
        "dx": [("R07.4", "Dor torácica não especificada", True)],
        "rx": [("aas", "100 mg", "VO", ["12:00"]), ("dipirona", "1 g", "IV", None)],
        "sv": {"pa": (138, 84), "fc": 82, "fr": 16, "temp": 36.5, "spo2": 97},
        "lab": {"troponina": 0.01, "creatinina": 0.9},
        "nota": "Dor torácica atípica, ECG sem alterações isquêmicas agudas. Aguardando segunda troponina.",
    },
    {
        "key": "gastroenterite",
        "setor": "Pronto-Socorro",
        "idade": (20, 70),
        "los": (0, 0),
        "motivo": "Gastroenterite aguda com desidratação",
        "dx": [("A09", "Gastroenterite infecciosa", True)],
        "rx": [("ondansetrona", "4 mg", "IV", None), ("dipirona", "1 g", "IV", None)],
        "sv": {"pa": (108, 68), "fc": 98, "fr": 18, "temp": 37.6, "spo2": 98},
        "lab": {"potassio": 3.6, "sodio": 134, "creatinina": 1.1},
        "nota": "Hidratação venosa em curso, aceitando líquidos por via oral.",
    },
]

# (template, leito) — leitos livres dos cenários roteirizados
PLAN = [
    ("pneumonia", "01"),
    ("itu", "02"),
    ("icc", "03"),
    ("avc", "04"),
    ("dpoc", "06"),
    ("celulite", "07"),
    ("pneumonia", "10"),
    ("icc", "14"),
    ("hernia", "01"),
    ("colelitiase", "02"),
    ("fratura", "05"),
    ("colelitiase", "06"),
    ("sepse", "01"),
    ("iam", "02"),
    ("cad", "03"),
    ("sepse", "05"),
    ("iam", "08"),
    ("dor_toracica", "OBS-01"),
    ("gastroenterite", "OBS-03"),
]


def _name(rng: random.Random, sex: str) -> str:
    given = rng.choice(MALE if sex == "male" else FEMALE)
    first, second = rng.sample(SURNAMES, 2)
    return f"{given} {first} {second}"


def _cpf(rng: random.Random) -> str:
    d = [rng.randint(0, 9) for _ in range(11)]
    return f"{d[0]}{d[1]}{d[2]}.{d[3]}{d[4]}{d[5]}.{d[6]}{d[7]}{d[8]}-{d[9]}{d[10]}"


def _jitter(rng: random.Random, value: float, spread: float, decimals: int = 0) -> float:
    out = value + rng.uniform(-spread, spread)
    return round(out, decimals) if decimals else int(round(out))


def generate(seed: int = 20251015) -> list[dict]:
    rng = random.Random(seed)
    templates = {t["key"]: t for t in TEMPLATES}
    out: list[dict] = []
    for n, (key, bed) in enumerate(PLAN, start=1):
        tpl = templates[key]
        sex = rng.choice(["male", "female"])
        age = rng.randint(*tpl["idade"])
        los_days = rng.randint(*tpl["los"])
        los_hours = los_days * 24 + rng.randint(3, 20) if los_days else rng.randint(4, 14)
        sid = f"bg{n:02d}"
        start = f"-{los_hours}h"
        dsl: dict = {
            "id": sid,
            "paciente": {
                "nome": _name(rng, sex),
                "sexo": sex,
                "nascimento": f"{2026 - age}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
                "cpf": _cpf(rng),
                "telefone": f"(11) 9{rng.randint(1000, 9999)}-{rng.randint(1000, 9999)}",
            },
            "internacao": {
                "inicio": start,
                "tipo": "eletiva" if tpl.get("eletiva") else "urgencia",
                "motivo": tpl["motivo"],
                "setores": [{"id": "s1", "setor": tpl["setor"], "leito": bed, "inicio": start}],
            },
            "diagnosticos": [{"cid": c, "texto": t, "principal": p, "inicio": start} for c, t, p in tpl["dx"]],
            "alergias": [],
            "encontros_anteriores": [
                {"inicio": f"-{rng.randint(20, 170)}d", "motivo": "Atendimento no pronto-socorro"}
                for _ in range(rng.choice([0, 0, 0, 1, 1, 2]))
            ],
            "prescricoes": [],
            "sinais": [],
            "exames": [],
            "escalas": [
                {
                    "t": f"-{min(los_hours - 1, 10)}h",
                    "morse": rng.choice([15, 25, 35, 45, 55]),
                    "braden": rng.randint(13, 21),
                }
            ],
            "evolucoes": [],
        }
        for i, (med, dose, via, horarios) in enumerate(tpl["rx"]):
            rx = {"id": f"m{i}", "med": med, "dose": dose, "via": via}
            if horarios is None:
                rx |= {"se_necessario": True, "condicao": "se necessário"}
            elif horarios == "continuo":
                rx |= {"frequencia": "contínuo em bomba de infusão", "administrar": False}
            else:
                rx["horarios"] = horarios
            dsl["prescricoes"].append(rx)

        base = tpl["sv"]
        step = 6
        for h in range(min(los_hours - 1, 48), 0, -step):
            sv = {
                "t": f"-{h}h",
                "pa": [_jitter(rng, base["pa"][0], 8), _jitter(rng, base["pa"][1], 5)],
                "fc": _jitter(rng, base["fc"], 6),
                "fr": _jitter(rng, base["fr"], 2),
                "temp": _jitter(rng, base["temp"], 0.3, 1),
                "spo2": min(100, _jitter(rng, base["spo2"], 1)),
            }
            for extra in ("glicemia", "dor"):
                if extra in base:
                    sv[extra] = max(0, _jitter(rng, base[extra], 2 if extra == "dor" else 20))
            dsl["sinais"].append(sv)

        for h in sorted({min(los_hours - 1, 30), min(los_hours - 1, 8)}, reverse=True):
            lab = {"t": f"-{h}h"}
            for k, v in tpl["lab"].items():
                decimals = 1 if isinstance(v, float) and v < 50 else 0
                lab[k] = _jitter(rng, v, v * 0.08, decimals) if decimals else _jitter(rng, v, v * 0.08)
            dsl["exames"].append(lab)

        if tpl.get("proc"):
            dsl["procedimentos"] = [{"t": f"-{max(los_hours - 3, 2)}h", "texto": tpl["proc"], "categoria": "cirurgia"}]
        dsl["evolucoes"] = [
            {"t": f"-{min(los_hours - 1, 9)}h", "tipo": "medica", "autor": "Equipe médica", "texto": tpl["nota"]},
            {
                "t": f"-{min(los_hours - 1, 4)}h",
                "tipo": "enfermagem",
                "autor": "Equipe de enfermagem",
                "texto": (
                    "Paciente consciente, orientado. Sinais vitais aferidos e registrados. "
                    "Sem intercorrências no período."
                ),
            },
        ]
        out.append(dsl)
    return out + _ambulatory(rng) + _after_discharge()


# Agenda ambulatorial do dia (fundo): pacientes simples para a tela de consultas parecer real
AMBULATORY = [
    (
        "-3h",
        "finished",
        "Retorno de hipertensão",
        [("I10", "Hipertensão arterial sistêmica")],
        [("losartana", "50 mg", ["08:00"])],
    ),
    ("+35m", "planned", "Primeira consulta: dor lombar", [("M54.5", "Dor lombar baixa")], []),
    (
        "+1h20m",
        "planned",
        "Retorno de hipotireoidismo",
        [("E03.9", "Hipotireoidismo")],
        [("levotiroxina", "50 mcg", ["06:00"])],
    ),
    ("+2h10m", "planned", "Acompanhamento de DPOC", [("J44.9", "Doença pulmonar obstrutiva crônica")], []),
]


def _ambulatory(rng: random.Random) -> list[dict]:
    out = []
    for n, (when, status, reason, dx, meds) in enumerate(AMBULATORY, start=1):
        sex = rng.choice(["male", "female"])
        age = rng.randint(34, 78)
        visits = [{"id": "ant", "inicio": f"-{rng.randint(80, 200)}d", "duracao": "30m", "motivo": "Consulta anterior"}]
        today = {"id": "hoje", "inicio": when, "motivo": reason, "status": status}
        if status == "finished":
            today["duracao"] = "25m"
        out.append(
            {
                "id": f"amb{n:02d}",
                "paciente": {
                    "nome": _name(rng, sex),
                    "sexo": sex,
                    "nascimento": f"{2026 - age}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
                    "cpf": _cpf(rng),
                },
                "ambulatorio": {
                    "servico": "Ambulatório de Clínica Médica",
                    "sala": f"Consultório {rng.randint(1, 6)}",
                    "profissional": "Dra. Helena Prado",
                    "consultas": [*visits, today],
                },
                "diagnosticos": [{"cid": c, "texto": t, "inicio": f"-{rng.randint(400, 3000)}d"} for c, t in dx],
                "alergias": [],
                "prescricoes": [
                    {"id": f"m{i}", "med": m, "dose": d, "via": "VO", "horarios": h, "setor": "ant", "inicio": "-300d"}
                    for i, (m, d, h) in enumerate(meds)
                ],
            }
        )
    return out


# Coorte pós-alta (fundo): pacientes que já tiveram alta, com e sem retorno, falta e reinternação. Alimenta as
# métricas da jornada (reinternação em 30 dias e retornos perdidos). (modelo, dias desde a alta, dias de internação,
# reinternação depois de N dias | None, retorno: feito | falta | agendado | nenhum)
AFTER_DISCHARGE = [
    ("icc", 150, 6, 12, "nenhum"),
    ("icc", 120, 5, 21, "nenhum"),
    ("dpoc", 100, 6, 9, "nenhum"),
    ("pneumonia", 140, 4, None, "feito"),
    ("pneumonia", 90, 3, None, "feito"),
    ("itu", 80, 4, None, "feito"),
    ("hernia", 130, 2, None, "feito"),
    ("colelitiase", 110, 2, None, "feito"),
    ("fratura", 95, 6, 25, "feito"),
    ("avc", 70, 7, None, "falta"),
    ("sepse", 60, 8, None, "nenhum"),
    ("iam", 55, 5, None, "feito"),
    ("celulite", 45, 3, None, "feito"),
    ("icc", 75, 5, 33, "feito"),
    ("dpoc", 38, 4, None, "nenhum"),
    ("pneumonia", 25, 3, None, "feito"),
    ("itu", 18, 4, None, "agendado"),
    ("hernia", 9, 2, None, "agendado"),
]


def _after_discharge(seed: int = 20260101) -> list[dict]:
    """Mesma DSL dos cenários. Usa um gerador próprio: não altera o sorteio dos demais pacientes de fundo."""
    rng = random.Random(seed)
    templates = {t["key"]: t for t in TEMPLATES}
    out = []
    for n, (key, ended, los, gap, retorno) in enumerate(AFTER_DISCHARGE, start=1):
        tpl = templates[key]
        sex = rng.choice(["male", "female"])
        age = rng.randint(*tpl["idade"])
        index = {"inicio": f"-{ended + los}d", "duracao": f"{los}d", "motivo": tpl["motivo"], "tipo": "internacao"}
        if gap is None:
            main_start, main_end, earlier = f"-{ended + los}d", f"-{ended}d", []
            last_end = ended
        else:  # reinternado: a internação principal é a segunda; a primeira vira encontro anterior (IMP)
            again = ended - gap
            main_start, main_end, earlier = f"-{again}d", f"-{again - 4}d", [index]
            last_end = again - 4
        sid = f"alt{n:02d}"
        dsl: dict = {
            "id": sid,
            "paciente": {
                "nome": _name(rng, sex),
                "sexo": sex,
                "nascimento": f"{2026 - age}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
                "cpf": _cpf(rng),
                "telefone": f"(11) 9{rng.randint(1000, 9999)}-{rng.randint(1000, 9999)}",
            },
            "internacao": {
                "inicio": main_start,
                "fim": main_end,
                "tipo": "eletiva" if tpl.get("eletiva") else "urgencia",
                "motivo": tpl["motivo"],
                "setores": [
                    {"id": "s1", "setor": tpl["setor"], "leito": f"{20 + n}", "inicio": main_start, "fim": main_end}
                ],
            },
            "encontros_anteriores": [
                *earlier,
                *(  # quem volta ao hospital costuma ter passado pelo pronto-socorro antes (componente E do LACE)
                    {"inicio": f"-{ended + los + rng.randint(15, 120)}d", "motivo": "Atendimento no pronto-socorro"}
                    for _ in range(3 if gap else rng.choice([0, 0, 1, 2]))
                ),
            ],
            "diagnosticos": [{"cid": c, "texto": t, "principal": p, "inicio": main_start} for c, t, p in tpl["dx"]],
            "alergias": [],
        }
        if retorno != "nenhum":
            when, status = {
                "feito": (f"-{max(last_end - 7, 1)}d", "fulfilled"),
                "falta": (f"-{max(last_end - 7, 1)}d", "noshow"),
                "agendado": (f"+{5 + n % 3}d", "booked"),
            }[retorno]
            dsl["agendamentos"] = [{"id": "ret", "t": when, "status": status, "motivo": "Retorno pós-alta"}]
        out.append(dsl)
    return out
