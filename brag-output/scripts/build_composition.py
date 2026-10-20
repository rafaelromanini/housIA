"""Monta composition.html com os trechos reais da UI do HousIA (HTML + CSS do app) para o vídeo /brag."""

import json
from pathlib import Path

W = Path(__file__).parent
snip = {p.stem: p.read_text(encoding="utf-8") for p in (W / "snippets").glob("*.html")}

STETHO = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
    'stroke-linejoin="round"><path d="M11 2v2"/><path d="M5 2v2"/>'
    '<path d="M5 3H4a2 2 0 0 0-2 2v4a6 6 0 0 0 12 0V5a2 2 0 0 0-2-2h-1"/>'
    '<path d="M8 15a6 6 0 0 0 12 0v-3"/><circle cx="20" cy="10" r="2"/></svg>'
)
CURSOR = (
    '<svg viewBox="0 0 24 24" width="44" height="44"><path d="M4 2l16 9.5-7 1.6-3.6 6.9z" fill="#12404f" '
    'stroke="#fff" stroke-width="1.5" stroke-linejoin="round"/></svg>'
)

html = f"""<!doctype html>
<html lang="pt-BR" class="__variable_de3fe8">
<head>
<meta charset="utf-8">
<link rel="stylesheet" href="app.css">
<style>
  html, body {{ margin: 0; width: 1920px; height: 1080px; overflow: hidden; background: #f3fbfc; }}
  body {{ font-family: var(--font-atkinson), sans-serif; color: #12404f; }}
  .stage {{ position: absolute; inset: 0; overflow: hidden; }}
  .scene {{ position: absolute; inset: 0; opacity: 0; }}
  .a {{ position: absolute; opacity: 0; will-change: transform, opacity; }}
  .title {{ font-size: 64px; font-weight: 700; letter-spacing: -0.5px; line-height: 1.1; }}
  .pill {{ display: inline-block; font-size: 24px; font-weight: 700; color: #0e7490; background: #e0f4f8;
          border: 2px solid #b6e3ec; border-radius: 999px; padding: 6px 18px; }}
  .caption {{ font-size: 44px; font-weight: 700; color: #0f3a47; }}
  .caption em {{ font-style: normal; color: #0e7490; }}
  .card-shadow {{ box-shadow: 0 30px 60px -20px rgba(15,58,71,.35), 0 8px 20px -8px rgba(15,58,71,.2);
                  border-radius: 14px; }}
  .clip {{ overflow: hidden; }}
  .fade-bottom {{ -webkit-mask-image: linear-gradient(180deg, #000 82%, transparent 100%); mask-image: linear-gradient(180deg, #000 82%, transparent 100%); }}
  .ui {{ background: #f3fbfc; }}
  /* diálogo real do app, fora do modo "fixed" */
  .dialog-wrap [role=dialog] {{ position: static !important; transform: none !important; translate: none !important;
      scale: none !important; max-width: none !important; overflow: hidden !important;
      width: 100% !important; max-height: none !important; animation: none !important; }}
  .dialog-wrap [role=dialog] > button {{ display: none; }}
  /* cena 1 */
  #s1 {{ background: radial-gradient(1200px 700px at 30% 30%, #14505f 0%, #0f3a47 55%, #0a2a33 100%); }}
  .bubble {{ background: rgba(255,255,255,.08); border: 2px solid rgba(255,255,255,.18); border-radius: 36px;
            padding: 40px 56px; color: #fff; font-size: 76px; font-weight: 700; line-height: 1.15; }}
  .bubble:after {{ content: ""; position: absolute; left: 90px; bottom: -34px; border: 18px solid transparent;
                  border-top: 18px solid rgba(255,255,255,.18); }}
  .caret {{ display: inline-block; width: 6px; height: 72px; background: #67e8f9; vertical-align: -8px; margin-left: 6px; }}
  .label-dark {{ color: #67e8f9; font-size: 32px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; }}
  .caption-dark {{ color: #fff; font-size: 64px; font-weight: 700; }}
  /* revelação e fechamento */
  .logo {{ display: flex; align-items: center; gap: 28px; color: #0e7490; font-weight: 700; }}
  .logo svg {{ width: 1em; height: 1em; }}
  .chip {{ display: inline-flex; align-items: center; gap: 10px; font-size: 28px; font-weight: 700; color: #0f3a47;
          background: #fff; border: 2px solid #cfe0e6; border-radius: 999px; padding: 10px 24px; margin: 0 8px; }}
  .chip i {{ width: 12px; height: 12px; border-radius: 99px; background: #047857; display: inline-block; }}
  #cursor {{ position: absolute; z-index: 50; opacity: 0; }}
  .ripple {{ position: absolute; z-index: 49; width: 60px; height: 60px; margin: -30px 0 0 -30px; border-radius: 99px;
            border: 4px solid #0891b2; opacity: 0; }}
</style>
</head>
<body>
<div class="stage">

  <!-- 1. Gancho -->
  <section id="s1" class="scene" data-start="0" data-end="4.2">
    <div class="a label-dark" data-in="0.05" data-dy="16" style="left:160px; top:150px">Troca de plantão · relato verbal</div>
    <div class="a bubble" data-in="0.1" data-dy="24" style="left:160px; top:215px; width:1180px; min-height:120px">
      <span id="typed"></span><span class="caret" id="caret"></span>
    </div>
    <div class="a" id="s1card" data-in="1.25" data-dx="140" style="left:700px; top:510px; width:880px; transform-origin: left top">
      <div class="ui card-shadow" style="padding:0; border-radius:14px; overflow:hidden" id="s1cardInner"></div>
    </div>
    <div class="a caption-dark" data-in="1.55" data-dy="20" style="left:160px; top:880px; width:1600px">
      O que não é dito na passagem, se perde.
    </div>
  </section>

  <!-- 2. Revelação -->
  <section id="s2" class="scene" data-start="4.2" data-end="7.2">
    <div class="a" data-in="4.2" style="left:-40px; top:-60px; width:2000px; filter: blur(3px); opacity:.0" id="s2bg">
      <div class="ui" style="width:1400px; transform: scale(1.4); transform-origin: left top">{snip["leitos"]}</div>
    </div>
    <div class="a" data-in="4.2" style="inset:0; background: radial-gradient(900px 520px at 50% 50%, rgba(243,251,252,.97) 40%, rgba(243,251,252,.75) 100%)"></div>
    <div class="a logo" data-in="4.35" data-scale="0.92" style="left:0; right:0; top:330px; justify-content:center; font-size:170px">
      {STETHO}<span>HousIA</span>
    </div>
    <div class="a" data-in="4.75" data-dy="20" style="left:0; right:0; top:560px; text-align:center; font-size:66px; font-weight:700">
      IA para a transição do cuidado
    </div>
    <div class="a" data-in="5.1" data-dy="14" style="left:0; right:0; top:665px; text-align:center; font-size:38px; color:#475569; font-weight:700">
      Passagem de plantão &nbsp;·&nbsp; Transferência &nbsp;·&nbsp; Alta
    </div>
  </section>

  <!-- 3. Passagem de plantão -->
  <section id="s3" class="scene" data-start="7.2" data-end="12.0">
    <div class="a" data-in="7.3" data-dy="16" style="left:120px; top:70px"><span class="pill">Pilar 1</span></div>
    <div class="a title" data-in="7.35" data-dy="16" style="left:120px; top:122px">Passagem de plantão por perfil</div>
    <div class="a card-shadow" data-in="7.6" data-dy="30" style="left:120px; top:240px; width:1060px">{snip["handoff-headline"]}</div>
    <div class="a" id="s3risks" data-in="7.9" data-dy="30" style="left:120px; top:440px; width:1060px">
      <div class="card-shadow clip" style="max-height:520px; border-radius:14px">{snip["handoff-risks"]}</div>
    </div>
    <div class="a dialog-wrap" id="s3source" data-in="9.75" data-scale="0.94" style="left:1240px; top:240px; width:600px; transform-origin: 30% 30%">
      <div class="card-shadow clip fade-bottom" style="max-height:600px; border-radius:16px">{snip["handoff-source"]}</div>
    </div>
    <div class="a caption" data-in="9.9" data-dy="16" style="left:1240px; top:890px; width:620px">
      Cada alerta com a <em>fonte</em> no prontuário.
    </div>
  </section>

  <!-- 4. Reconciliação -->
  <section id="s4" class="scene" data-start="12.0" data-end="16.2">
    <div class="a" data-in="12.1" data-dy="16" style="left:120px; top:70px"><span class="pill">Pilar 3</span></div>
    <div class="a title" data-in="12.15" data-dy="16" style="left:120px; top:122px">Reconciliação na transferência</div>
    <div class="a" id="s4alerts" data-in="12.4" data-dy="30" style="left:120px; top:240px; width:1000px">
      <div class="card-shadow clip" style="max-height:640px; border-radius:14px">{snip["medrec-alerts"]}</div>
    </div>
    <div class="a" data-in="13.7" data-dx="80" style="left:1170px; top:250px; width:640px">
      <div class="card-shadow" style="border-radius:14px">{snip["medrec-hypothesis"]}</div>
    </div>
    <div class="a caption" data-in="13.4" data-dy="16" style="left:120px; top:930px; width:1700px">
      Alergias, interações e omissões <em>antes da dose</em>.
    </div>
  </section>

  <!-- 5. Alta -->
  <section id="s5" class="scene" data-start="16.2" data-end="20.4">
    <div class="a" data-in="16.3" data-dy="16" style="left:120px; top:70px"><span class="pill">Pilar 2</span></div>
    <div class="a title" data-in="16.35" data-dy="16" style="left:120px; top:122px">Alta segura</div>
    <div class="a" data-in="16.6" data-dy="30" style="left:120px; top:240px; width:820px">
      <div class="card-shadow clip" style="max-height:600px; border-radius:14px">{snip["alta-travada"]}</div>
    </div>
    <div class="a" data-in="17.7" data-dx="90" style="left:1000px; top:200px; width:820px">
      <div class="card-shadow clip ui" style="max-height:660px; border-radius:14px">{snip["alta-handout-es"]}</div>
    </div>
    <div class="a" data-in="18.1" data-dy="12" style="left:1000px; top:880px"><span class="pill">Orientações ao paciente · PT · ES · EN</span></div>
    <div class="a caption" data-in="17.4" data-dy="16" style="left:120px; top:930px; width:860px">
      Alta de risco só sai <em>com plano</em>.
    </div>
  </section>

  <!-- 6. Fechamento -->
  <section id="s6" class="scene" data-start="20.4" data-end="24.2" style="background: linear-gradient(180deg, #f3fbfc 0%, #e0f4f8 100%)">
    <div class="a logo" data-in="20.5" data-scale="0.94" style="left:0; right:0; top:190px; justify-content:center; font-size:110px">
      {STETHO}<span>HousIA</span>
    </div>
    <div class="a" data-in="20.8" data-dy="18" style="left:0; right:0; top:380px; text-align:center; font-size:76px; font-weight:700; line-height:1.15">
      Nenhum alerta sem fonte.<br><span style="color:#0e7490">Nenhuma transição no escuro.</span>
    </div>
    <div class="a" data-in="21.6" data-dy="12" style="left:0; right:0; top:640px; text-align:center">
      <span class="chip"><i></i>Lê o prontuário via FHIR R4</span>
      <span class="chip"><i></i>Fonte em cada item</span>
      <span class="chip"><i></i>Pseudonimização antes da IA</span>
      <span class="chip"><i></i>Suporte à decisão</span>
    </div>
    <div class="a" data-in="22.1" data-dy="12" style="left:0; right:0; top:790px; text-align:center; font-size:46px; font-weight:700; color:#0e7490">
      Piloto com o seu hospital — vamos conversar.
    </div>
  </section>

  <div id="cursor">{CURSOR}</div>
  <div class="ripple" id="ripple"></div>
</div>

<script>
const FADE_IN = 0.5, FADE_OUT = 0.35;
const clamp = (x) => Math.max(0, Math.min(1, x));
const ease = (x) => 1 - Math.pow(1 - clamp(x), 3);
const QUOTE = "“PA estável, sem intercorrências.”";

// trechos reais reaproveitados: card crítico da conferência verbal, só os primeiros itens de cada lista
const divergence = document.createElement("div");
divergence.innerHTML = {json.dumps(snip["verbal-check"])};
const firstDiv = divergence.querySelector('section[aria-label="Divergências com o prontuário"] li');
document.getElementById("s1cardInner").appendChild(firstDiv);
firstDiv.style.margin = "0"; firstDiv.style.fontSize = "26px"; firstDiv.style.padding = "28px";
const riskItems = [...document.querySelectorAll("#s3risks li")].filter(li => li.parentElement.closest("li") === null);
riskItems.forEach((li, i) => {{ if (i > 2) li.style.display = "none"; }});
const alertItems = [...document.querySelectorAll("#s4alerts ul > li")];
alertItems.forEach((li, i) => {{ if (i > 1) li.style.display = "none"; }});
document.querySelectorAll("#s4alerts p.mt-3").forEach(p => p.style.display = "none");

const scenes = [...document.querySelectorAll(".scene")];
function sceneOpacity(sc, t) {{
  const s = +sc.dataset.start, e = +sc.dataset.end;
  if (t < s || t > e) return 0;
  const fin = s === 0 ? 1 : ease((t - s) / 0.25);
  const fout = 1 - clamp((t - (e - FADE_OUT)) / FADE_OUT);
  return Math.min(fin, fout);
}}

function animate(el, t, start) {{
  const p = ease((t - start) / FADE_IN);
  const dx = +(el.dataset.dx || 0), dy = +(el.dataset.dy || 0), s0 = +(el.dataset.scale || 1);
  el.style.opacity = p;
  const sc = s0 + (1 - s0) * p;
  el.style.transform = `translate(${{dx * (1 - p)}}px, ${{dy * (1 - p)}}px) scale(${{sc}})`;
}}

window.render = (t) => {{
  for (const sc of scenes) {{
    const o = sceneOpacity(sc, t);
    sc.style.opacity = o;
    sc.style.visibility = o > 0 ? "visible" : "hidden";
    // "ken burns" bem leve para dar vida às cenas
    const s = +sc.dataset.start, e = +sc.dataset.end;
    sc.style.transform = `scale(${{1 + 0.015 * clamp((t - s) / (e - s))}})`;
    for (const el of sc.querySelectorAll(".a")) animate(el, t, +el.dataset.in);
  }}
  // cena 1: digitação
  const typedChars = Math.floor(QUOTE.length * clamp((t - 0.2) / 1.0));
  document.getElementById("typed").textContent = QUOTE.slice(0, typedChars);
  document.getElementById("caret").style.opacity = (t < 1.25 || Math.floor(t * 2) % 2 === 0) ? 1 : 0;
  const card = document.getElementById("s1card");
  card.style.transform += ` scale(1.22)`;
  // cena 2: painel real ao fundo
  document.getElementById("s2bg").style.opacity = 0.35 * ease((t - 4.2) / 0.6);
  // cena 3: riscos entram um a um
  riskItems.forEach((li, i) => {{
    const p = ease((t - (8.1 + i * 0.35)) / 0.45);
    li.style.opacity = p; li.style.transform = `translateY(${{14 * (1 - p)}}px)`;
  }});
  // cena 4: alertas críticos um a um
  alertItems.forEach((li, i) => {{
    const p = ease((t - (12.6 + i * 0.5)) / 0.45);
    li.style.opacity = p; li.style.transform = `translateX(${{-30 * (1 - p)}}px)`;
  }});
  // cursor clica em "ver fonte" na cena 3
  const cursor = document.getElementById("cursor"), ripple = document.getElementById("ripple");
  const target = [...document.querySelectorAll("#s3risks button")].find(b => /ver fonte/.test(b.textContent));
  if (target && t > 8.6 && t < 11.6) {{
    const r = target.getBoundingClientRect();
    const tx = r.left + r.width * 0.35, ty = r.top + r.height * 0.55;
    const p = ease((t - 8.7) / 0.9);
    const x = 1500 + (tx - 1500) * p, y = 980 + (ty - 980) * p;
    cursor.style.opacity = sceneOpacity(scenes[2], t);
    cursor.style.transform = `translate(${{x}}px, ${{y}}px) scale(${{t > 9.6 && t < 9.75 ? 0.88 : 1}})`;
    const rp = clamp((t - 9.6) / 0.5);
    ripple.style.left = tx + "px"; ripple.style.top = ty + "px";
    ripple.style.opacity = t > 9.6 ? (1 - rp) * 0.9 : 0;
    ripple.style.transform = `scale(${{0.4 + rp * 1.4}})`;
  }} else {{
    cursor.style.opacity = 0; ripple.style.opacity = 0;
  }}
}};
window.ready = document.fonts.ready.then(() => Promise.all([...document.images].map(i => i.decode().catch(() => null))));
window.render(0);
</script>
</body>
</html>
"""
(W / "composition.html").write_text(html, encoding="utf-8")
print("composition.html", len(html))
