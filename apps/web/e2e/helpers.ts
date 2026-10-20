import { expect, type Page } from "@playwright/test";

const EMAIL = { medico: "medico@housia.dev", enfermagem: "enfermeira@housia.dev", gestor: "gestor@housia.dev" } as const;

export async function login(page: Page, role: keyof typeof EMAIL = "medico") {
  await page.goto("/login");
  await page.getByLabel("E-mail").fill(EMAIL[role]);
  await page.getByLabel("Senha").fill("housia123");
  await page.getByRole("button", { name: "Entrar", exact: true }).click();
  await expect(page).toHaveURL(/\/leitos/);
}

export async function answer(page: Page, question: string, value: "Sim" | "Não") {
  await page.getByRole("radiogroup", { name: question }).getByRole("radio", { name: value }).click();
}
