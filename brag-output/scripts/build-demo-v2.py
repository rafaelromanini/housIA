"""Narração e montagem do vídeo de apresentação v2 (HousIA).

  tts       gera um MP3 por frase (Microsoft Edge TTS, pt-BR) e grava durations.json   [venv com edge-tts]
  assemble  junta o vídeo gravado, a narração (sincronizada pelo timeline.json), a trilha discreta e as legendas

Uso:
  <venv-tts>/python brag-output/scripts/build-demo-v2.py tts --work <pasta>
  python brag-output/scripts/build-demo-v2.py assemble --work <pasta> --out brag-output/work

O texto enviado ao serviço de voz é só o roteiro de narração (demonstração sintética, sem dado de paciente real).
"""

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ROTEIRO = ROOT / "brag-output" / "demo-v2-roteiro.json"
GAP = 0.35  # pausa entre frases (s), a mesma usada na gravação


def roteiro() -> dict:
    return json.loads(ROTEIRO.read_text(encoding="utf-8"))


def duracao(arq: Path) -> float:
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(arq)], text=True
    )
    return float(out.strip())


async def sintetizar(work: Path) -> None:
    import edge_tts

    r = roteiro()
    audio = work / "audio"
    audio.mkdir(parents=True, exist_ok=True)
    durs = {}
    for f in r["frases"]:
        alvo = audio / f"{f['id']}.mp3"
        texto = f.get("fala") or f["texto"]
        for tentativa in range(4):
            try:
                await edge_tts.Communicate(texto, r["voz"], rate=r["rate"]).save(str(alvo))
                break
            except Exception as e:  # rede instável: tenta de novo
                if tentativa == 3:
                    raise
                print("nova tentativa", f["id"], e)
                await asyncio.sleep(2)
        durs[f["id"]] = round(duracao(alvo), 3)
        print(f"{f['id']:8s} {durs[f['id']]:6.2f}s  {len(texto)} car.")
    (work / "durations.json").write_text(json.dumps(durs, indent=1), encoding="utf-8")
    print("total de fala:", round(sum(durs.values()), 1), "s")


