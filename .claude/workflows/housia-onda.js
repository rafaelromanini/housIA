export const meta = {
  name: 'housia-onda',
  description: 'Onda do backlog de paridade Lya: duas frentes em paralelo (repo principal e worktree), cada entrega implementada e verificada',
  phases: [
    { title: 'Frente A', detail: 'entregas da frente A, em sequência no repositório principal' },
    { title: 'Frente B', detail: 'entregas da frente B, em sequência no worktree' },
  ],
}

const REPO = args.repo
const WT = args.wt
// backlog da paridade com a Lya (versionado no repositório), venv da API e linhas de atribuição dos commits
const BACKLOG = args.backlog || `${REPO}/docs/paridade-lya/backlog.json`
const VENV = args.venv || `${REPO}/apps/api/.venv`
const ATTRIBUTION = args.attribution

if (!ATTRIBUTION) throw new Error('args.attribution (linhas de atribuição dos commits) é obrigatório')

const REPORT = {
  type: 'object',
  properties: {
    base_sha: { type: 'string', description: 'HEAD sha before you started (git rev-parse HEAD at the very beginning)' },
    feito: { type: 'string', description: 'what was implemented, concise' },
    arquivos: { type: 'array', items: { type: 'string' } },
    testes: { type: 'string', description: 'final pytest/ruff/typecheck/lint result lines' },
    criterios: {
      type: 'array',
      items: {
        type: 'object',
        properties: { criterio: { type: 'string' }, ok: { type: 'boolean' }, evidencia: { type: 'string' } },
        required: ['criterio', 'ok', 'evidencia'],
      },
    },
    pendente: { type: 'array', items: { type: 'string' }, description: 'spec steps not done or partially done, honestly' },
    migracoes: { type: 'array', items: { type: 'string' }, description: 'alembic revision ids added' },
    commits: { type: 'array', items: { type: 'string' } },
  },
  required: ['base_sha', 'feito', 'arquivos', 'testes', 'criterios', 'pendente', 'migracoes', 'commits'],
}

function common(workdir, revPrefix) {
  return `
PROJECT CONTEXT
HousIA is a hospital AI MVP: FastAPI + SQLAlchemy 2 + Alembic + FHIR (apps/api), Next.js 15 App Router + React 19 + Tailwind 4 + radix-ui (apps/web), curated CSV reference data (data/reference), seed scenarios (data/scenarios, builder in apps/api/app/seed/), demo consultation scripts (data/consultations). Pillars: passagem de plantão (handoff), reconciliação medicamentosa (medrec), "Waze da alta" (discharge), and "Consulta com IA" (apps/api/app/consult/: engine.py deterministic rules, text.py text utils/negation, knowledge.py CSV loaders, chart.py FHIR chart view, ai.py LLM layer with mock_live/mock_note/merge_live, schemas.py, service.py, router.py, surgical.py, stt.py, export.py; prompts apps/api/app/llm/prompts/consult_*.md and surgical_report.md; frontend apps/web/components/consult/*). The consult module is our answer to Lya Health and must do everything Lya does, better. Read docs/decisions/0009-consulta-com-ia.md first: deterministic rules run on every segment, AI cycles are merged on top; every item cites refs "Fala/N" and/or FHIR refs (e.g. "MedicationRequest/abc"); invalid refs are dropped to "descartados"; safety rules are never removed by the AI; transcript is pseudonymized before the LLM ([PACIENTE]/[FAMILIAR]) and reidentified on display/export.

The backlog spec items live in ${BACKLOG} at plano.backlog[N] (fields titulo, por_que, o_que_fazer, criterios_de_aceite, melhor_que_lya). Read with:
python3 -c "import json;print(json.dumps(json.load(open('${BACKLOG}'))['plano']['backlog'][N],ensure_ascii=False,indent=1))"
File:line references in the spec come from an earlier snapshot; confirm against current code.

RULES
- Work ONLY inside ${workdir}. Another agent is working in parallel in a different checkout: never touch other directories, never switch branches, never git push, never run docker / docker compose / playwright / "next build" / "npm run build" / "npm install".
- Backend tests (all must pass): cd ${workdir}/apps/api && PYTHONPATH=. LLM_MODE=mock FHIR_MODE=local ${VENV}/bin/python -m pytest -q
- Backend lint: cd ${workdir}/apps/api && ${VENV}/bin/ruff check app tests && ${VENV}/bin/ruff format app tests
- Frontend, whenever you touch apps/web: cd ${workdir}/apps/web && npm run typecheck && npm run lint
- e2e specs live in apps/web/e2e (Playwright). You cannot run them; if your UI change affects selectors used there, update the spec; add new e2e only when the spec asks for it, keep them deterministic (demo script playback, mock LLM).
- Everything must work in LLM_MODE=mock (no API key): rules first, AI optional. If you add fields to the LLM output models in ai.py, keep them small (structured-output grammar size limit: live analysis is split into ConversaOutput and ClinicaOutput for that reason), validate refs with the existing RefValidator, update mock_* functions and the prompt files (bump their version line).
- New Python dependencies: avoid; if truly needed, add to apps/api/pyproject.toml and install into ${VENV} with pip. No new npm dependencies.
- All user-facing text in Brazilian Portuguese with correct accents. Code follows the existing style (Portuguese domain names, same comment density). Keep the UI accessible (labels, aria) like the existing components.
- Data is synthetic only. Never invent official codes: CID-10 codes must be real; TUSS/CBHPM are not licensed here; SIGTAP codes only if you are certain, else clearly-labelled demo codes (DEMO-*). Curated rows carry a "fonte" column when the spec asks.
- DB schema changes: new Alembic migration in apps/api/app/db/migrations/versions/ with revision id starting with "${revPrefix}" (e.g. ${revPrefix}01_nome.py, revision="${revPrefix}01"), down_revision = the single current head of your checkout (read the revision/down_revision chain of the existing files; there must be exactly one head after your change).
- Commit after each coherent deliverable. Message in Portuguese (style: "consulta com IA: ..."), ending with a blank line and exactly:
${ATTRIBUTION}
- Before each commit run: git grep -n "sk-ant" -- .   (must print nothing). Never print or commit secrets. Never mention model names in commits or code.
- Do not weaken or delete existing tests to make them pass; change a golden expectation only when the new behaviour is a deliberate improvement, and say so in the report.
- Report honestly: list anything not done in "pendente".`
}

