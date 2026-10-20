"""Escalas clínicas determinísticas (#27): cada ponto sai de uma resposta ou de um dado do prontuário e cita a fonte.

Duas famílias:
- questionários (IPSS, PHQ-9, GAD-7, Morse, Braden, IVCF-20): tabela de itens × opções com pontos, faixas de
  interpretação e a referência do instrumento. As respostas vêm do profissional (endpoint de escalas) ou, no IPSS e
  na classe funcional NYHA, são estimadas pela fala do paciente (cada estimativa cita `Fala/N`).
- calculadoras sobre o prontuário (CHA2DS2-VASc, IMC): leem os diagnósticos e a idade, com a ref de cada ponto.

Nada aqui chama IA nem inventa pontuação: faltou resposta, a escala sai incompleta e diz o que falta. Base de
demonstração: os pontos seguem os instrumentos publicados, mas a conduta é sempre decisão do profissional.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.consult.schemas import Segment, fala_ref
from app.consult.text import find, norm

PATIENT_SIDE = ("paciente", "acompanhante")


@dataclass(frozen=True)
class Opcao:
    rotulo: str
    pontos: int


@dataclass(frozen=True)
class Item:
    chave: str
    rotulo: str
    opcoes: dict[str, Opcao]
    grupo: str = ""  # itens do mesmo grupo somam até o teto do grupo (IVCF-20)


@dataclass(frozen=True)
class Faixa:
    minimo: int
    maximo: int
    rotulo: str
    severidade: str  # critico | atencao | info | ok


@dataclass(frozen=True)
class Escala:
    id: str
    nome: str
    fonte: str
    itens: tuple[Item, ...]
    faixas: tuple[Faixa, ...]
    maximo: int
    tetos: dict[str, int] = field(default_factory=dict)
    aviso: str = ""


@dataclass
class Ponto:
    """Uma linha da pontuação: o que contou, quantos pontos e de onde veio (resposta, fala ou recurso do prontuário)."""

    rotulo: str
    resposta: str
    pontos: int
    refs: list[str] = field(default_factory=list)
    origem: str = "resposta"  # resposta | fala | prontuario


@dataclass
class Resultado:
    id: str
    nome: str
    total: int | None
    maximo: int
    faixa: str
    severidade: str
    pontos: list[Ponto]
    fonte: str
    completo: bool = True
    faltam: list[str] = field(default_factory=list)
    aviso: str = ""
    estimada: bool = False  # alguma resposta veio da fala do paciente: o profissional confirma

    @property
    def refs(self) -> list[str]:
        return list(dict.fromkeys(r for p in self.pontos for r in p.refs))

    def resumo(self) -> str:
        if self.total is None:
            return f"{self.nome}: incompleta (faltam {len(self.faltam)} item(ns))"
        parcial = "" if self.completo else " (parcial)"
        return f"{self.nome}: {self.total} de {self.maximo} pontos{parcial} — {self.faixa}"


def _opc(*pares: tuple[str, str, int]) -> dict[str, Opcao]:
    return {k: Opcao(r, p) for k, r, p in pares}


def _escala_0_3() -> dict[str, Opcao]:
    return _opc(
        ("0", "Nenhuma vez", 0),
        ("1", "Vários dias", 1),
        ("2", "Mais da metade dos dias", 2),
        ("3", "Quase todos os dias", 3),
    )


# ---------- PHQ-9 e GAD-7 ----------
PHQ9 = Escala(
    id="phq9",
    nome="PHQ-9 (depressão)",
    fonte="Kroenke K, Spitzer RL, Williams JB. The PHQ-9. J Gen Intern Med 2001; validação brasileira: "
    "Santos IS et al., Cad Saúde Pública 2013",
    itens=tuple(
        Item(k, r, _escala_0_3())
        for k, r in (
            ("interesse", "Pouco interesse ou prazer em fazer as coisas"),
            ("humor", "Sentir-se para baixo, deprimido(a) ou sem perspectiva"),
            ("sono", "Dificuldade para dormir, ou dormir demais"),
            ("energia", "Sentir-se cansado(a) ou com pouca energia"),
            ("apetite", "Falta de apetite ou comer demais"),
            ("culpa", "Sentir-se mal consigo mesmo(a), um fracasso ou que decepcionou a família"),
            ("concentracao", "Dificuldade para se concentrar"),
            ("lentidao", "Lentidão (ou agitação) percebida pelos outros"),
            ("ideacao", "Pensamentos de que seria melhor estar morto(a) ou de se ferir de alguma maneira"),
        )
    ),
    faixas=(
        Faixa(0, 4, "sintomas mínimos", "ok"),
        Faixa(5, 9, "depressão leve", "info"),
        Faixa(10, 14, "depressão moderada", "atencao"),
        Faixa(15, 19, "depressão moderadamente grave", "atencao"),
        Faixa(20, 27, "depressão grave", "critico"),
    ),
    maximo=27,
    aviso="Rastreio, não diagnóstico. O item 9 (ideação) positivo pede avaliação de risco no mesmo atendimento.",
)

GAD7 = Escala(
    id="gad7",
    nome="GAD-7 (ansiedade)",
    fonte="Spitzer RL et al. A brief measure for assessing generalized anxiety disorder. Arch Intern Med 2006; "
    "adaptação brasileira: Moreno AL et al., Trends Psychiatry Psychother 2016",
    itens=tuple(
        Item(k, r, _escala_0_3())
        for k, r in (
            ("nervosismo", "Sentir-se nervoso(a), ansioso(a) ou muito tenso(a)"),
            ("preocupacao_controle", "Não ser capaz de impedir ou controlar as preocupações"),
            ("preocupacao_excesso", "Preocupar-se muito com diversas coisas"),
            ("relaxar", "Dificuldade para relaxar"),
            ("inquietacao", "Ficar tão agitado(a) que se torna difícil permanecer sentado(a)"),
            ("irritabilidade", "Ficar facilmente aborrecido(a) ou irritado(a)"),
            ("medo", "Sentir medo como se algo terrível fosse acontecer"),
        )
    ),
    faixas=(
        Faixa(0, 4, "ansiedade mínima", "ok"),
        Faixa(5, 9, "ansiedade leve", "info"),
        Faixa(10, 14, "ansiedade moderada", "atencao"),
        Faixa(15, 21, "ansiedade grave", "critico"),
    ),
    maximo=21,
    aviso="Rastreio, não diagnóstico. Pontuação ≥ 10 é o ponto de corte usual para avaliação clínica.",
)

# ---------- IPSS ----------
_FREQ = _opc(
    ("0", "Nenhuma vez", 0),
    ("1", "Menos de 1 vez em cada 5", 1),
    ("2", "Menos da metade das vezes", 2),
    ("3", "Cerca de metade das vezes", 3),
    ("4", "Mais da metade das vezes", 4),
    ("5", "Quase sempre", 5),
)
IPSS = Escala(
    id="ipss",
    nome="IPSS (sintomas urinários)",
    fonte="Barry MJ et al. The AUA Symptom Index. J Urol 1992 (IPSS, International Prostate Symptom Score); "
    "versão em português validada: Barry MJ et al., 1995, e SBU",
    itens=(
        Item("esvaziamento", "Sensação de não esvaziar completamente a bexiga", _FREQ),
        Item("frequencia", "Voltou a urinar em menos de 2 horas", _FREQ),
        Item("intermitencia", "Jato que para e recomeça várias vezes", _FREQ),
        Item("urgencia", "Dificuldade para adiar a vontade de urinar", _FREQ),
        Item("jato_fraco", "Jato urinário fraco", _FREQ),
        Item("esforco", "Precisou fazer força para começar a urinar", _FREQ),
        Item(
            "nocturia",
            "Vezes que levanta à noite para urinar",
            _opc(
                ("0", "Nenhuma", 0),
                ("1", "1 vez", 1),
                ("2", "2 vezes", 2),
                ("3", "3 vezes", 3),
                ("4", "4 vezes", 4),
                ("5", "5 ou mais vezes", 5),
            ),
        ),
    ),
    faixas=(
        Faixa(0, 7, "sintomas leves", "ok"),
        Faixa(8, 19, "sintomas moderados", "atencao"),
        Faixa(20, 35, "sintomas graves", "critico"),
    ),
    maximo=35,
    aviso="Pergunta de qualidade de vida (0 a 6) não entra na soma. Pontuação ≥ 8 costuma motivar tratamento.",
)

# ---------- Morse ----------
MORSE = Escala(
    id="morse",
    nome="Escala de Morse (risco de queda)",
    fonte="Morse JM, Morse RM, Tylko SJ. Development of a scale to identify the fall-prone patient. Can J Aging 1989; "
    "adaptação brasileira: Urbanetto JS et al., Rev Esc Enferm USP 2013",
    itens=(
        Item(
            "historico_queda",
            "Histórico de queda (agora ou nos últimos 3 meses)",
            _opc(("nao", "Não", 0), ("sim", "Sim", 25)),
        ),
        Item("diagnostico_secundario", "Diagnóstico secundário", _opc(("nao", "Não", 0), ("sim", "Sim", 15))),
        Item(
            "auxilio_deambulacao",
            "Auxílio na deambulação",
            _opc(
                ("nenhum", "Nenhum, acamado ou auxiliado por profissional", 0),
                ("muleta", "Muleta, bengala ou andador", 15),
                ("mobiliario", "Apoia-se em móveis", 30),
            ),
        ),
        Item("terapia_iv", "Terapia endovenosa ou dispositivo salinizado", _opc(("nao", "Não", 0), ("sim", "Sim", 20))),
        Item(
            "marcha",
            "Marcha",
            _opc(
                ("normal", "Normal, acamado ou cadeira de rodas", 0),
                ("fraca", "Fraca", 10),
                ("comprometida", "Comprometida", 20),
            ),
        ),
        Item(
            "estado_mental",
            "Estado mental",
            _opc(
                ("orientado", "Orientado/capaz quanto à sua capacidade", 0),
                ("esquece", "Superestima ou esquece limitações", 15),
            ),
        ),
    ),
    faixas=(
        Faixa(0, 24, "risco baixo", "ok"),
        Faixa(25, 44, "risco moderado", "atencao"),
        Faixa(45, 125, "risco elevado", "critico"),
    ),
    maximo=125,
)

# ---------- Braden ----------
BRADEN = Escala(
    id="braden",
    nome="Escala de Braden (lesão por pressão)",
    fonte="Bergstrom N et al. The Braden Scale for Predicting Pressure Sore Risk. Nurs Res 1987; "
    "versão brasileira: Paranhos WY, Santos VLCG. Rev Esc Enferm USP 1999",
    itens=(
        Item(
            "percepcao_sensorial",
            "Percepção sensorial",
            _opc(
                ("1", "Completamente limitado", 1),
                ("2", "Muito limitado", 2),
                ("3", "Levemente limitado", 3),
                ("4", "Nenhuma limitação", 4),
            ),
        ),
        Item(
            "umidade",
            "Umidade",
            _opc(
                ("1", "Completamente molhado", 1),
                ("2", "Muito molhado", 2),
                ("3", "Ocasionalmente molhado", 3),
                ("4", "Raramente molhado", 4),
            ),
        ),
        Item(
            "atividade",
            "Atividade",
            _opc(
                ("1", "Acamado", 1),
                ("2", "Confinado à cadeira", 2),
                ("3", "Anda ocasionalmente", 3),
                ("4", "Anda frequentemente", 4),
            ),
        ),
        Item(
            "mobilidade",
            "Mobilidade",
            _opc(
                ("1", "Completamente imóvel", 1),
                ("2", "Muito limitado", 2),
                ("3", "Levemente limitado", 3),
                ("4", "Nenhuma limitação", 4),
            ),
        ),
        Item(
            "nutricao",
            "Nutrição",
            _opc(
                ("1", "Muito pobre", 1),
                ("2", "Provavelmente inadequada", 2),
                ("3", "Adequada", 3),
                ("4", "Excelente", 4),
            ),
        ),
        Item(
            "friccao",
            "Fricção e cisalhamento",
            _opc(("1", "Problema", 1), ("2", "Problema potencial", 2), ("3", "Sem problema aparente", 3)),
        ),
    ),
    faixas=(
        Faixa(6, 9, "risco muito alto", "critico"),
        Faixa(10, 12, "risco alto", "critico"),
        Faixa(13, 14, "risco moderado", "atencao"),
        Faixa(15, 18, "risco baixo", "info"),
        Faixa(19, 23, "sem risco", "ok"),
    ),
    maximo=23,
    aviso="Quanto menor a pontuação, maior o risco.",
)


# ---------- IVCF-20 ----------
def _sn(pontos: int) -> dict[str, Opcao]:
    return _opc(("nao", "Não", 0), ("sim", "Sim", pontos))


IVCF20 = Escala(
    id="ivcf20",
    nome="IVCF-20 (vulnerabilidade clínico-funcional)",
    fonte="Moraes EN et al. Índice de Vulnerabilidade Clínico-Funcional-20. Rev Saúde Pública 2016; "
    "Ministério da Saúde (Caderneta da Pessoa Idosa)",
    itens=(
        Item(
            "idade",
            "Idade",
            _opc(("60-74", "60 a 74 anos", 0), ("75-84", "75 a 84 anos", 1), ("85+", "85 anos ou mais", 3)),
        ),
        Item(
            "saude",
            "Auto-percepção da saúde",
            _opc(("boa", "Excelente, muito boa ou boa", 0), ("regular_ruim", "Regular ou ruim", 1)),
        ),
        Item("avdi_dinheiro", "Deixou de controlar o dinheiro por causa da saúde", _sn(4), "avd_instrumental"),
        Item("avdi_compras", "Deixou de fazer compras por causa da saúde", _sn(4), "avd_instrumental"),
        Item(
            "avdi_domestico",
            "Deixou de fazer pequenos trabalhos domésticos por causa da saúde",
            _sn(4),
            "avd_instrumental",
        ),
        Item("avdb_banho", "Deixou de tomar banho sozinho(a)", _sn(6)),
        Item("cog_familiar", "Familiar ou amigo disse que está esquecido(a)", _sn(1), "cognicao"),
        Item("cog_piora", "O esquecimento está piorando nos últimos meses", _sn(1), "cognicao"),
        Item("cog_atividade", "O esquecimento impede atividades do cotidiano", _sn(2), "cognicao"),
        Item("humor_desanimo", "No último mês ficou desanimado(a), triste ou desesperançoso(a)", _sn(2), "humor"),
        Item("humor_interesse", "No último mês perdeu o interesse ou prazer em atividades", _sn(2), "humor"),
        Item("mob_alcance", "Incapaz de elevar os braços acima do ombro", _sn(1)),
        Item("mob_pinca", "Incapaz de manusear ou segurar pequenos objetos", _sn(1)),
        Item(
            "mob_peso",
            "Perda de peso não intencional (4,5 kg ou 5% no ano, ou 6 kg em 6 meses)",
            _sn(2),
            "capacidade_aerobica",
        ),
        Item("mob_imc", "IMC menor que 22 kg/m²", _sn(2), "capacidade_aerobica"),
        Item("mob_panturrilha", "Circunferência da panturrilha menor que 31 cm", _sn(2), "capacidade_aerobica"),
        Item("mob_marcha4m", "Tempo na marcha de 4 metros maior que 5 segundos", _sn(2), "capacidade_aerobica"),
        Item("marcha_dificuldade", "Dificuldade para caminhar que impede atividades do cotidiano", _sn(2), "marcha"),
        Item("marcha_quedas", "Duas ou mais quedas no último ano", _sn(2), "marcha"),
        Item("continencia", "Perda involuntária de urina ou fezes", _sn(2)),
        Item("visao", "Problema de visão que impede atividades do cotidiano", _sn(2), "comunicacao"),
        Item("audicao", "Problema de audição que impede atividades do cotidiano", _sn(2), "comunicacao"),
        Item("comorbidade_doencas", "Cinco ou mais doenças crônicas", _sn(4), "comorbidade"),
        Item("comorbidade_remedios", "Uso regular de cinco ou mais medicamentos", _sn(4), "comorbidade"),
        Item("comorbidade_internacao", "Internação nos últimos seis meses", _sn(4), "comorbidade"),
    ),
    faixas=(
        Faixa(0, 6, "baixo risco de vulnerabilidade (idoso robusto)", "ok"),
        Faixa(7, 14, "risco moderado de vulnerabilidade (idoso em risco de fragilização)", "atencao"),
        Faixa(15, 40, "alto risco de vulnerabilidade (idoso frágil)", "critico"),
    ),
    tetos={
        "avd_instrumental": 4,
        "cognicao": 4,
        "humor": 4,
        "capacidade_aerobica": 4,
        "marcha": 2,
        "comunicacao": 4,
        "comorbidade": 4,
    },
    maximo=40,
    aviso="Os itens de peso e panturrilha pedem medida na consulta. Confira os pesos com o instrumento oficial do MS.",
)

QUESTIONARIOS: dict[str, Escala] = {e.id: e for e in (IPSS, PHQ9, GAD7, MORSE, BRADEN, IVCF20)}


def _faixa(escala: Escala, total: int) -> Faixa:
    for f in escala.faixas:
        if f.minimo <= total <= f.maximo:
            return f
    return escala.faixas[-1]


def calcular(escala_id: str, respostas: dict[str, str], refs: dict[str, list[str]] | None = None) -> Resultado:
    """Soma os pontos das respostas (`item -> opção`). `refs[item]` cita de onde veio cada resposta (fala, registro)."""
    escala = QUESTIONARIOS[escala_id]
    refs = refs or {}
    pontos: list[Ponto] = []
    faltam: list[str] = []
    por_grupo: dict[str, int] = {}
    total = 0
    for item in escala.itens:
        chave = respostas.get(item.chave)
        opcao = item.opcoes.get(str(chave)) if chave is not None else None
        if opcao is None:
            faltam.append(item.rotulo)
            continue
        origem = "fala" if any(r.startswith("Fala/") for r in refs.get(item.chave, [])) else "resposta"
        p = opcao.pontos
        if item.grupo:
            teto = escala.tetos.get(item.grupo, 99)
            usado = por_grupo.get(item.grupo, 0)
            p = max(0, min(p, teto - usado))
            por_grupo[item.grupo] = usado + p
        total += p
        pontos.append(Ponto(item.rotulo, opcao.rotulo, p, list(refs.get(item.chave, [])), origem))
    completo = not faltam
    faixa = _faixa(escala, total) if completo else None
    aviso = escala.aviso
    if escala.id == "phq9" and respostas.get("ideacao") not in (None, "0"):
        aviso = "ATENÇÃO: item 9 (ideação) positivo — avaliar risco de suicídio hoje. " + aviso
    return Resultado(
        id=escala.id,
        nome=escala.nome,
        total=total if completo else None,
        maximo=escala.maximo,
        faixa=faixa.rotulo if faixa else "incompleta",
        severidade=("critico" if aviso.startswith("ATENÇÃO") else faixa.severidade) if faixa else "info",
        pontos=pontos,
        fonte=escala.fonte,
        completo=completo,
        faltam=faltam,
        aviso=aviso,
        estimada=any(p.origem == "fala" for p in pontos),
    )


def validar_respostas(escala_id: str, respostas: dict[str, str]) -> list[str]:
    """Erros de uma resposta enviada: escala, item ou opção que não existem."""
    escala = QUESTIONARIOS.get(escala_id)
    if escala is None:
        return [f"Escala desconhecida: {escala_id}"]
    itens = {i.chave: i for i in escala.itens}
    erros = []
    for chave, valor in respostas.items():
        if chave not in itens:
            erros.append(f"Item desconhecido em {escala_id}: {chave}")
        elif str(valor) not in itens[chave].opcoes:
            erros.append(f"Opção inválida em {chave}: {valor} (use {', '.join(itens[chave].opcoes)})")
    return erros


# ---------- CHA2DS2-VASc ----------
FONTE_CHA2DS2 = (
    "Lip GYH et al. Chest 2010;137:263-72 (CHA2DS2-VASc); ESC Guidelines for AF 2020; Diretriz Brasileira de "
    "Fibrilação Atrial (SBC)"
)
_IC = ("I50", "I11.0", "I13.0", "I13.2", "I42")
_HAS = ("I10", "I11", "I12", "I13", "I15")
_DM = ("E10", "E11", "E12", "E13", "E14")
_AVC = ("I63", "I64", "G45", "I74", "I69.3")
_VASC = ("I21", "I22", "I25.2", "I70", "I73.9", "I71")


def tem_fa(conditions: list[tuple[str, str, dict]]) -> bool:
    return any(icd.startswith("I48") for icd, _, _ in conditions)


def cha2ds2_vasc(
    idade: int | None, feminino: bool, conditions: list[tuple[str, str, dict]], patient_ref: str
) -> Resultado:
    """Escore sobre o prontuário. `conditions`: (CID, texto, recurso FHIR) dos diagnósticos ativos."""

    def hit(prefixos: tuple[str, ...]) -> tuple[str, str] | None:
        for icd, texto, res in conditions:
            if icd.startswith(prefixos):
                return texto, f"Condition/{res['id']}"
        return None

    pontos: list[Ponto] = []

    def add(rotulo: str, pontos_: int, achado: tuple[str, str] | None, sem: str = "Não consta") -> None:
        if achado:
            pontos.append(Ponto(rotulo, f"{achado[0]} ({achado[1].split('/')[0]})", pontos_, [achado[1]], "prontuario"))
        else:
            pontos.append(Ponto(rotulo, sem, 0, [], "prontuario"))

    add("C — Insuficiência cardíaca", 1, hit(_IC))
    add("H — Hipertensão", 1, hit(_HAS))
    idade_ = idade or 0
    if idade_ >= 75:
        pontos.append(Ponto("A2 — Idade ≥ 75 anos", f"{idade_} anos", 2, [patient_ref], "prontuario"))
    else:
        pontos.append(Ponto("A2 — Idade ≥ 75 anos", f"{idade_} anos", 0, [], "prontuario"))
    add("D — Diabetes mellitus", 1, hit(_DM))
    add("S2 — AVC, AIT ou tromboembolismo prévio", 2, hit(_AVC))
    add("V — Doença vascular (IAM prévio, DAP, placa aórtica)", 1, hit(_VASC))
    if 65 <= idade_ < 75:
        pontos.append(Ponto("A — Idade 65 a 74 anos", f"{idade_} anos", 1, [patient_ref], "prontuario"))
    else:
        pontos.append(Ponto("A — Idade 65 a 74 anos", f"{idade_} anos", 0, [], "prontuario"))
    if feminino:
        pontos.append(Ponto("Sc — Sexo feminino", "Feminino", 1, [patient_ref], "prontuario"))
    else:
        pontos.append(Ponto("Sc — Sexo feminino", "Masculino", 0, [], "prontuario"))
    total = sum(p.pontos for p in pontos)
    limite = 3 if feminino else 2
    if total >= limite:
        faixa, sev = "anticoagulação oral recomendada (se não houver contraindicação)", "critico"
    elif total == limite - 1:
        faixa, sev = "considerar anticoagulação oral (decisão compartilhada)", "atencao"
    else:
        faixa, sev = "baixo risco tromboembólico: anticoagulação em geral não indicada", "ok"
    return Resultado(
        id="cha2ds2_vasc",
        nome="CHA2DS2-VASc (risco tromboembólico na FA)",
        total=total,
        maximo=9,
        faixa=faixa,
        severidade=sev,
        pontos=pontos,
        fonte=FONTE_CHA2DS2,
        aviso="Conferir também o risco de sangramento (HAS-BLED) antes de decidir.",
    )


# ---------- IMC ----------
FONTE_IMC = "OMS: Obesity: preventing and managing the global epidemic (2000); Lipschitz DA 1994 e SISVAN/MS (idosos)"


def imc(peso_kg: float, altura_cm: float, idade: int | None, peso_ref: str = "", altura_ref: str = "") -> Resultado:
    """IMC do adulto (OMS) e do idoso (Lipschitz/SISVAN). Criança e adolescente pedem a curva de IMC por idade da OMS,
    cujas tabelas (LMS) não estão embarcadas: devolve o IMC sem classificar."""
    valor = round(peso_kg / ((altura_cm / 100) ** 2), 1)
    pontos = [Ponto("Peso", f"{peso_kg:g} kg", 0, [peso_ref] if peso_ref else [], "prontuario")]
    pontos.append(Ponto("Altura", f"{altura_cm:g} cm", 0, [altura_ref] if altura_ref else [], "resposta"))
    if idade is not None and idade < 19:
        return Resultado(
            "imc",
            "IMC",
            None,
            0,
            "classificar pela curva de IMC por idade da OMS",
            "info",
            pontos,
            FONTE_IMC,
            aviso=f"IMC {str(valor).replace('.', ',')} kg/m². Criança e adolescente: usar o percentil/escore z da OMS.",
        )
    if idade is not None and idade >= 60:
        faixa, sev = (
            ("baixo peso (< 22)", "atencao")
            if valor < 22
            else ("eutrofia (22 a 27)", "ok")
            if valor <= 27
            else ("sobrepeso (> 27)", "atencao")
        )
    else:
        faixa, sev = (
            ("baixo peso (< 18,5)", "atencao")
            if valor < 18.5
            else ("eutrofia (18,5 a 24,9)", "ok")
            if valor < 25
            else ("sobrepeso (25 a 29,9)", "atencao")
            if valor < 30
            else ("obesidade grau I (30 a 34,9)", "atencao")
            if valor < 35
            else ("obesidade grau II (35 a 39,9)", "critico")
            if valor < 40
            else ("obesidade grau III (≥ 40)", "critico")
        )
    return Resultado(
        "imc", "IMC", None, 0, faixa, sev, pontos, FONTE_IMC, aviso=f"IMC {str(valor).replace('.', ',')} kg/m²"
    )


# ---------- estimativas pela fala ----------
_FREQ_WORDS = (
    ("5", ("quase sempre", "sempre", "toda vez", "todas as vezes", "quase toda vez")),
    ("4", ("mais da metade", "na maioria das vezes", "maioria das vezes", "quase toda hora")),
    ("2", ("menos da metade", "as vezes", "de vez em quando", "uma ou outra vez")),
    ("3", ("metade das vezes", "metade", "umas cinco de dez", "meio a meio")),
    ("1", ("raramente", "dificilmente", "quase nunca", "menos de uma vez")),
    ("0", ("nunca", "nenhuma vez", "nao tenho", "nao sinto", "de jeito nenhum")),
)
_IPSS_TRIGGERS: dict[str, tuple[str, ...]] = {
    "esvaziamento": (
        "nao esvazia",
        "nao esvaziou",
        "esvaziar a bexiga",
        "esvaziar completamente",
        "sobra urina",
        "fica urina",
    ),
    "frequencia": (
        "urino de novo",
        "volto a urinar",
        "volta a urinar",
        "urino toda hora",
        "urinar toda hora",
        "duas em duas horas",
        "de duas em duas horas",
    ),
    "intermitencia": ("jato para e", "para e volta", "para e recomeca", "interrompe", "jato corta", "pinga e para"),
    "urgencia": ("urgencia", "nao consigo segurar", "nao da tempo", "nao aguento segurar", "dificil segurar"),
    "jato_fraco": ("jato fraco", "jato fino", "jato devagar", "fraco o jato"),
    "esforco": ("faco forca", "fazer forca", "forcar para urinar", "forco para urinar", "forco pra urinar"),
}
_NUM_WORDS = {"uma": 1, "um": 1, "duas": 2, "dois": 2, "tres": 3, "quatro": 4, "cinco": 5, "seis": 5, "sete": 5}


def _freq_pontos(texto_norm: str) -> str | None:
    for chave, palavras in _FREQ_WORDS:
        if find(texto_norm, palavras):
            return chave
    return None


def _nocturia(texto_norm: str) -> str | None:
    if "noite" not in texto_norm and "madrugada" not in texto_norm and "dormindo" not in texto_norm:
        return None
    if re.search(r"\bnao (?:levanto|acordo|urino)\b|\bnenhuma vez\b|\bnunca (?:levanto|acordo)\b", texto_norm):
        return "0"
    m = re.search(r"\b(\d|uma|um|duas|dois|tres|quatro|cinco|seis|sete)\s+(?:vez|vezes)\b", texto_norm)
    if not m:
        return None
    n = int(m.group(1)) if m.group(1).isdigit() else _NUM_WORDS[m.group(1)]
    return str(min(n, 5))


def ipss_pela_fala(segments: list[Segment]) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Itens do IPSS que o paciente respondeu falando (na mesma fala ou na resposta à pergunta do profissional).

    Estimativa por palavras de frequência ("quase sempre", "metade das vezes"): o profissional confirma ou corrige
    pelo endpoint de escalas. Cada resposta cita a(s) fala(s) de onde saiu.
    """
    respostas: dict[str, str] = {}
    refs: dict[str, list[str]] = {}
    for i, s in enumerate(segments):
        texto = norm(s.texto)
        if s.falante in PATIENT_SIDE:
            reply, source = s, [s]
        else:
            nxt = next((x for x in segments[i + 1 : i + 3] if x.falante in PATIENT_SIDE), None)
            if nxt is None:
                continue
            reply, source = nxt, [s, nxt]
        resp = norm(reply.texto)
        for chave, gatilhos in _IPSS_TRIGGERS.items():
            if chave in respostas:
                continue
            em_pergunta = find(texto, gatilhos) is not None
            if not em_pergunta:
                continue
            if s.falante in PATIENT_SIDE:
                resp_text = texto
            else:
                resp_text = resp
            valor = _freq_pontos(resp_text)
            if valor is None and s.falante in PATIENT_SIDE:
                valor = "3" if "as vezes" in resp_text else None
            if valor is not None:
                respostas[chave] = valor
                refs[chave] = [fala_ref(x.n) for x in source]
        if "nocturia" not in respostas:
            noct_text = texto if s.falante in PATIENT_SIDE else resp
            if s.falante in PATIENT_SIDE or "noite" in texto:
                valor = _nocturia(noct_text)
                if valor is not None:
                    respostas["nocturia"] = valor
                    refs["nocturia"] = [fala_ref(x.n) for x in source]
    return respostas, refs


