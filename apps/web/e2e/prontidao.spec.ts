import { expect, test } from "@playwright/test";

import { login } from "./helpers";

test("o token de sessão não fica no localStorage e o cookie é httpOnly", async ({ page, context }) => {
  await login(page, "gestor");
  const storage = await page.evaluate(() => ({
    local: JSON.stringify({ ...window.localStorage }),
    session: JSON.stringify({ ...window.sessionStorage }),
    cookieJs: document.cookie,
  }));
  expect(storage.local).not.toMatch(/eyJ|token|housia\.token/i);
  expect(storage.session).not.toMatch(/eyJ|token/i);
  expect(storage.cookieJs).not.toContain("housia_session");
  const cookies = await context.cookies();
  const sessao = cookies.find((c) => c.name === "housia_session");
  expect(sessao?.httpOnly).toBe(true);
  expect(sessao?.sameSite).toBe("Strict");
  // recarregar mantém a sessão (o servidor decide pelo cookie)
  await page.reload();
  await expect(page).toHaveURL(/\/leitos/);
});

test("a tela Ambiente mostra o checklist da configuração em vigor para o gestor", async ({ page }) => {
  await login(page, "gestor");
  await page.getByRole("link", { name: "Ambiente" }).click();
  await expect(page.getByRole("heading", { name: "Ambiente" })).toBeVisible();
  await expect(page.getByRole("list", { name: "Checklist de transparência" })).toBeVisible();
  await expect(page.getByTestId("ambiente-ia")).toContainText("mock");
  await expect(page.getByTestId("ambiente-criptografia")).toBeVisible();
});

test("sair apaga a sessão", async ({ page }) => {
  await login(page, "medico");
  await page.getByRole("button", { name: "Sair" }).click();
  await expect(page).toHaveURL(/\/login/);
  await page.goto("/leitos");
  await expect(page).toHaveURL(/\/login/);
});
