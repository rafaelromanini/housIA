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

/** Etapa de consentimento (obrigatória antes de Iniciar): confirma o aviso lido ao paciente e inicia. */
async function startWithConsent(page: Page) {
  const start = page.getByRole("button", { name: "Iniciar", exact: true });
  await expect(start).toBeDisabled();
  await page.getByLabel("Li o aviso ao paciente e registrei a decisão dele").check();
  await start.click();
}

/** Atesta a revisão e aprova; se o servidor pedir justificativa por pendência crítica aberta, justifica e envia. */
async function approveAndSend(page: Page) {
  await page.getByLabel(/Revisei e assumo a responsabilidade/).check();
  await page.getByRole("button", { name: /Aprovar e (enviar ao prontuário|exportar)/ }).click();
  const justification = page.getByLabel(/Justificativa do envio com pendência crítica/);
  const done = page.getByText(/Enviado ao prontuário|Envio automático desligado/).first();
  await expect(justification.or(done)).toBeVisible();
  if (await justification.isVisible()) {
    await justification.fill("Risco avaliado e discutido com a paciente e a filha.");
    await page.getByRole("button", { name: "Enviar com justificativa" }).click();
  }
  await expect(done).toBeVisible();
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
  await startWithConsent(page);

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
  await approveAndSend(page);
});

test("revisão segura: CID por autocomplete, orientação editada, atestação, envio e diff com o rascunho da IA", async ({ page }) => {
  await login(page, "medico");
  await openFresh(page, "cen6");
  await startWithConsent(page);
  await simulate(page);
  await page.getByRole("button", { name: "Finalizar e gerar nota" }).click();
  await expect(page.getByText("Nota da consulta para revisão")).toBeVisible();

  // sem atestação o servidor não deixa copiar nem enviar: os botões ficam desligados
  await expect(page.getByRole("button", { name: /Aprovar e (enviar ao prontuário|exportar)/ })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Copiar para o prontuário" })).toBeDisabled();

  // CID: busca na tabela oficial e inclusão
  await page.getByLabel(/Buscar CID-10/).fill("hipoglicemia");
  await page.getByRole("button", { name: /^Adicionar CID E16/ }).first().click();
  await expect(page.getByLabel("CID-10 da nota")).toContainText("E16");
  // orientação ao paciente editada
  const orientation = page.getByLabel("Orientação 1", { exact: true });
  await orientation.fill("Oriente a filha a conferir a glicemia antes do café da manhã.");
  // a cronologia vem aberta e editável (cada evento é um campo de texto), com o evento de medicamento da fala 26
  await expect
    .poll(() => page.getByTestId("cronologia").getByRole("textbox").evaluateAll((els) => els.map((el) => (el as HTMLInputElement).value).join("\n")))
    .toMatch(/Iniciou diurético/);

  await approveAndSend(page);

  // diff objetivo: a orientação e o CID mudaram em relação ao rascunho da IA
  await page.getByRole("button", { name: "Comparar com o rascunho da IA" }).click();
  const diff = page.getByTestId("painel-diff");
  await expect(diff).toContainText(/Editado em relação ao rascunho da IA/);
  await expect(diff.getByText(/% editado/).first()).toBeVisible();
});

test("consulta com IA: descartar uma pergunta com motivo a faz sumir e ela não volta durante a simulação", async ({ page }) => {
  await login(page, "medico");
  await openFresh(page, "cen6");
  await startWithConsent(page);

  const anesthesia = /problema com anestesia em cirurgias/;
  await page.getByText(/Mais \d+ perguntas/).click();
  await expect(page.getByText(anesthesia).first()).toBeVisible();
  await page.getByRole("button", { name: /^Descartar: Já teve algum problema com anestesia/ }).click();
  const reason = page.getByLabel(/Motivo do descarte/);
  await reason.fill("curto");
  await expect(page.getByRole("button", { name: "Confirmar descarte" })).toBeDisabled();
  await reason.fill("Anestesia já registrada na avaliação pré-anestésica");
  await page.getByRole("button", { name: "Confirmar descarte" }).click();
  await expect(page.getByText(anesthesia)).toHaveCount(0);

  await simulate(page);
  await expect(page.getByLabel("Transcrição da conversa")).toContainText("Tenho alergia a camarão");
  await expect(page.getByText(anesthesia)).toHaveCount(0);
  // o alerta crítico da consulta aparece com os botões de decisão e o chip de latência mostra as duas camadas
  await expect(page.getByRole("button", { name: /^Aceitar: Glibenclamida/ })).toBeVisible();
  await expect(page.getByTestId("chip-latencia")).toContainText(/regras \d+ ms/);
});

test("centro cirúrgico: relatório por ditado com pendências anti-glosa e conciliação de OPME", async ({ page }) => {
  await login(page, "medico");
  await openFresh(page, "cen4");
  await page.getByLabel("Atendimento").selectOption("cirurgia");
  await startWithConsent(page);
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
  await page.getByLabel(/Revisei e assumo a responsabilidade/).check();
  await page.getByRole("button", { name: /Aprovar e (enviar ao prontuário|exportar)/ }).click();
  await expect(page.getByLabel(/Justificativa do envio com pendência crítica/)).toBeVisible();
});

test("transcrição editável: 'fala N' da nota rola até a fala e a edição persiste depois de recarregar", async ({ page }) => {
  await login(page, "medico");
  await openFresh(page, "cen6");
  await startWithConsent(page);
  await simulate(page);
  await page.getByRole("button", { name: "Finalizar e gerar nota" }).click();
  await expect(page.getByText("Nota da consulta para revisão")).toBeVisible();

  // a revisão mostra a transcrição ao lado da nota; clicar em "fala N" rola até a fala e a destaca
  const link = page.getByRole("button", { name: /Ir para a fala \d+ da conversa/ }).first();
  const label = (await link.getAttribute("aria-label")) ?? "";
  const n = label.match(/fala (\d+)/)?.[1] ?? "1";
  await link.click();
  await expect(page.locator(`#fala-${n}`)).toHaveClass(/ring-2/);

  // edição: texto novo, selo "editada" e persistência após recarregar
  await page.getByRole("button", { name: `Editar a fala ${n}`, exact: true }).click();
  await page.getByLabel(`Texto da fala ${n}`).fill("Texto corrigido pelo profissional.");
  await page.getByRole("button", { name: "Salvar correção" }).click();
  const row = page.locator(`#fala-${n}`);
  await expect(row).toContainText("Texto corrigido pelo profissional.");
  await expect(row).toContainText("editada");
  await expect(page.getByTestId("nota-desatualizada")).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: /Nota para revisar/ }).first().click();
  await expect(page.locator(`#fala-${n}`)).toContainText("Texto corrigido pelo profissional.");
});