# ---------- NYHA ----------
FONTE_NYHA = (
    "The Criteria Committee of the New York Heart Association (1994); Diretriz Brasileira de Insuficiência Cardíaca "
    "Crônica (SBC)"
)
NYHA_CLASSES = {
    "I": "Sem limitação: a atividade física habitual não causa dispneia, cansaço ou palpitação.",
    "II": "Limitação leve: confortável em repouso; a atividade habitual (ex.: subir escada) causa sintomas.",
    "III": "Limitação acentuada: confortável em repouso; esforços menores que os habituais causam sintomas.",
    "IV": "Sintomas em repouso ou aos mínimos esforços; incapaz de qualquer atividade sem desconforto.",
}
_DISPNEIA = ("falta de ar", "cansaco", "canso", "folego", "ofegante", "sem ar", "dispneia", "cansad")
_NYHA_WORDS = (
    (
        "IV",
        ("em repouso", "parada", "parado", "deitada", "deitado", "sentada", "sentado", "para falar", "ate pra comer"),
    ),
    (
        "III",
        (
            "andar no plano",
            "caminhar no plano",
            "no plano",
            "tomar banho",
            "tomando banho",
            "me vestir",
            "me vestindo",
            "andar pouco",
            "poucos passos",
            "uma quadra",
            "meia quadra",
            "pequenos esforcos",
            "varrer a casa",
        ),
    ),
    (
        "II",
        (
            "subir escada",
            "subir uma escada",
            "subir a escada",
            "subir escadas",
            "lance de escada",
            "ladeira",
            "esforco moderado",
            "correr",
            "subir rampa",
            "carregar peso",
            "esforco",
        ),
    ),
)
_NYHA_I = ("nao tenho falta de ar", "nao canso", "sem falta de ar", "faco tudo sem", "nao sinto falta de ar")


