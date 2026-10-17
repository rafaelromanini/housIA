import { expect, type Page, test } from "@playwright/test";

import { login } from "./helpers";

/** Abre a aba da consulta e deixa a tela no formulário de início (encerra uma consulta que tenha ficado aberta). */
async function openFresh(page: Page, patientId: string) {
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

async function simulate(page: Page) {
  await page.getByRole("tab", { name: /Simular consulta/ }).click();
  await page.getByLabel("Velocidade").selectOption("8");
  await page.getByRole("button", { name: /Simular consulta|Continuar simulação/ }).click();
  await expect(page.getByRole("button", { name: "Roteiro concluído" })).toBeVisible({ timeout: 90_000 });
}

test("consulta com IA: transcrição ao vivo, perguntas em tempo real, Smart Apps e nota para o prontuário", async ({ page }) => {
  await login(page, "medico");
  await page.getByRole("link", { name: "Consultas" }).click();
  await expect(page.getByRole("heading", { name: "Agenda de consultas" })).toBeVisible();
  await expect(page.getByText("Joana Ferreira Lima")).toBeVisible();

  await openFresh(page, "cen6");
  await page.getByRole("button", { name: "Iniciar", exact: true }).click();

  // antes da conversa: perguntas a partir do prontuário
  await expect(page.getByText("Atualiza a cada fala. Some da lista quando o profissional já perguntou, mesmo com outras palavras.")).toBeVisible();
  await expect(page.getByText(/Toma a Glibenclamida mesmo nos dias/)).toBeVisible();

  await simulate(page);
  await expect(page.getByLabel("Transcrição da conversa")).toContainText("Tenho alergia a camarão");
  // filtro "Só clínico": o cumprimento (fala social) some; a fala clínica continua
  const transcript = page.getByLabel("Transcrição da conversa");
  await expect(transcript).toContainText("Bom dia, dona Joana");
  await page.getByLabel("Só clínico").check();
  await expect(transcript).not.toContainText("Bom dia, dona Joana");
  await expect(transcript).toContainText("Tenho alergia a camarão");
  await page.getByLabel("Só clínico").uncheck();
  await expect(transcript).toContainText("Bom dia, dona Joana");
  // a sugestão some quando a pergunta foi feita na conversa
  await expect(page.getByText(/Já abordadas na conversa/)).toBeVisible();
  await expect(page.getByText("Tríplice ameaça renal", { exact: false }).first()).toBeVisible();
  await expect(page.getByRole("region", { name: "Smart Apps" }).getByText("Suor frio e tremor", { exact: true })).toBeVisible();

  await page.getByRole("tab", { name: "Raciocínio clínico" }).click();
  await expect(page.getByText("Hipoglicemia induzida por sulfonilureia")).toBeVisible();
  await page.getByRole("button", { name: "Ir para a fala 7 da conversa" }).first().click();

  await page.getByRole("tab", { name: "Medicamentos e alergias" }).click();
  await expect(page.getByText("Relatada — registrar no prontuário")).toBeVisible();

  await page.getByRole("tab", { name: "Prontuário e exames" }).click();
  await expect(page.getByText("Protocolos de acompanhamento")).toBeVisible();

  await page.getByRole("button", { name: "Finalizar e gerar nota" }).click();
  await expect(page.getByText("Nota da consulta para revisão")).toBeVisible();
  // painel "Fora da nota": cobertura calculada e botão para incluir a fala
  await expect(page.getByTestId("cobertura-nota")).toContainText(/\d+ de \d+ falas clínicas citadas/);
  await page.getByLabel("Plano").fill("Suspender ibuprofeno e glibenclamida; reduzir metformina; retorno em 2 semanas.");
  await page.getByRole("button", { name: /Aprovar e (enviar ao prontuário|exportar)/ }).click();
  await expect(page.getByText(/Enviado ao prontuário|Envio automático desligado/).first()).toBeVisible();
});

test("centro cirúrgico: relatório por ditado com pendências anti-glosa e conciliação de OPME", async ({ page }) => {
  await login(page, "medico");
  await openFresh(page, "cen4");
  await page.getByLabel("Atendimento").selectOption("cirurgia");
  await page.getByRole("button", { name: "Iniciar", exact: true }).click();
  await simulate(page);
  await expect(page.getByText("Verificações automáticas")).toBeVisible();
  await expect(page.getByText(/Pinça de energia ultrassônica citado no ditado sem autorização/).first()).toBeVisible();
  // prontidão para faturamento ao vivo (regras anti-glosa em dados, com a fonte de cada pendência)
  await expect(page.getByRole("progressbar", { name: "Prontidão para faturamento" })).toBeVisible();
  await expect(page.getByText("Pronto para faturar").first()).toBeVisible();
  await expect(page.getByText("Não ditado — obrigatório").first()).toBeVisible();
  await expect(page.getByRole("table", { name: "Materiais citados e autorizados" })).toContainText("Conferido");
  await page.getByRole("button", { name: "Finalizar relatório" }).click();
  await expect(page.getByText("Relatório cirúrgico para revisão")).toBeVisible();
  // pendência crítica de faturamento: o envio pede justificativa (trava suave, auditada)
  await page.getByRole("button", { name: /Aprovar e (enviar ao prontuário|exportar)/ }).click();
  await expect(page.getByLabel(/Justificativa do envio com pendência crítica/)).toBeVisible();
});
