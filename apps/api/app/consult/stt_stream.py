"""Transcrição em fluxo (#11): PCM 16 kHz em blocos → texto parcial e frases fechadas.

O áudio só existe em memória (buffer da janela): nada é escrito em disco e o buffer é descartado a cada frase
fechada. Três mecanismos evitam palavra cortada ou repetida na fronteira da janela deslizante:

1. VAD por energia: o silêncio depois da fala fecha a frase (a janela recomeça limpa, sem fronteira no meio de palavra).
2. Fala contínua além de `janela_s`: a janela é transcrita, só as palavras que terminam antes da última
   `sobreposicao_s` são fechadas (o fim da janela pode ter palavra cortada) e o áudio restante, com a sobreposição,
   é transcrito de novo na janela seguinte.
3. Deduplicação pelo tempo das palavras: só entra palavra cujo ponto médio é posterior ao que já foi fechado.

O motor é plugável (`Motor`): `FakeMotor` (determinístico, usado nos testes e na demonstração, sem baixar pesos) e
`HttpMotor` (serviço compatível com /v1/audio/transcriptions, ex.: faster-whisper-server no perfil `stt` do compose).
"""

import io
import math
import wave
from array import array
from dataclasses import dataclass
from typing import Protocol

from app.config import get_settings
from app.consult import stt

TAXA = 16000  # Hz, mono, int16 little-endian
QUADRO = 0.1  # s: resolução do VAD e do motor falso
LIMIAR_ENERGIA = 0.02  # fração de 32768 (RMS) abaixo da qual o quadro é silêncio


@dataclass(frozen=True)
class Palavra:
    texto: str
    inicio: float  # s, relativos ao início da janela transcrita
    fim: float
    prob: float | None = None


class Motor(Protocol):
    def transcrever(self, pcm: bytes, *, corte_final: bool, idioma: str, vocabulario: str) -> list[Palavra]:
        """`corte_final`: o fim do áudio é um corte da janela (a fala segue), não um silêncio."""
        ...


# ---------- motor falso, determinístico ----------
# Cada palavra do vocabulário é um tom puro (frequência própria) de 0,4 s, com 0,2 s de silêncio entre palavras. O
# motor decodifica contando cruzamentos de zero: sem pesos, sem rede, e uma palavra cortada na borda não é reconhecida,
# como acontece com um Whisper real. Serve para exercitar o caminho de ponta a ponta (janela, VAD, deduplicação).
VOCABULARIO = sorted(
    set(
        "paciente toma glibenclamida de manhã refere tontura metformina diabetes dor no peito desde ontem sem febre "
        "alergia a dipirona usa insulina pressão alta hoje dois comprimidos por dia náusea e vômito tosse falta ar "
        "medico pergunta quanto tempo".split()
    )
)
_BASE_HZ, _PASSO_HZ = 400.0, 40.0
_TOM_S, _PAUSA_S, _AMP = 0.4, 0.2, 0.3


def _freq(palavra: str) -> float:
    return _BASE_HZ + _PASSO_HZ * VOCABULARIO.index(palavra)


def sintetizar(frases: list[str], pausa_frase_s: float = 0.9) -> bytes:
    """Frases (palavras do VOCABULARIO) → PCM int16. Entre frases há `pausa_frase_s` de silêncio."""
    out = array("h")
    for i, frase in enumerate(frases):
        for j, palavra in enumerate(frase.split()):
            if palavra not in VOCABULARIO:
                raise ValueError(f"Palavra fora do vocabulário do motor falso: {palavra}")
            f = _freq(palavra)
            out.extend(int(_AMP * 32767 * math.sin(2 * math.pi * f * k / TAXA)) for k in range(int(_TOM_S * TAXA)))
            if j < len(frase.split()) - 1:
                out.extend([0] * int(_PAUSA_S * TAXA))
        if i < len(frases) - 1:
            out.extend([0] * int(pausa_frase_s * TAXA))
    return out.tobytes()


def para_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(TAXA)
        w.writeframes(pcm)
    return buf.getvalue()


