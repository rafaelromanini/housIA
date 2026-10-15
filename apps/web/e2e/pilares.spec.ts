import { expect, test } from "@playwright/test";

import { answer, login } from "./helpers";

test("painel de leitos mostra os 5 cenários e a faixa de demonstração", async ({ page }) => {
  await login(page, "enfermagem");
  await expect(page.getByText("AMBIENTE DE DEMONSTRAÇÃO — DADOS SINTÉTICOS")).toBeVisible();
  await expect(page.getByText("Ferramenta de apoio à decisão. Não substitui avaliação profissional.")).toBeVisible();
  await page.getByRole("button", { name: "Só cenários da demo" }).click();
  for (const name of ["Antônio Carlos Ribeiro", "Maria das Graças Souza", "Sebastião Alves Moreira", "Lucas Henrique Martins", "Iracema Lopes Teixeira"]) {
    await expect(page.getByRole("link", { name: new RegExp(name) })).toBeVisible();
  }
  await page.getByRole("link", { name: /Antônio Carlos Ribeiro/ }).click();
  await expect(page.getByRole("heading", { name: "Timeline clínica" })).toBeVisible();
  await expect(page.getByText(/Fonte: Observation\//).first()).toBeVisible();
});

test("pilar 1 — passagem de plantão por perfil com fonte de cada item", async ({ page }) => {
  await login(page, "medico");
  await page.goto("/pacientes/cen1/passagem");
  await page.getByRole("radio", { name: "Médico(a)" }).click();
  await page.getByRole("button", { name: "Gerar passagem" }).click();
  await expect(page.getByText("Em uma frase")).toBeVisible();
  const risks = page.getByRole("article", { name: "Passagem de plantão" });
  await expect(risks.getByText("Gerado por IA — confira")).toBeVisible();
  await expect(risks.getByText(/PA sistólica em ascensão/).first()).toBeVisible();
  const medHeadline = await page.locator("p.text-lg").first().innerText();

  await risks.getByRole("button", { name: /ver fonte/ }).first().click();
  await expect(page.getByRole("dialog", { name: "Fonte da afirmação" })).toBeVisible();
  await expect(page.getByRole("dialog").getByText(/^(Observation|Encounter|Condition)\//).first()).toBeVisible();
  await page.keyboard.press("Escape");

  await page.getByRole("radio", { name: "Enfermagem" }).click();
  await page.getByRole("button", { name: "Gerar passagem" }).click();
  await expect(page.getByText("Em uma frase")).toBeVisible();
  await expect(page.getByText(/Acesso venoso/).first()).toBeVisible();
  await expect(page.getByText(/Risco de queda/).first()).toBeVisible();
  expect(medHeadline.length).toBeGreaterThan(10);

  const ack = page.getByRole("button", { name: "Reconhecer alerta" }).first();
  await ack.click();
  await expect(page.getByText("Reconhecido").first()).toBeVisible();
});

test("pilar 3 — reconciliação gera alertas críticos e hipótese da IA separada", async ({ page }) => {
  await login(page, "medico");
  await page.goto("/pacientes/cen5/reconciliacao");
  await page.getByRole("button", { name: "Executar reconciliação" }).click();
  await expect(page.getByText("Alergia: Dipirona prescrito para paciente alérgico a Dipirona")).toBeVisible();
  await expect(page.getByText("Interação: Morfina + Diazepam")).toBeVisible();
  await expect(page.getByText("Hipótese da IA — confirmar")).toBeVisible();
  await expect(page.getByRole("table")).toContainText("Diazepam");

  await page.goto("/pacientes/cen2/reconciliacao");
  await page.getByRole("button", { name: "Executar reconciliação" }).click();
  await expect(page.getByText(/Possível omissão: Metoprolol/)).toBeVisible();
  await expect(page.getByRole("table").getByText("Omitida?")).toBeVisible();
  await expect(page.getByText(/após omissão de Metoprolol/)).toBeVisible();
});

test("pilar 2 — alta de alto risco trava e exige override justificado; baixo risco não trava", async ({ page }) => {
  await login(page, "medico");
  await page.goto("/pacientes/cen3/alta");
  await answer(page, "Mora sozinho(a)?", "Sim");
  await answer(page, "Tem cuidador(a) para o pós-alta?", "Não");
  await answer(page, "Há escadas em casa?", "Sim");
  await answer(page, "Mobilidade reduzida?", "Sim");
  await answer(page, "Tem acesso garantido aos medicamentos?", "Não");
  await answer(page, "Precisa de dispositivos em casa?", "Não");
  await answer(page, "Consulta de retorno já agendada?", "Não");
  await answer(page, "Entende o plano de alta (teach-back realizado)?", "Não");
  await page.getByRole("button", { name: /Calcular risco|Reavaliar risco/ }).click();

  await expect(page.getByText("Alta travada")).toBeVisible();
  const confirm = page.getByRole("button", { name: "Confirmar alta" });
  await expect(confirm).toBeDisabled();
  await page.getByRole("button", { name: "Registrar override" }).click();
  await expect(page.getByText("A justificativa precisa ter pelo menos 20 caracteres.")).toBeVisible();
  await page.getByLabel(/Registrar override/).fill("Filha virá morar com o paciente; visita do Melhor em Casa agendada para amanhã.");
  await page.getByRole("button", { name: "Registrar override" }).click();
  await expect(page.getByText("Trava liberada por override justificado.")).toBeVisible();
  await confirm.click();
  await expect(page.getByText(/Alta confirmada no HousIA/)).toBeVisible();

  await page.goto("/pacientes/cen4/alta");
  for (const [q, v] of [
    ["Mora sozinho(a)?", "Não"],
    ["Tem cuidador(a) para o pós-alta?", "Sim"],
    ["O cuidador está capacitado para os cuidados e medicações?", "Sim"],
    ["Há escadas em casa?", "Não"],
    ["Mobilidade reduzida?", "Não"],
    ["Tem acesso garantido aos medicamentos?", "Sim"],
    ["Precisa de dispositivos em casa?", "Não"],
    ["Consulta de retorno já agendada?", "Sim"],
    ["Entende o plano de alta (teach-back realizado)?", "Sim"],
  ] as const) {
    await answer(page, q, v);
  }
  await page.getByRole("button", { name: /Calcular risco|Reavaliar risco/ }).click();
  await expect(page.getByText("Risco baixo").first()).toBeVisible();
  await expect(page.getByRole("button", { name: "Confirmar alta" })).toBeEnabled();
});

test("auditoria mostra o override de alta para o gestor", async ({ page }) => {
  await login(page, "gestor");
  await page.getByRole("link", { name: "Auditoria" }).click();
  await expect(page.getByRole("heading", { name: "Auditoria", exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "Métricas do período" }).getByText("Overrides de alta")).toBeVisible();
  await page.getByLabel("Ação").selectOption("discharge.override");
  await expect(page.getByRole("table").getByText("Override de alta (justificado)").first()).toBeVisible();
  await expect(page.getByRole("table").getByText(/Melhor em Casa/).first()).toBeVisible();
});
