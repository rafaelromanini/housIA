import { expect, test } from "@playwright/test";

import { login, openFresh, startWithConsent } from "./helpers";

/**
 * Captura em celular e tablet (#12): roda no Chromium e nos projetos WebKit "iPhone 13" e "iPad".
 * Fluxos Digitar e Simular, fila offline (context.setOffline) e Wake Lock (mock).
 */

test("manifest válido e instalável", async ({ request }) => {
  const res = await request.get("/manifest.webmanifest");
  expect(res.ok()).toBeTruthy();
  const m = await res.json();
  expect(m).toMatchObject({ name: "HousIA", display: "standalone", start_url: "/leitos", scope: "/" });
  const sizes = m.icons.map((i: { sizes: string }) => i.sizes);
  expect(sizes).toEqual(expect.arrayContaining(["192x192", "512x512"]));
  for (const icon of m.icons) expect((await request.get(icon.src)).ok()).toBeTruthy();
  const sw = await request.get("/sw.js");
  expect(sw.ok()).toBeTruthy();
  expect(await sw.text()).not.toMatch(/\/api\b|patients|consultations/); // o service worker não toca em dado clínico
});

test("Digitar no celular/tablet: a fala aparece na transcrição", async ({ page }) => {
  await login(page, "medico");
  await openFresh(page, "cen6");
  await startWithConsent(page);
  await page.getByRole("tab", { name: "Digitar" }).click();
  await page.getByLabel("Fala").fill("Bom dia, o que a senhora está sentindo?");
  await page.getByRole("button", { name: "Enviar" }).click();
  await expect(page.getByLabel("Transcrição da conversa")).toContainText("o que a senhora está sentindo");
});

test("Simular no celular/tablet", async ({ page }) => {
  await login(page, "medico");
  await openFresh(page, "cen6");
  await startWithConsent(page);
  await page.getByRole("tab", { name: /Simular consulta/ }).click();
  await page.getByLabel("Velocidade").selectOption("8");
  await page.getByRole("button", { name: /Simular consulta|Continuar simulação/ }).click();
  await expect(page.getByRole("button", { name: "Roteiro concluído" })).toBeVisible({ timeout: 90_000 });
  await expect(page.getByLabel("Transcrição da conversa")).toContainText("Tenho alergia a camarão");
});

test("sem rede: 3 falas digitadas chegam em ordem e sem duplicar quando a rede volta", async ({ page, context }) => {
  await login(page, "medico");
  await openFresh(page, "cen6");
  await startWithConsent(page);
  await page.getByRole("tab", { name: "Digitar" }).click();

  await context.setOffline(true);
  const falas = ["Primeira fala sem rede.", "Segunda fala sem rede.", "Terceira fala sem rede."];
  for (const f of falas) {
    await page.getByLabel("Fala").fill(f);
    await page.getByRole("button", { name: "Enviar" }).click();
  }
  await expect(page.getByTestId("falas-aguardando-rede")).toContainText("3 falas aguardando rede");
  await expect(page.getByLabel("Transcrição da conversa")).not.toContainText("Primeira fala sem rede.");

  await context.setOffline(false);
  const transcript = page.getByLabel("Transcrição da conversa");
  await expect(transcript).toContainText("Terceira fala sem rede.");
  await expect(page.getByTestId("falas-aguardando-rede")).toHaveCount(0);
  const text = await transcript.innerText();
  const pos = falas.map((f) => text.indexOf(f));
  expect(pos.every((p) => p >= 0)).toBeTruthy();
  expect(pos).toEqual([...pos].sort((a, b) => a - b));
  for (const f of falas) expect(text.split(f).length - 1).toBe(1);
});

test("Wake Lock é solicitado ao iniciar a captura (mock)", async ({ page }) => {
  await page.addInitScript(() => {
    const w = window as unknown as { __wakeLocks: string[] };
    w.__wakeLocks = [];
    Object.defineProperty(navigator, "wakeLock", {
      configurable: true,
      value: {
        request: async (type: string) => {
          w.__wakeLocks.push(type);
          return { release: async () => undefined, addEventListener: () => undefined };
        },
      },
    });
  });
  await login(page, "medico");
  await openFresh(page, "cen6");
  await startWithConsent(page);
  await page.getByRole("tab", { name: /Simular consulta/ }).click();
  expect(await page.evaluate(() => (window as unknown as { __wakeLocks: string[] }).__wakeLocks.length)).toBe(0);
  await page.getByLabel("Velocidade").selectOption("1");
  await page.getByRole("button", { name: /Simular consulta|Continuar simulação/ }).click();
  await expect.poll(() => page.evaluate(() => (window as unknown as { __wakeLocks: string[] }).__wakeLocks)).toContain("screen");
});
