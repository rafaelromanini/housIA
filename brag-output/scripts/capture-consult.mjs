// Captura a Consulta com IA real (cen6, roteiro de demonstração, IA real) para o vídeo.
import { chromium } from "/home/user/housIA/apps/web/node_modules/playwright-core/index.mjs";
import fs from "node:fs";

const W = "/home/user/housIA/brag-output/work";
const BASE = "http://localhost:3000";
const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2, locale: "pt-BR", timezoneId: "America/Sao_Paulo" });
const page = await ctx.newPage();
page.setDefaultTimeout(30000);
const save = async (name, locator) => {
  const html = await locator.evaluate((el) => el.outerHTML);
  fs.writeFileSync(`${W}/snippets/${name}.html`, html);
  await locator.screenshot({ path: `${W}/shots/${name}.png` });
  console.log("ok", name, html.length);
};
const card = (title) =>
  page.locator("div.rounded-lg.border").filter({ has: page.getByRole("heading", { name: new RegExp("^" + title) }) }).last();
const transcript = () => page.getByLabel("Transcrição da conversa");

await page.goto(`${BASE}/login`);
await page.getByLabel("E-mail").fill("medico@housia.dev");
await page.getByRole("button", { name: "Entrar", exact: true }).click();
await page.waitForURL("**/leitos");

await page.goto(`${BASE}/consultas`);
await page.getByRole("heading", { name: "Agenda de consultas" }).waitFor();
await page.waitForTimeout(500);
await save("consult-agenda", page.locator("main"));

await page.goto(`${BASE}/pacientes/cen6/consulta`);
const start = page.getByRole("button", { name: "Iniciar", exact: true });
const finish = page.getByRole("button", { name: /Finalizar (e gerar nota|relatório)/ });
await start.or(finish).first().waitFor();
if (await finish.isVisible()) {
  if (await finish.isEnabled()) await finish.click();
  else {
    await page.getByRole("tab", { name: "Digitar" }).click();
    await page.getByLabel("Fala").fill("Encerrando atendimento anterior.");
    await page.getByRole("button", { name: "Enviar" }).click();
    await finish.click();
  }
  await page.getByRole("button", { name: "Nova consulta" }).click({ timeout: 180000 });
}
await start.waitFor();
await save("consult-start", page.locator("main"));
await start.click();
await card("Sugestão de perguntas").waitFor();
await page.waitForTimeout(800);
await save("consult-q0", card("Sugestão de perguntas"));
await page.screenshot({ path: `${W}/shots/consult-page0.png` });

await page.getByRole("tab", { name: /Simular consulta/ }).click();
await page.getByLabel("Velocidade").selectOption("2");
await page.getByRole("button", { name: /Simular consulta|Continuar simulação/ }).click();

await transcript().getByText("Faz umas três semanas").waitFor({ timeout: 60000 });
await page.waitForTimeout(700);
await save("consult-q1", card("Sugestão de perguntas"));
await save("consult-t1", page.getByRole("region", { name: "Captura e transcrição" }));
await page.screenshot({ path: `${W}/shots/consult-page1.png` });

await transcript().getByText("Tomei a glibenclamida e vim direto").waitFor({ timeout: 90000 });
await page.waitForTimeout(900);
await save("consult-alerts1", card("Alertas de segurança"));
await save("consult-q2", card("Sugestão de perguntas"));
await page.screenshot({ path: `${W}/shots/consult-page2.png` });

await page.getByRole("button", { name: "Roteiro concluído" }).waitFor({ timeout: 180000 });
// espera o último ciclo da IA terminar
await page.waitForTimeout(3000);
await page.getByText("IA analisando a conversa…").waitFor({ state: "hidden", timeout: 180000 });
await page.getByRole("button", { name: "Analisar agora" }).click();
await page.getByText("IA analisando a conversa…").waitFor({ timeout: 10000 }).catch(() => {});
await page.getByText("IA analisando a conversa…").waitFor({ state: "hidden", timeout: 240000 });
await page.waitForTimeout(1200);
await page.screenshot({ path: `${W}/shots/consult-page3.png` });
await save("consult-transcript", page.getByRole("region", { name: "Captura e transcrição" }));
await save("consult-q3", card("Sugestão de perguntas"));
await save("consult-alerts", card("Alertas de segurança"));
await save("consult-queixas", card("Queixa principal"));
await save("consult-emocoes", card("Análise multidimensional"));
await save("consult-resumo", card("Sumarizador"));
await save("consult-smartapps", page.getByRole("region", { name: "Smart Apps" }));

await page.getByRole("tab", { name: "Raciocínio clínico" }).click();
await page.waitForTimeout(600);
await save("consult-hipoteses", card("Possíveis hipóteses"));
await save("consult-anamnese", card("Anamnese estruturada"));
await page.getByRole("tab", { name: "Medicamentos e alergias" }).click();
await page.waitForTimeout(600);
await save("consult-meds", card("Histórico de medicamentos"));
await save("consult-alergias", card("Alergias"));
await page.getByRole("tab", { name: "Ao vivo" }).click();

await finish.click();
await page.getByText("Nota da consulta para revisão").waitFor({ timeout: 300000 });
await page.waitForTimeout(1000);
const note = page.locator("div.rounded-lg.border").filter({ has: page.getByText("Nota da consulta para revisão") }).last();
await save("consult-note", note);
await page.getByRole("button", { name: /Aprovar e (enviar ao prontuário|exportar)/ }).click();
await page.getByText(/Enviado ao prontuário/).first().waitFor({ timeout: 60000 });
await page.waitForTimeout(800);
await save("consult-note-sent", note);
await page.screenshot({ path: `${W}/shots/consult-page4.png`, fullPage: true });
await browser.close();
