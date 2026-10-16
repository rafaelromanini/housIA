// Captura a UI real do HousIA (com o Claude gerando o conteúdo) para reutilizar no vídeo.
import { chromium } from "/home/user/housIA/apps/web/node_modules/playwright-core/index.mjs";
import fs from "node:fs";

const W = "/home/user/housIA/brag-output/work";
const BASE = "http://localhost:3000";
const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2, locale: "pt-BR", timezoneId: "America/Sao_Paulo" });
const page = await ctx.newPage();
const save = async (name, locator) => {
  const html = await locator.evaluate((el) => el.outerHTML);
  fs.writeFileSync(`${W}/snippets/${name}.html`, html);
  await locator.screenshot({ path: `${W}/shots/${name}.png` });
  console.log("ok", name);
};
const answer = (q, v) => page.getByRole("radiogroup", { name: q }).getByRole("radio", { name: v }).click();

await page.goto(`${BASE}/login`);
await page.getByLabel("E-mail").fill("medico@housia.dev");
await page.getByRole("button", { name: "Entrar", exact: true }).click();
await page.waitForURL("**/leitos");

// CSS e fontes reais do app
const cssHrefs = await page.$$eval('link[rel="stylesheet"]', (ls) => ls.map((l) => l.href));
let css = "";
for (const href of cssHrefs) css += await (await page.request.get(href)).text();
for (const m of new Set([...css.matchAll(/url\((\/_next\/static\/media\/[^)]+)\)/g)].map((x) => x[1]))) {
  const file = m.split("/").pop();
  fs.writeFileSync(`${W}/assets/${file}`, await (await page.request.get(BASE + m)).body());
  css = css.replaceAll(m, `assets/${file}`);
}
fs.writeFileSync(`${W}/app.css`, css);
console.log("css", css.length);

// Painel de leitos
await page.getByRole("button", { name: "Só cenários da demo" }).click();
await save("leitos", page.locator("main"));

// Pilar 1 — passagem (médico)
await page.goto(`${BASE}/pacientes/cen1/passagem`);
await page.getByRole("radio", { name: "Médico(a)" }).click();
await page.getByRole("button", { name: "Gerar passagem" }).click();
await page.getByText("Em uma frase").waitFor({ timeout: 180000 });
await save("handoff-header", page.locator("section[aria-label='Identificação do paciente']"));
await save("handoff-headline", page.getByText("Em uma frase").locator(".."));
const risks = page.getByText("Riscos nas próximas horas").locator("xpath=ancestor::div[contains(@class,'rounded-lg')][1]");
await save("handoff-risks", risks);
await risks.getByRole("button", { name: /ver fonte/ }).first().click();
await page.getByRole("dialog").getByText(/Observation\/|DocumentReference\//).first().waitFor();
await page.waitForTimeout(400);
await save("handoff-source", page.getByRole("dialog"));
await page.keyboard.press("Escape");

// Conferência da passagem verbal
await page.getByLabel("Relato de quem está saindo").fill("Paciente veio da UTI, pós-op de colecistectomia, PA estável, sem intercorrências. Ceftriaxona às 20h.");
await page.getByRole("button", { name: "Conferir com o prontuário" }).click();
await page.getByRole("region", { name: "Divergências com o prontuário" }).waitFor({ timeout: 180000 });
await save("verbal-check", page.getByText("Conferir a passagem verbal").locator("xpath=ancestor::div[contains(@class,'rounded-lg')][1]"));

// Pilar 3 — reconciliação (cenário 5)
await page.goto(`${BASE}/pacientes/cen5/reconciliacao`);
await page.getByRole("button", { name: "Executar reconciliação" }).click();
await page.getByText("Interação: Morfina + Diazepam").waitFor({ timeout: 180000 });
await save("medrec-alerts", page.getByText(/Alertas das regras/).locator("xpath=ancestor::div[contains(@class,'rounded-lg')][1]"));
await save("medrec-hypothesis", page.getByRole("region", { name: "Hipóteses da IA" }));
await save("medrec-table", page.getByRole("table").locator("xpath=ancestor::div[contains(@class,'rounded-lg')][1]"));

// Pilar 2 — alta (cenário 3)
await page.goto(`${BASE}/pacientes/cen3/alta`);
for (const [q, v] of [["Mora sozinho(a)?", "Sim"], ["Tem cuidador(a) para o pós-alta?", "Não"], ["Há escadas em casa?", "Sim"],
  ["Mobilidade reduzida?", "Sim"], ["Tem acesso garantido aos medicamentos?", "Não"], ["Precisa de dispositivos em casa?", "Não"],
  ["Consulta de retorno já agendada?", "Não"], ["Entende o plano de alta (teach-back realizado)?", "Não"]]) await answer(q, v);
await page.getByRole("button", { name: /Calcular risco|Reavaliar risco/ }).click();
await page.getByText("Alta travada").waitFor({ timeout: 180000 });
const riskCard = page.getByText("Risco de reinternação").locator("xpath=ancestor::div[contains(@class,'rounded-lg')][1]");
await save("alta-travada", riskCard);
await save("alta-sugestoes", page.getByText("Sugestões para o plano de alta").locator("xpath=ancestor::div[contains(@class,'rounded-lg')][1]"));
await page.getByLabel(/Registrar override/).fill("Filha virá morar com o paciente por 2 semanas; visita do Melhor em Casa agendada para amanhã.");
await page.getByRole("button", { name: "Registrar override" }).click();
await page.getByText("Trava liberada por override justificado.").waitFor();
await save("alta-liberada", riskCard);
await page.getByLabel("Idioma das orientações").selectOption("es");
await page.getByRole("button", { name: /Gerar documentos|Gerar novamente/ }).click();
await page.locator("#handout").getByText(/Instrucciones|Indicaciones|alta/i).first().waitFor({ timeout: 240000 });
await save("alta-handout-es", page.locator("#handout"));

// Auditoria (gestor)
await page.goto(`${BASE}/login`);
await page.getByLabel("E-mail").fill("gestor@housia.dev");
await page.getByRole("button", { name: "Entrar", exact: true }).click();
await page.waitForURL("**/leitos");
await page.goto(`${BASE}/auditoria`);
await page.getByText("Utilidade percebida da IA").waitFor();
await page.waitForTimeout(600);
await save("auditoria-tiles", page.getByRole("region", { name: "Métricas do período" }));
await browser.close();
