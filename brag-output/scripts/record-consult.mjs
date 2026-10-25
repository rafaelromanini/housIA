// Grava em vídeo (WebM) a demo da Consulta com IA: cen6, modo "Simular consulta", nota SOAP e telas de conformidade/auditoria.
// Uso (API e web já no ar; LLM_MODE=mock e FHIR_MODE=local bastam):
//   BASE_URL=http://localhost:3020 OUT_DIR=<pasta fora do git> node brag-output/scripts/record-consult.mjs
// Converta depois: ffmpeg -i <OUT_DIR>/*.webm -c:v libx264 -pix_fmt yuv420p demo-consulta.mp4
import path from "node:path";
import fs from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const { chromium } = await import(pathToFileURL(path.join(ROOT, "apps", "web", "node_modules", "playwright-core", "index.mjs")).href);

const BASE = process.env.BASE_URL ?? "http://localhost:3000";
const OUT = path.resolve(process.env.OUT_DIR ?? path.join(ROOT, "brag-output", "work", "video"));
fs.mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch();
const ctx = await browser.newContext({
  viewport: { width: 1440, height: 900 },
  recordVideo: { dir: OUT, size: { width: 1440, height: 900 } },
  locale: "pt-BR",
  timezoneId: "America/Sao_Paulo",
});
const page = await ctx.newPage();
page.setDefaultTimeout(60000);
const pause = (ms) => page.waitForTimeout(ms);

await page.goto(`${BASE}/login`);
await pause(1500);
await page.getByLabel("E-mail").fill("medico@housia.dev");
await page.getByRole("button", { name: "Entrar", exact: true }).click();
await page.waitForURL("**/leitos");
await page.goto(`${BASE}/consultas`);
await page.getByRole("heading", { name: "Agenda de consultas" }).waitFor();
await pause(1500);

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
await pause(2000);
await page.getByLabel("Li o aviso ao paciente e registrei a decisão dele").check();
await pause(1500);
await start.click();
await pause(2000);

await page.getByRole("tab", { name: /Simular consulta/ }).click();
await page.getByLabel("Velocidade").selectOption("1");
await page.getByRole("button", { name: /Simular consulta|Continuar simulação/ }).click();
await page.getByRole("button", { name: "Roteiro concluído" }).waitFor({ timeout: 240000 });
await page.getByText("IA analisando a conversa…").waitFor({ state: "hidden", timeout: 120000 }).catch(() => {});
await pause(2500);
for (const tab of ["Raciocínio clínico", "Medicamentos e alergias", "Ao vivo"]) {
  await page.getByRole("tab", { name: tab }).click();
  await pause(3000);
}

await finish.click();
await page.getByText("Nota da consulta para revisão").waitFor({ timeout: 300000 });
await pause(4000);
await page.getByLabel(/Revisei e assumo a responsabilidade/).check();
await page.waitForTimeout(1500);
await page.getByRole("button", { name: /Aprovar e (enviar ao prontuário|exportar)/ }).click();
const justificativa = page.getByLabel(/Justificativa do envio com pendência crítica/);
await justificativa.or(page.getByText(/Enviado ao prontuário/).first()).waitFor();
if (await justificativa.isVisible()) {
  await justificativa.fill("Risco avaliado e discutido com o paciente.");
  await page.getByRole("button", { name: "Enviar com justificativa" }).click();
}
await page.getByText(/Enviado ao prontuário/).first().waitFor();
await pause(3000);

for (const rota of ["/conformidade", "/auditoria"]) {
  await page.goto(`${BASE}${rota}`);
  await pause(3500);
  await page.mouse.wheel(0, 500);
  await pause(2500);
}
const video = page.video();
await ctx.close();
console.log("vídeo:", await video.path());
await browser.close();
