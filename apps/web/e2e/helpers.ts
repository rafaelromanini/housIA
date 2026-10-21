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

/** Abre a aba da consulta e deixa a tela no formulário de início (encerra uma consulta que tenha ficado aberta). */
export async function openFresh(page: Page, patientId: string) {
  await page.goto(`/pacientes/${patientId}/consulta`);
  const start = page.getByRole("button", { name: "Iniciar", exact: true });
  const finish = page.getByRole("button", { name: /Finalizar (e gerar nota|relatório)/ });
  await expect(start.or(finish).first()).toBeVisible();
  if (await finish.isVisible()) {
    if (await finish.isEnabled()) {
      await finish.click();
    } else {
      await page.getByRole("tab", { name: "Digitar" }).click();
      await page.getByLabel("Fala").fill("Encerrando atendimento anterior.");
      await page.getByRole("button", { name: "Enviar" }).click();
      await finish.click();
    }
    await page.getByRole("button", { name: "Nova consulta" }).click();
  }
  await expect(start).toBeVisible();
}

/** Etapa de consentimento (obrigatória antes de Iniciar): confirma o aviso lido ao paciente e inicia. */
export async function startWithConsent(page: Page) {
  const start = page.getByRole("button", { name: "Iniciar", exact: true });
  await expect(start).toBeDisabled();
  await page.getByLabel("Li o aviso ao paciente e registrei a decisão dele").check();
  await start.click();
}
