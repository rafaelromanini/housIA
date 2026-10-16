"""Trilha sintetizada do vídeo /brag (Ré maior, 100 BPM, I–V–vi–IV) + efeitos no mesmo tom. Só numpy."""

from pathlib import Path

import numpy as np

SR = 48000
DUR = 24.0
N = int(SR * DUR)
BEAT = 0.6  # 100 BPM
rng = np.random.default_rng(7)
t_all = np.arange(N) / SR


def hz(midi):
    return 440.0 * 2 ** ((midi - 69) / 12)


def lowpass(x, cutoff):
    """Passa-baixa por FFT (estático, rolloff suave)."""
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / SR)
    X *= 1 / np.sqrt(1 + (f / cutoff) ** 4)
    return np.fft.irfft(X, len(x))


def highpass(x, cutoff):
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / SR)
    X *= 1 / np.sqrt(1 + (cutoff / np.maximum(f, 1e-3)) ** 4)
    return np.fft.irfft(X, len(x))


def bandpass(x, lo, hi):
    return highpass(lowpass(x, hi), lo)


def place(buf, sig, at, gain=1.0, pan=0.0):
    i = int(at * SR)
    if i >= N:
        return
    sig = sig[: N - i]
    left = np.cos((pan + 1) * np.pi / 4)
    right = np.sin((pan + 1) * np.pi / 4)
    buf[0, i : i + len(sig)] += sig * gain * left * np.sqrt(2)
    buf[1, i : i + len(sig)] += sig * gain * right * np.sqrt(2)


def adsr(n, a, r, sustain_level=1.0):
    env = np.ones(n) * sustain_level
    na, nr = int(a * SR), int(r * SR)
    env[:na] = np.linspace(0, sustain_level, na) if na else env[:na]
    if nr:
        env[-nr:] *= np.linspace(1, 0, nr) ** 2
    return env


def soft_tone(freq, dur, harmonics=6, tilt=2.0, detune=0.0):
    t = np.arange(int(dur * SR)) / SR
    out = np.zeros_like(t)
    for k in range(1, harmonics + 1):
        out += np.sin(2 * np.pi * freq * (1 + detune) * k * t + rng.uniform(0, 6.28)) / k**tilt
    return out


music = np.zeros((2, N))
sfx = np.zeros((2, N))

# Ré maior: D(62) A(57) Bm(59) G(55) — vozes do pad em posição fechada
CHORDS = {
    "D": [62, 66, 69, 74],
    "A": [61, 64, 69, 73],
    "Bm": [62, 66, 71, 74],
    "G": [62, 67, 71, 74],
}
BASS = {"D": 38, "A": 33, "Bm": 35, "G": 31}
ARP = {"D": [74, 78, 81, 78], "A": [73, 76, 81, 76], "Bm": [74, 78, 83, 78], "G": [74, 79, 83, 79]}

# --- Cena 1 (0–4,2 s): tensão contida — pad em Si menor filtrado + bipe de monitor no tom
intro = np.zeros(int(4.4 * SR))
for m in [47, 59, 62, 66]:
    intro += soft_tone(hz(m), 4.4, harmonics=5, tilt=2.2) * 0.5
    intro += soft_tone(hz(m), 4.4, harmonics=5, tilt=2.2, detune=0.003) * 0.5
intro = lowpass(intro, 1100) * adsr(len(intro), 0.35, 0.5)
# crescendo para a revelação
intro *= np.linspace(0.55, 1.0, len(intro)) ** 1.5
place(music, intro, 0.0, 0.05, -0.1)
place(music, intro, 0.0, 0.05, 0.1)

beep = np.sin(2 * np.pi * hz(81) * np.arange(int(0.14 * SR)) / SR) * adsr(int(0.14 * SR), 0.005, 0.1)
for i, at in enumerate([0.6, 1.8, 3.0]):
    place(sfx, beep, at, 0.045 if i < 2 else 0.035, 0.25)

