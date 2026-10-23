import { defineConfig, devices } from "@playwright/test";

/**
 * E2E dos 3 pilares. Requer a stack rodando (docker compose up, ou API + web locais) com LLM_MODE=mock.
 * E2E_BASE_URL muda o alvo; PLAYWRIGHT_CHROMIUM_PATH usa um Chromium já instalado.
 */
export default defineConfig({
  testDir: "./e2e",
  timeout: 120_000,
  expect: { timeout: 30_000 },
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3000",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    locale: "pt-BR",
    timezoneId: "America/Sao_Paulo",
    launchOptions: process.env.PLAYWRIGHT_CHROMIUM_PATH ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_PATH } : {},
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
    // celular e tablet (#12): o CI só instala o Chromium, então emulamos os aparelhos (viewport, userAgent, toque,
    // isMobile) no Chromium. WebKit real (Safari/iOS) segue pendente: ver STATUS.md.
    { name: "iPhone 13", use: { ...devices["iPhone 13"], browserName: "chromium", defaultBrowserType: "chromium" }, testMatch: /mobile\.spec\.ts/ },
    { name: "iPad", use: { ...devices["iPad (gen 7)"], browserName: "chromium", defaultBrowserType: "chromium" }, testMatch: /mobile\.spec\.ts/ },
  ],
});
