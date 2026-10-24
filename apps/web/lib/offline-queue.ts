/**
 * Fila offline de falas (#12). Só TEXTO de falas — nunca áudio. Decisões de privacidade (ver STATUS.md):
 *  - o que vai para o disco do aparelho (IndexedDB) é cifrado com AES-GCM; a chave é não-extraível (WebCrypto) e fica
 *    no próprio IndexedDB, então copiar o arquivo do banco não revela as falas. Isto NÃO protege contra script na
 *    própria página (XSS) nem contra quem usa o navegador já aberto: por isso a fila é curta e se apaga sozinha;
 *  - sem IndexedDB ou WebCrypto (modo privado de alguns navegadores) a fila vive só na memória da aba: nada em disco;
 *  - validade de 12 h; apagada ao finalizar a consulta e ao sair da conta (logout); nunca guarda nome nem dado do
 *    prontuário, só o texto que a pessoa digitou/falou, o falante, o tempo e o client_id.
 */

export interface FalaPendente {
  client_id: string;
  falante: string;
  texto: string;
  t?: number;
  origem: string;
  idioma?: string;
  criado?: number; // quando entrou na fila (validade por fala, não da fila inteira)
}

const DB = "housia-offline";
const FILAS = "filas";
const CHAVES = "chaves";
export const VALIDADE_MS = 12 * 60 * 60 * 1000;

const memoria = new Map<number, FalaPendente[]>();
const carregadas = new Set<number>();
let epoca = 0; // sobe a cada limparTudo: uma gravação em andamento não ressuscita o que foi apagado
const DONO = "housia-offline-dono";
let dbPromise: Promise<IDBDatabase | null> | null = null;

function vigentes(itens: FalaPendente[]): FalaPendente[] {
  const agora = Date.now();
  return itens.filter((f) => typeof f.criado === "number" && agora - f.criado <= VALIDADE_MS);
}