# digitação 0,2–1,2 s: toques curtos e suaves
for at in np.arange(0.22, 1.2, 0.072) + rng.uniform(-0.012, 0.012, 14):
    n = int(0.012 * SR)
    tick = bandpass(rng.standard_normal(n * 4), 1800, 5200)[:n] * np.exp(-np.arange(n) / (0.0025 * SR))
    place(sfx, tick, at, 0.28 * rng.uniform(0.7, 1.0), rng.uniform(-0.3, 0.3))

# o card crítico entra (1,25 s): duas notas descendentes (Fá# → Ré), discretas
for k, (m, at) in enumerate([(78, 1.25), (74, 1.42)]):
    n = int(0.9 * SR)
    note = soft_tone(hz(m), 0.9, harmonics=3, tilt=1.6) * np.exp(-np.arange(n) / (0.28 * SR))
    note *= adsr(n, 0.004, 0.2)
    place(sfx, note, at, 0.07, 0.15)

# swell antes da revelação (3,0–4,2 s)
n = int(1.2 * SR)
riser = rng.standard_normal(n)
chunks = np.array_split(riser, 24)
riser = np.concatenate([lowpass(c, 300 + 5000 * (i / 23) ** 2) for i, c in enumerate(chunks)])
riser *= np.linspace(0, 1, n) ** 2.5 * adsr(n, 0.01, 0.03)
place(sfx, riser, 3.0, 0.11)

# --- Revelação em diante: I–V–vi–IV, um acorde a cada 2,4 s a partir de 4,2 s
prog = ["D", "A", "Bm", "G", "D", "A", "Bm", "G", "D"]
starts = [4.2 + 2.4 * i for i in range(len(prog))]
END_CHORD = 21.0  # acorde final de Ré sustentado até o fim

for ch, st in zip(prog, starts):
    if st >= END_CHORD:
        break
    dur = 2.4 + 0.6
    pad = sum(
        soft_tone(hz(m), dur, harmonics=6, tilt=1.8, detune=d) for m in CHORDS[ch] for d in (-0.002, 0.002)
    )
    pad = lowpass(pad, 3600) * adsr(int(dur * SR), 0.25, 0.7)
    place(music, pad, st, 0.028, -0.2)
    place(music, np.roll(pad, 300), st, 0.028, 0.2)
    # baixo
    nb = int(2.3 * SR)
    bass = soft_tone(hz(BASS[ch]), 2.3, harmonics=3, tilt=1.5) * adsr(nb, 0.01, 0.4)
    place(music, bass, st, 0.06)

# acorde final
nf = int((DUR - END_CHORD) * SR)
final = sum(soft_tone(hz(m), DUR - END_CHORD, harmonics=6, tilt=1.8, detune=d) for m in [50, 62, 66, 69, 74] for d in (-0.002, 0.002))
final = lowpass(final, 3600) * adsr(nf, 0.05, 1.2)
place(music, final, END_CHORD, 0.03, -0.15)
place(music, np.roll(final, 300), END_CHORD, 0.03, 0.15)
place(music, soft_tone(hz(38), DUR - END_CHORD, harmonics=3, tilt=1.5) * adsr(nf, 0.01, 1.2), END_CHORD, 0.06)

# pluck em colcheias (a partir da cena 3)
def pluck(freq):
    n = int(0.5 * SR)
    t = np.arange(n) / SR
    s = np.sin(2 * np.pi * freq * t) + 0.35 * np.sin(4 * np.pi * freq * t) + 0.12 * np.sin(6 * np.pi * freq * t)
    return s * np.exp(-t / 0.13) * adsr(n, 0.002, 0.05)


for ch, st in zip(prog, starts):
    for k in range(8):
        at = st + k * BEAT / 2
        if at < 7.2 or at >= END_CHORD:
            continue
        m = ARP[ch][k % 4]
        place(music, pluck(hz(m)), at, 0.075 if k % 2 == 0 else 0.055, -0.35 if k % 2 else 0.35)

# percussão suave (4,2 → 21,0 s)
def kick():
    n = int(0.35 * SR)
    t = np.arange(n) / SR
    f = 45 + 75 * np.exp(-t / 0.03)
    return np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t / 0.11)