function implPrompt(task, workdir, revPrefix, prev) {
  return `You are implementing HousIA backlog item(s) ${task.items.join(' + ')} in ${workdir}.
${common(workdir, revPrefix)}

${prev ? `PREVIOUS DELIVERABLE IN THIS CHECKOUT (already committed, build on it):\n${prev}\n` : ''}
TASK
Implement every step of "o_que_fazer" and satisfy every "criterios_de_aceite" of backlog item(s) ${task.items.join(', ')}. Also aim for "melhor_que_lya".
${task.notes}

Start by recording git rev-parse HEAD (base_sha). Read the specs, then the relevant code, then implement. Write pytest tests that exercise each acceptance criterion through the real code path (engine/service/API TestClient on the real scenario data, mock LLM). Finish with all tests, ruff and (if touched) typecheck+lint green, and everything committed.`
}

function verifyPrompt(task, workdir, revPrefix, impl) {
  return `You are an independent, skeptical verifier of HousIA backlog item(s) ${task.items.join(' + ')}, just implemented in ${workdir} by another agent. Assume it is incomplete or subtly wrong until you prove otherwise.
${common(workdir, revPrefix)}

IMPLEMENTER REPORT (claims to check, not facts):
${JSON.stringify(impl, null, 1)}

DO
1. Read the spec(s) and the change: git log --oneline ${impl && impl.base_sha ? impl.base_sha + '..HEAD' : '-15'} and git diff ${impl && impl.base_sha ? impl.base_sha + '..HEAD' : 'HEAD~5..HEAD'}.
2. For EVERY acceptance criterion, make sure an automated test exercises it end-to-end through the real path (not a stub of the function under test); run it; record pass/fail with evidence. Check every "o_que_fazer" step: anything silently skipped must be implemented now.
3. Hunt for regressions and false positives with at least 12 adversarial PT-BR utterances or inputs relevant to this item (negation, no punctuation, no accents/typos, the other speaker saying it, pediatric and elderly patients, the doctor asking vs the patient affirming, unrelated small talk). Confirm mock mode, the cen6 golden tests and the other scenarios still behave; if apps/web changed, run typecheck and lint and read the components for broken states (empty data, long text, tablet width).
4. Fix everything you find (code, data, tests) minimally in the existing style, and commit following the rules. ${task.verifyNotes || ''}
5. Report: criterios with ok=true only when a passing test proves it; base_sha = the implementer's base_sha; commits = all commits of this item (implementer's + yours).`
}


async function runLane(lane, workdir, revPrefix, phaseName) {
  const out = []
  let prev = null
  for (const task of lane) {
    const impl = await agent(implPrompt(task, workdir, revPrefix, prev), {
      label: `${task.key}:implementar`, phase: phaseName, schema: REPORT,
    })
    const ver = await agent(verifyPrompt(task, workdir, revPrefix, impl), {
      label: `${task.key}:verificar`, phase: phaseName, schema: REPORT,
    })
    const r = ver || impl
    out.push({ key: task.key, items: task.items, impl, ver })
    if (r) {
      prev = `${task.key} (items ${task.items.join(', ')}): ${r.feito}\nPending: ${(r.pendente || []).join(' | ') || 'none'}\nMigrations: ${(r.migracoes || []).join(', ') || 'none'}`
      log(`${task.key}: ${r.criterios.filter(c => c.ok).length}/${r.criterios.length} critérios ok`)
    } else {
      log(`${task.key}: sem relatório`)
    }
  }
  return out
}

const [a, b] = await parallel([
  () => runLane(args.laneA || [], REPO, args.revA, 'Frente A'),
  () => runLane(args.laneB || [], WT, args.revB, 'Frente B'),
])
return { laneA: a, laneB: b }
