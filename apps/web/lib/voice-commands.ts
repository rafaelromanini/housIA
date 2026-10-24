/** Comandos de voz do ditado (#41). Só valem quando são a fala inteira; no meio de uma frase é texto comum.
 * Espelham as regras do servidor (apps/api/app/consult/adendo.py). */
export type VoiceCommand = "proximo_campo" | "apagar_ultima" | "desfazer" | "nova_linha";

const COMMANDS: [VoiceCommand, RegExp][] = [
  ["proximo_campo", /^(proximo campo|proxima secao|proximo|avancar)$/],
  ["apagar_ultima", /^(apagar|apague|remover|remova) (a )?ultima frase$/],
  ["desfazer", /^(desfazer|desfaca)( ultima acao)?$/],
  ["nova_linha", /^(nova linha|novo paragrafo|quebra de linha)$/],
];

function normalize(text: string): string {
  return text
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/\s+/g, " ")
    .trim()
    .replace(/^[\s.,;:!?"'“”]+|[\s.,;:!?"'“”]+$/g, "");
}

export function parseVoiceCommand(text: string): VoiceCommand | null {
  const n = normalize(text);
  return COMMANDS.find(([, rx]) => rx.test(n))?.[0] ?? null;
}

/** Estado de um campo de texto livre ditado: o texto e a pilha de versões anteriores (para "desfazer"). */
export type LocalDictation = { text: string; history: string[]; newLine: boolean };

function lastSentenceStart(text: string): number {
  const trimmed = text.replace(/\s+$/, "");
  const re = /[.!?;\n]\s*/g;
  let start = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(trimmed)) !== null) {
    if (m.index + m[0].length < trimmed.length) start = m.index + m[0].length;
  }
  return start;
}

function remember(state: LocalDictation): string[] {
  return [...state.history, state.text].slice(-20);
}

/** Aplica uma fala ao campo local (justificativa, override): texto novo ou comando. `next` indica "próximo campo". */
export function applyDictation(state: LocalDictation, spoken: string): { state: LocalDictation; next: boolean } {
  const cmd = parseVoiceCommand(spoken);
  if (cmd === "proximo_campo") return { state, next: true };
  if (cmd === "nova_linha") return { state: { ...state, newLine: true }, next: false };
  if (cmd === "desfazer") {
    const history = state.history.slice();
    const text = history.pop();
    return { state: text === undefined ? state : { text, history, newLine: false }, next: false };
  }
  if (cmd === "apagar_ultima") {
    const text = state.text.slice(0, lastSentenceStart(state.text)).replace(/\s+$/, "");
    return { state: { text, history: remember(state), newLine: false }, next: false };
  }
  const clean = spoken.replace(/\s+/g, " ").trim();
  if (!clean) return { state, next: false };
  const sep = !state.text.trim() ? "" : state.newLine ? "\n" : " ";
  return {
    state: { text: state.text.replace(/\s+$/, "") + sep + clean, history: remember(state), newLine: false },
    next: false,
  };
}