export function novoClientId(): string {
  const c = typeof crypto !== "undefined" ? crypto : null;
  const rnd = c?.randomUUID ? c.randomUUID() : `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;
  return `c-${rnd}`;
}

function abrir(): Promise<IDBDatabase | null> {
  if (dbPromise) return dbPromise;
  dbPromise = new Promise((resolve) => {
    try {
      if (typeof indexedDB === "undefined" || typeof crypto === "undefined" || !crypto.subtle) return resolve(null);
      const req = indexedDB.open(DB, 1);
      req.onupgradeneeded = () => {
        req.result.createObjectStore(FILAS);
        req.result.createObjectStore(CHAVES);
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => resolve(null);
      req.onblocked = () => resolve(null);
    } catch {
      resolve(null);
    }
  });
  return dbPromise;
}

function pedido<T>(db: IDBDatabase, store: string, modo: IDBTransactionMode, fn: (s: IDBObjectStore) => IDBRequest): Promise<T | undefined> {
  return new Promise((resolve) => {
    try {
      const tx = db.transaction(store, modo);
      const req = fn(tx.objectStore(store));
      tx.oncomplete = () => resolve(req.result as T);
      tx.onerror = () => resolve(undefined);
      tx.onabort = () => resolve(undefined);
    } catch {
      resolve(undefined);
    }
  });
}

async function chave(db: IDBDatabase): Promise<CryptoKey | null> {
  try {
    const existente = await pedido<CryptoKey>(db, CHAVES, "readonly", (s) => s.get("principal"));
    if (existente) return existente;
    const nova = await crypto.subtle.generateKey({ name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"]);
    await pedido(db, CHAVES, "readwrite", (s) => s.put(nova, "principal"));
    return nova;
  } catch {
    return null;
  }
}

async function gravar(consultaId: number): Promise<void> {
  const db = await abrir();
  if (!db) return;
  const minha = epoca;
  try {
    const itens = vigentes(memoria.get(consultaId) ?? []);
    memoria.set(consultaId, itens);
    if (itens.length === 0) {
      await pedido(db, FILAS, "readwrite", (s) => s.delete(consultaId));
      return;
    }
    const k = await chave(db);
    if (!k) return; // sem chave não grava nada em disco
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const dados = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, k, new TextEncoder().encode(JSON.stringify(itens)));
    if (minha !== epoca) return; // saiu da conta durante a cifragem: não grava
    await pedido(db, FILAS, "readwrite", (s) => s.put({ iv, dados, atualizado: Date.now() }, consultaId));
  } catch {
    /* fila só em memória */
  }
}

/** Carrega do disco (uma vez por consulta) o que ficou pendente e ainda está na validade. */
export async function carregarFila(consultaId: number): Promise<FalaPendente[]> {
  if (!carregadas.has(consultaId)) {
    carregadas.add(consultaId);
    const db = await abrir();
    if (db) {
      try {
        const rec = await pedido<{ iv: Uint8Array; dados: ArrayBuffer; atualizado: number }>(db, FILAS, "readonly", (s) => s.get(consultaId));
        const k = rec ? await chave(db) : null;
        if (rec && k && Date.now() - rec.atualizado <= VALIDADE_MS + 60_000) {
          const claro = await crypto.subtle.decrypt({ name: "AES-GCM", iv: rec.iv as BufferSource }, k, rec.dados);
          const lidas = vigentes(JSON.parse(new TextDecoder().decode(claro)) as FalaPendente[]); // validade por fala, na leitura
          const atuais = memoria.get(consultaId) ?? [];
          const ids = new Set(atuais.map((f) => f.client_id));
          memoria.set(consultaId, [...lidas.filter((f) => !ids.has(f.client_id)), ...atuais]);
        } else if (rec) {
          await pedido(db, FILAS, "readwrite", (s) => s.delete(consultaId)); // vencida ou ilegível
        }
      } catch {
        await pedido(db, FILAS, "readwrite", (s) => s.delete(consultaId)).catch(() => undefined);
      }
    }
  }
  return pendentes(consultaId);
}

export function pendentes(consultaId: number): FalaPendente[] {
  const vivas = vigentes(memoria.get(consultaId) ?? []);
  memoria.set(consultaId, vivas);
  return [...vivas];
}

export async function enfileirar(consultaId: number, falas: FalaPendente[]): Promise<void> {
  const atuais = pendentes(consultaId);
  const ids = new Set(atuais.map((f) => f.client_id));
  const agora = Date.now();
  memoria.set(consultaId, [...atuais, ...falas.filter((f) => !ids.has(f.client_id)).map((f) => ({ ...f, criado: agora }))]);
  await gravar(consultaId);
}

export async function removerEnviadas(consultaId: number, clientIds: string[]): Promise<void> {
  const fora = new Set(clientIds);
  memoria.set(
    consultaId,
    (memoria.get(consultaId) ?? []).filter((f) => !fora.has(f.client_id)),
  );
  await gravar(consultaId);
}

/** Apaga a fila de uma consulta (ao finalizar). */
export async function limparFila(consultaId: number): Promise<void> {
  memoria.delete(consultaId);
  await gravar(consultaId);
}

/** Apaga tudo, inclusive a chave (ao sair da conta). */
export async function limparTudo(): Promise<void> {
  epoca += 1;
  try {
    localStorage.removeItem(DONO);
  } catch {
    /* sem localStorage */
  }
  memoria.clear();
  carregadas.clear();
  const db = await abrir();
  if (!db) return;
  await pedido(db, FILAS, "readwrite", (s) => s.clear());
  await pedido(db, CHAVES, "readwrite", (s) => s.clear());
}

/** Troca de usuário no mesmo aparelho: a fila de outra pessoa é apagada antes de qualquer uso (guarda só o id do usuário). */
export async function garantirDono(userId: string | number): Promise<void> {
  let anterior: string | null = null;
  try {
    anterior = localStorage.getItem(DONO);
  } catch {
    /* sem localStorage: trata como desconhecido */
  }
  if (anterior !== String(userId)) await limparTudo();
  try {
    localStorage.setItem(DONO, String(userId));
  } catch {
    /* ignora */
  }
}
