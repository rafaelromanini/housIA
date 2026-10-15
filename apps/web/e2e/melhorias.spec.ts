import { expect, test } from "@playwright/test";

import { answer, login } from "./helpers";

test("conferência da passagem verbal aponta divergência e omissões", async ({ page }) => {
  await login(page, "enfermagem");
  await page.goto("/pacientes/cen1/passagem");
  await page
    .getByLabel("Relato de quem está saindo")
    .fill("Paciente veio da UTI, pós-op de colecistectomia, PA estável, sem intercorrências. Ceftriaxona às 20h.");
  await page.getByRole("button", { name: "Conferir com o prontuário" }).click();
  await expect(page.getByRole("region", { name: "Divergências com o prontuário" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Não mencionado no relato" })).toContainText("Punção venosa");
  await page.getByRole("button", { name: "Sim, foi útil" }).first().click();
  await expect(page.getByText("Obrigado pelo retorno").first()).toBeVisible();
});

test("documentos de alta: orientações em linguagem simples e SOAP com fonte", async ({ page }) => {
  await login(page, "medico");
  await page.goto("/pacientes/cen3/alta");
  for (const [q, v] of [
    ["Mora sozinho(a)?", "Sim"],
    ["Tem cuidador(a) para o pós-alta?", "Sim"],
    ["O cuidador está capacitado para os cuidados e medicações?", "Sim"],
    ["Há escadas em casa?", "Não"],
    ["Mobilidade reduzida?", "Sim"],
    ["Tem acesso garantido aos medicamentos?", "Sim"],
    ["Precisa de dispositivos em casa?", "Não"],
    ["Consulta de retorno já agendada?", "Sim"],
    ["Entende o plano de alta (teach-back realizado)?", "Sim"],
  ] as const) {
    await answer(page, q, v);
  }
  await page.getByRole("button", { name: /Calcular risco|Reavaliar risco/ }).click();
  await expect(page.getByText("Risco moderado").first()).toBeVisible();
  await page.getByRole("button", { name: /Gerar documentos|Gerar novamente/ }).click();
  const handout = page.locator("#handout");
  await expect(handout).toContainText("Orientações para depois da alta");
  await expect(handout).toContainText("Insulina NPH");
  await expect(handout).toContainText("açúcar baixo");
  await page.getByRole("tab", { name: "Sumário SOAP (prontuário)" }).click();
  await expect(page.getByText("A — Avaliação")).toBeVisible();
  await expect(page.getByRole("button", { name: "Copiar para o prontuário" })).toBeVisible();
});
