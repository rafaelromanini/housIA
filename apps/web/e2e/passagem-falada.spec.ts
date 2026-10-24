import { expect, test } from "@playwright/test";

import { login } from "./helpers";

/** #42 — roteiro de demonstração da passagem falada: a conferência acontece fala a fala, antes do fim. */
test("passagem falada: 'PA estável' com PA 184/102 aparece como divergência antes do fim da passagem", async ({ page }) => {
  await login(page, "enfermagem");
  await page.goto("/pacientes/cen1/passagem");
  await page.getByRole("radio", { name: "Enfermagem" }).click();
  await page.getByRole("button", { name: "Falar ao vivo" }).click();

  const live = page.getByTestId("passagem-ao-vivo");
  await expect(live).toBeVisible();
  // sem ciência, não começa
  await live.getByLabel("Captura").selectOption("demo");
  await expect(live.getByRole("button", { name: "Tocar roteiro" })).toBeDisabled();
  await live.getByLabel(/Estou ciente de que a minha fala é transcrita/).check();
  await live.getByRole("button", { name: "Tocar roteiro" }).click();

  // 1ª fala: ainda sem PA, a omissão crítica já está na tela
  const omitidos = live.getByTestId("grupo-omitido");
  await expect(omitidos.getByText(/PA 184\/102/).first()).toBeVisible();
  await expect(live.getByTestId("guia-sbar")).toContainText("Próximo");

  // 2ª fala ("PA estável"): a divergência aparece com a fala e a fonte, sem esperar o fim
  const divergentes = live.getByTestId("grupo-divergente");
  await expect(divergentes.getByText(/PA 184\/102/).first()).toBeVisible();
  await expect(divergentes.getByText("Fala 2").first()).toBeVisible();
  await expect(divergentes.getByRole("button", { name: /ver fonte/ }).first()).toBeVisible();
  await expect(live.getByRole("list", { name: "O que foi dito" })).not.toContainText("Pendente: reavaliar");

  // 4ª fala corrige a PA: a divergência some; o roteiro SBAR fecha as quatro etapas
  await expect(divergentes).toHaveCount(0, { timeout: 30_000 });
  await expect(live.getByRole("list", { name: "O que foi dito" })).toContainText("Pendente: reavaliar", { timeout: 60_000 });
  for (const sigla of ["S", "B", "A", "R"]) await expect(live.getByTestId(`sbar-${sigla}`)).toContainText("Dito");

  // fim: conferência completa, gravada como a digitada (só contagens na auditoria)
  await live.getByRole("button", { name: "Encerrar e conferir" }).click();
  await expect(live.getByText(/Cobertura 100%/)).toBeVisible();
  await expect(live.getByTestId("grupo-omitido")).toHaveCount(0);
});

test("passagem falada: digitar cada fala também confere na hora", async ({ page }) => {
  await login(page, "enfermagem");
  await page.goto("/pacientes/cen1/passagem");
  await page.getByRole("radio", { name: "Enfermagem" }).click();
  await page.getByRole("button", { name: "Falar ao vivo" }).click();
  const live = page.getByTestId("passagem-ao-vivo");
  await live.getByLabel("Captura").selectOption("digitar");
  await live.getByLabel(/Estou ciente/).check();
  await live.getByLabel("Próxima fala da passagem").fill("PA estável, sem intercorrências");
  await live.getByRole("button", { name: "Enviar fala" }).click();
  await expect(live.getByTestId("grupo-divergente").getByText(/PA 184\/102/).first()).toBeVisible();
});