def hat():
    n = int(0.06 * SR)
    return highpass(rng.standard_normal(n), 7000) * np.exp(-np.arange(n) / (0.012 * SR))


for i in range(int((END_CHORD - 4.2) / BEAT) + 1):
    at = 4.2 + i * BEAT
    if at >= END_CHORD:
        break
    if i % 2 == 0:
        place(music, kick(), at, 0.13)
    if at >= 7.2:
        place(music, hat(), at + BEAT / 2, 0.06, 0.3)

# impacto da revelação
n = int(1.6 * SR)
imp = lowpass(rng.standard_normal(n), 1800) * np.exp(-np.arange(n) / (0.35 * SR))
place(sfx, imp, 4.2, 0.05)
place(sfx, kick(), 4.2, 0.12)

# whooshes curtos nas trocas de cena
def whoosh(dur=0.5):
    n = int(dur * SR)
    noise = rng.standard_normal(n)
    parts = np.array_split(noise, 20)
    out = np.concatenate([bandpass(c, 400, 900 + 2600 * np.sin(np.pi * i / 19)) for i, c in enumerate(parts)])
    env = np.sin(np.pi * np.linspace(0, 1, n)) ** 2
    return out * env


for at in [7.2, 12.0, 16.2, 20.4]:
    place(sfx, whoosh(), at - 0.3, 0.06, 0.0)

# clique no "ver fonte" (9,6 s)
n = int(0.03 * SR)
click = bandpass(rng.standard_normal(n * 4), 1500, 6000)[:n] * np.exp(-np.arange(n) / (0.004 * SR))
click += np.sin(2 * np.pi * 2200 * np.arange(n) / SR) * np.exp(-np.arange(n) / (0.006 * SR)) * 0.5
place(sfx, click, 9.6, 0.22, 0.1)
# o diálogo da fonte abre (9,75 s): brilho curto no tom
n = int(0.7 * SR)
place(sfx, soft_tone(hz(86), 0.7, harmonics=2, tilt=2) * np.exp(-np.arange(n) / (0.2 * SR)), 9.75, 0.03, 0.3)

# --- reverb simples (resposta ao impulso de ruído decaindo) e mixagem
def reverb(x, secs=1.4, mix=0.22):
    n = int(secs * SR)
    ir = rng.standard_normal(n) * np.exp(-np.arange(n) / (0.32 * SR))
    ir = lowpass(ir, 5000)
    ir /= np.sqrt(np.sum(ir**2))
    L = len(x) + n
    wet = np.fft.irfft(np.fft.rfft(x, L) * np.fft.rfft(ir, L), L)[: len(x)]
    return x * (1 - mix) + wet * mix


mix = np.stack([reverb(music[c] + sfx[c] * 0.9) for c in range(2)])
# fade de entrada curtíssimo e saída final
fade_in = np.clip(t_all / 0.03, 0, 1)
fade_out = np.clip((DUR - t_all) / 1.1, 0, 1)
mix *= fade_in * fade_out
mix = highpass(mix[0], 30), highpass(mix[1], 30)
mix = np.stack(mix)
peak = np.max(np.abs(mix))
mix *= 10 ** (-1.0 / 20) / peak  # pico em -1 dBFS
rms_db = 20 * np.log10(np.sqrt(np.mean(mix**2)))
print(f"pico -1 dBFS, RMS {rms_db:.1f} dBFS")

pcm = (mix.T * 32767).astype("<i2")
out = Path(__file__).parent / "audio.wav"
with open(out, "wb") as f:
    data = pcm.tobytes()
    f.write(b"RIFF" + (36 + len(data)).to_bytes(4, "little") + b"WAVEfmt ")
    f.write((16).to_bytes(4, "little") + (1).to_bytes(2, "little") + (2).to_bytes(2, "little"))
    f.write(SR.to_bytes(4, "little") + (SR * 4).to_bytes(4, "little") + (4).to_bytes(2, "little"))
    f.write((16).to_bytes(2, "little") + b"data" + len(data).to_bytes(4, "little") + data)
print(out)