def de_wav(dados: bytes) -> bytes:
    with wave.open(io.BytesIO(dados), "rb") as w:
        if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, TAXA):
            raise ValueError("WAV precisa ser mono, 16 bits, 16 kHz")
        return w.readframes(w.getnframes())


def _amostras(pcm: bytes) -> array:
    a = array("h")
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    return a


def _rms(a: array, i: int, j: int) -> float:
    n = j - i
    if n <= 0:
        return 0.0
    return math.sqrt(sum(x * x for x in a[i:j]) / n) / 32768


def silencioso(a: array, i: int, j: int) -> bool:
    return _rms(a, i, j) < LIMIAR_ENERGIA


class FakeMotor:
    def transcrever(self, pcm: bytes, *, corte_final: bool, idioma: str = "pt", vocabulario: str = "") -> list[Palavra]:
        a = _amostras(pcm)
        n = len(a)
        q = int(QUADRO * TAXA)
        limiar = int(LIMIAR_ENERGIA * 32768)
        rotulos: list[str | None] = []
        for k in range(n // q):
            ativos = a[k * q : (k + 1) * q]
            fortes = sum(1 for x in ativos if abs(x) > limiar)
            if fortes < 0.5 * q:  # silêncio; quadro parcial é descartado pela frequência estimada
                rotulos.append(None)
                continue
            cruzamentos = sum(1 for x, y in zip(ativos, ativos[1:], strict=False) if (x < 0) != (y < 0))
            f = cruzamentos / (2 * QUADRO)
            melhor = min(VOCABULARIO, key=lambda p: abs(_freq(p) - f))
            rotulos.append(melhor if abs(_freq(melhor) - f) < _PASSO_HZ / 2 else None)
        palavras: list[Palavra] = []
        k = 0
        while k < len(rotulos):
            if rotulos[k] is None:
                k += 1
                continue
            j = k
            while j + 1 < len(rotulos) and rotulos[j + 1] == rotulos[k]:
                j += 1
            if j - k + 1 >= 2:
                ini, fim = k * q, (j + 1) * q
                passo = q // 10
                while ini - passo >= 0 and ini > (k - 1) * q and not silencioso(a, ini - passo, ini):
                    ini -= passo
                while fim + passo <= n and fim < (j + 2) * q and not silencioso(a, fim, fim + passo):
                    fim += passo
                cortada = n - fim < passo  # a fala vai até o fim do áudio: a palavra pode continuar na janela seguinte
                if not (corte_final and cortada):
                    palavras.append(Palavra(rotulos[k] or "", ini / TAXA, fim / TAXA, 0.99))
            k = j + 1
        return palavras


class HttpMotor:
    """Serviço compatível com /v1/audio/transcriptions com timestamps por palavra (faster-whisper-server)."""

    def transcrever(self, pcm: bytes, *, corte_final: bool, idioma: str = "pt", vocabulario: str = "") -> list[Palavra]:
        res = stt.transcribe_words(para_wav(pcm), "audio/wav", idioma, vocabulario)
        return [Palavra(p.texto, p.inicio, p.fim, p.prob) for p in res.palavras]


def motor() -> Motor | None:
    s = get_settings()
    if s.stt_engine == "fake":
        return FakeMotor()
    if s.stt_url:
        return HttpMotor()
    return None


def disponivel() -> bool:
    return stt.enabled()


# ---------- sessão de transmissão ----------
@dataclass
class Evento:
    tipo: str  # "parcial" | "final"
    texto: str
    inicio: float = 0.0  # s desde o começo da transmissão
    fim: float = 0.0


class Sessao:
    def __init__(self, engine: Motor, idioma: str = "pt-BR", vocabulario: str = "") -> None:
        s = get_settings()
        self.engine, self.idioma, self.vocabulario = engine, idioma.split("-")[0], vocabulario
        self.janela_s, self.sobreposicao_s, self.silencio_s = s.stt_janela_s, s.stt_sobreposicao_s, s.stt_silencio_s
        self.parcial_cada_s = 1.0
        self.buf = bytearray()
        self.ini = 0.0  # tempo absoluto (s) da primeira amostra de `buf`
        self.fechado_ate = 0.0  # palavras com ponto médio <= isso já foram entregues como final
        self.ultimo_parcial_s = 0.0
        self.ultimo_texto_parcial = ""
        self.bytes_total = 0

    @property
    def fim(self) -> float:
        return self.ini + len(self.buf) / 2 / TAXA

    def _silencio_final(self) -> tuple[bool, float]:
        """(há fala na janela, segundos de silêncio no fim)."""
        a = _amostras(bytes(self.buf))
        q = int(QUADRO * TAXA)
        quadros = len(a) // q
        fala, silencio, fim_da_fala = False, 0.0, True
        for k in range(quadros - 1, -1, -1):
            quieto = silencioso(a, k * q, (k + 1) * q)
            if not quieto:
                fala = True
                fim_da_fala = False
            elif fim_da_fala:
                silencio += QUADRO
        return fala, silencio

    def _novas(self, palavras: list[Palavra], limite_fim: float | None = None) -> list[Palavra]:
        out = []
        for p in palavras:
            meio = self.ini + (p.inicio + p.fim) / 2
            if meio <= self.fechado_ate:
                continue  # já entregue (sobreposição)
            if limite_fim is not None and self.ini + p.fim > limite_fim:
                continue  # perto do corte: a palavra pode estar incompleta, volta na próxima janela
            out.append(p)
        return out

    def _transcrever(self, corte_final: bool) -> list[Palavra]:
        return self.engine.transcrever(
            bytes(self.buf), corte_final=corte_final, idioma=self.idioma, vocabulario=self.vocabulario
        )

    def _evento_final(self, palavras: list[Palavra]) -> Evento:
        texto = " ".join(p.texto.strip() for p in palavras).strip()
        return Evento("final", texto, self.ini + palavras[0].inicio, self.ini + palavras[-1].fim)

    def adicionar(self, pcm: bytes) -> list[Evento]:
        self.buf.extend(pcm[: len(pcm) // 2 * 2])
        self.bytes_total += len(pcm)
        fala, silencio = self._silencio_final()
        if not fala:
            # só silêncio: não acumula janela vazia
            self.ini = self.fim
            self.buf.clear()
            return []
        eventos: list[Evento] = []
        if silencio >= self.silencio_s:
            novas = self._novas(self._transcrever(corte_final=False))
            if novas:
                eventos.append(self._evento_final(novas))
            self.fechado_ate = self.fim
            self.ini = self.fim
            self.buf.clear()
            self.ultimo_texto_parcial = ""
            return eventos
        if self.fim - self.ini >= self.janela_s:
            fim = self.fim
            novas = self._novas(self._transcrever(corte_final=True), limite_fim=fim - self.sobreposicao_s)
            if novas:
                eventos.append(self._evento_final(novas))
                self.fechado_ate = self.ini + novas[-1].fim
            corte = (
                self.fechado_ate if novas else fim - self.sobreposicao_s
            )  # a janela seguinte recomeça no fim da última palavra fechada
            descartar = int((corte - self.ini) * TAXA) * 2
            del self.buf[:descartar]
            self.ini = corte
            self.ultimo_texto_parcial = ""
            return eventos
        if self.fim - self.ultimo_parcial_s >= self.parcial_cada_s:
            self.ultimo_parcial_s = self.fim
            novas = self._novas(self._transcrever(corte_final=True))
            texto = " ".join(p.texto.strip() for p in novas).strip()
            if texto and texto != self.ultimo_texto_parcial:
                self.ultimo_texto_parcial = texto
                eventos.append(Evento("parcial", texto, self.ini + novas[0].inicio, self.ini + novas[-1].fim))
        return eventos

    def encerrar(self) -> list[Evento]:
        """Fim da transmissão: fecha o que sobrou na janela."""
        eventos: list[Evento] = []
        fala, _ = self._silencio_final()
        if fala:
            novas = self._novas(self._transcrever(corte_final=False))
            if novas:
                eventos.append(self._evento_final(novas))
        self.buf.clear()
        return eventos
