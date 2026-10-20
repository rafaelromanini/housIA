import { expect, test } from "@playwright/test";

const MAILPIT = process.env.MAILPIT_URL ?? "http://localhost:8025";

async function linkDoEmail(request: import("@playwright/test").APIRequestContext, para: string, assunto: string) {
  for (let i = 0; i < 20; i++) {
    const lista = await request.get(`${MAILPIT}/api/v1/search`, { params: { query: `to:${para} subject:"${assunto}"` } });
    const msgs = (await lista.json()).messages ?? [];
    if (msgs.length) {
      const msg = await (await request.get(`${MAILPIT}/api/v1/message/${msgs[0].ID}`)).json();
      const m = /(https?:\/\/\S+\?token=[\w-]+)/.exec(msg.Text);
      if (m) return m[1];
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error("e-mail não chegou ao mailpit");
}

test("a raiz é a página pública e o cadastro de consultório termina em /consultas", async ({ page, request }) => {
  const ping = await request.get(`${MAILPIT}/api/v1/info`).catch(() => null);
  test.skip(!ping || !ping.ok(), "mailpit indisponível: suba o compose de desenvolvimento");

  const email = `bia.${Date.now()}@clinica.test`;
  await page.goto("/");
  await expect(page.getByRole("link", { name: "Começar grátis" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Piloto para hospital" })).toBeVisible();
  await page.getByRole("link", { name: "Começar grátis" }).click();

  await page.getByLabel("Nome do consultório").fill("Consultório Bia");
  await page.getByLabel("Seu nome").fill("Beatriz Souza");
  await page.getByLabel("E-mail").fill(email);
  await page.getByLabel(/^Senha/).fill("senha-segura-1");
  await page.getByLabel("Número").fill("998877");
  await page.getByLabel("UF").selectOption("MG");
  await page.getByRole("button", { name: "Criar conta" }).click();
  await expect(page.getByText("Confira seu e-mail")).toBeVisible();

  await page.goto(await linkDoEmail(request, email, "Confirme"));
  await expect(page.getByText("E-mail confirmado")).toBeVisible();

  await page.goto("/login");
  await page.getByLabel("E-mail").fill(email);
  await page.getByLabel("Senha").fill("senha-segura-1");
  await page.getByRole("button", { name: "Entrar", exact: true }).click();
  await expect(page).toHaveURL(/\/consultas/);
  await expect(page.getByText("Primeiros passos")).toBeVisible();
});