def nyha_pela_fala(segments: list[Segment]) -> tuple[str | None, list[str]]:
    """Classe funcional estimada pela fala do paciente: a pior classe que ele descreve, com as falas de origem."""
    ordem = {"I": 1, "II": 2, "III": 3, "IV": 4}
    melhor: str | None = None
    refs: list[str] = []
    for s in segments:
        if s.falante not in PATIENT_SIDE:
            continue
        t = norm(s.texto)
        if find(t, _NYHA_I):
            classe = "I"
        elif find(t, _DISPNEIA):
            classe = next((c for c, palavras in _NYHA_WORDS if find(t, palavras)), None)
        else:
            classe = None
        if classe is None:
            continue
        if melhor is None or ordem[classe] > ordem[melhor]:
            melhor, refs = classe, [fala_ref(s.n)]
        elif classe == melhor:
            refs.append(fala_ref(s.n))
    return melhor, refs


def nyha(classe: str, refs: list[str], origem: str = "fala") -> Resultado:
    return Resultado(
        id="nyha",
        nome="Classe funcional NYHA",
        total=None,
        maximo=0,
        faixa=f"classe {classe} — {NYHA_CLASSES[classe]}",
        severidade={"I": "ok", "II": "info", "III": "atencao", "IV": "critico"}[classe],
        pontos=[Ponto("Classe funcional descrita", f"NYHA {classe}", 0, refs, origem)],
        fonte=FONTE_NYHA,
        aviso="Classe estimada pela descrição do paciente: o profissional confirma." if origem == "fala" else "",
        estimada=origem == "fala",
    )