def tempo_srt(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def quebrar(texto: str, limite: int = 74) -> list[str]:
    """Divide a frase em blocos de legenda de até duas linhas, preferindo cortar em pontuação."""
    palavras = texto.split()
    blocos, atual = [], ""
    for p in palavras:
        if atual and len(atual) + 1 + len(p) > limite * 2:
            blocos.append(atual)
            atual = p
        else:
            atual = f"{atual} {p}".strip()
    if atual:
        blocos.append(atual)
    out = []
    for b in blocos:  # duas linhas equilibradas
        if len(b) <= limite:
            out.append(b)
            continue
        meio = len(b) // 2
        corte = min((i for i, c in enumerate(b) if c == " "), key=lambda i: abs(i - meio))
        out.append(b[:corte] + "\n" + b[corte + 1 :])
    return out


def gerar_srt(work: Path, destino: Path) -> None:
    r = roteiro()
    tl = json.loads((work / "timeline.json").read_text(encoding="utf-8"))
    linhas, n = [], 1
    for f in r["frases"]:
        s = tl["frases"][f["id"]]
        blocos = quebrar(f["texto"])
        total = sum(len(b) for b in blocos)
        t = s["inicio"]
        for b in blocos:
            dur = s["dur"] * len(b) / total
            linhas.append(f"{n}\n{tempo_srt(t)} --> {tempo_srt(t + dur)}\n{b}\n")
            n += 1
            t += dur
    destino.write_text("\n".join(linhas), encoding="utf-8")


def montar(work: Path, out: Path) -> None:
    r = roteiro()
    tl = json.loads((work / "timeline.json").read_text(encoding="utf-8"))
    out.mkdir(parents=True, exist_ok=True)
    video = work / "raw.mp4"
    total = tl["duracao"]

    # 1) narração: cada frase entra no instante em que a gravação a "disse"
    cmd = ["ffmpeg", "-y", "-v", "error"]
    for f in r["frases"]:
        cmd += ["-i", str(work / "audio" / f"{f['id']}.mp3")]
    partes, rotulos = [], []
    for i, f in enumerate(r["frases"]):
        ms = int(round(tl["frases"][f["id"]]["inicio"] * 1000))
        partes.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=mono,adelay={ms}:all=1[a{i}]")
        rotulos.append(f"[a{i}]")
    partes.append(
        "".join(rotulos)
        + f"amix=inputs={len(rotulos)}:normalize=0:dropout_transition=0,apad=whole_dur={total:.3f},atrim=0:{total:.3f}[voz]"
    )
    cmd += ["-filter_complex", ";".join(partes), "-map", "[voz]", "-ar", "48000", str(work / "voz.wav")]
    subprocess.run(cmd, check=True)

    # 2) volume da voz padronizado (EBU R128) e trilha discreta sintetizada (acorde suave, bem abaixo da voz)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(work / "voz.wav"), "-af", "loudnorm=I=-16:TP=-1.5:LRA=9", "-ar", "48000",
         str(work / "voz-norm.wav")],
        check=True,
    )
    acorde = "+".join(f"sin(2*PI*{f}*t)*{a}" for f, a in [(110.0, 0.5), (164.81, 0.35), (220.0, 0.3), (277.18, 0.2), (329.63, 0.15)])
    trilha = (
        f"aevalsrc='({acorde})*(0.75+0.25*sin(2*PI*0.08*t))':s=48000:d={total:.3f},"
        f"lowpass=f=900,volume=0.09,afade=t=in:d=3,afade=t=out:st={total - 4:.3f}:d=4"
    )
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", trilha, "-ar", "48000", str(work / "trilha.wav")], check=True)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(work / "voz-norm.wav"), "-i", str(work / "trilha.wav"),
         "-filter_complex", "[0:a][1:a]amix=inputs=2:normalize=0:duration=first[m];[m]alimiter=limit=0.89[o]",
         "-map", "[o]", "-ar", "48000", str(work / "mix.wav")],
        check=True,
    )

    # 3) legenda .srt e as duas versões do MP4 (sem legenda queimada e com legenda queimada)
    srt = out / "demo-v2.srt"
    gerar_srt(work, srt)
    limpo = out / "demo-v2.mp4"
    comuns = ["-c:v", "libx264", "-preset", "slow", "-crf", "18", "-pix_fmt", "yuv420p", "-r", "30", "-c:a", "aac", "-b:a", "192k",
              "-movflags", "+faststart"]
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(video), "-i", str(work / "mix.wav"), "-map", "0:v", "-map", "1:a", *comuns,
         "-shortest", str(limpo)],
        check=True,
    )
    estilo = ("FontName=Segoe UI,FontSize=22,PrimaryColour=&H00FFFFFF,OutlineColour=&H80101820,BackColour=&H80101820,"
              "BorderStyle=1,Outline=2,Shadow=0,MarginV=36,Bold=1")
    # o filtro 'subtitles' lê o arquivo pelo nome relativo (evita problemas com ':' do caminho no Windows)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(video), "-i", str(work / "mix.wav"), "-map", "0:v", "-map", "1:a",
         "-vf", f"subtitles={srt.name}:force_style='{estilo}'", *comuns, "-shortest", str(out / "demo-v2-legendado.mp4")],
        check=True,
        cwd=str(out),
    )
    # roteiro de narração em texto
    cenas: dict[str, list[str]] = {}
    for f in r["frases"]:
        cenas.setdefault(f["cena"], []).append(f["texto"])
    txt = "\n\n".join(f"## {c}\n" + "\n".join(t) for c, t in cenas.items())
    (out / "demo-v2-narracao.txt").write_text("Roteiro de narração - HousIA (demonstração com dados sintéticos)\n\n" + txt + "\n", encoding="utf-8")
    print("pronto:", limpo, "e", out / "demo-v2-legendado.mp4")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["tts", "assemble"])
    ap.add_argument("--work", required=True)
    ap.add_argument("--out", default=str(ROOT / "brag-output" / "work"))
    a = ap.parse_args()
    w = Path(a.work)
    if a.cmd == "tts":
        asyncio.run(sintetizar(w))
    else:
        montar(w, Path(a.out))
    sys.exit(0)