def ids() -> frozenset[str]:
    """Escalas que um pacote de especialidade pode listar."""
    return frozenset(QUESTIONARIOS) | {"cha2ds2_vasc", "nyha", "imc"}


def catalogo() -> list[dict]:
    """Escalas em forma de formulário (itens, opções com pontos, faixas e fonte) para a tela e para a documentação."""
    out = [
        {
            "id": e.id,
            "nome": e.nome,
            "fonte": e.fonte,
            "maximo": e.maximo,
            "aviso": e.aviso,
            "itens": [
                {
                    "chave": i.chave,
                    "rotulo": i.rotulo,
                    "opcoes": [{"valor": k, "rotulo": o.rotulo, "pontos": o.pontos} for k, o in i.opcoes.items()],
                }
                for i in e.itens
            ],
            "faixas": [{"de": f.minimo, "ate": f.maximo, "rotulo": f.rotulo} for f in e.faixas],
        }
        for e in QUESTIONARIOS.values()
    ]
    out.append(
        {
            "id": "nyha",
            "nome": "Classe funcional NYHA",
            "fonte": FONTE_NYHA,
            "itens": [
                {
                    "chave": "classe",
                    "rotulo": "Classe",
                    "opcoes": [{"valor": k, "rotulo": v} for k, v in NYHA_CLASSES.items()],
                }
            ],
        }
    )
    return out
