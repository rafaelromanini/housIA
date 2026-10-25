// Renderiza quadros da composição como função pura do tempo.
// uso: node render.mjs stills 1.0 5.5 ...   |   node render.mjs frames
import { fileURLToPath, pathToFileURL } from "node:url";
const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..", "..");
const { chromium } = await import(pathToFileURL(path.join(ROOT, "apps", "web", "node_modules", "playwright-core", "index.mjs")).href);
import fs from "node:fs";
import http from "node:http";
import path from "node:path";

const W = path.join(ROOT, "brag-output", "work");
const FPS = 30, DURATION = 24;
const [mode, ...times] = process.argv.slice(2);
const types = { ".html": "text/html", ".css": "text/css", ".woff2": "font/woff2", ".png": "image/png" };
const server = http.createServer((req, res) => {
  const file = path.join(W, decodeURIComponent(req.url.split("?")[0]));
  if (!file.startsWith(W) || !fs.existsSync(file)) { res.writeHead(404); return res.end(); }
  res.writeHead(200, { "Content-Type": types[path.extname(file)] ?? "application/octet-stream" });
  fs.createReadStream(file).pipe(res);
}).listen(0);
const port = server.address().port;
const browser = await chromium.launch();
const page = await (await browser.newContext({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 })).newPage();
page.on("pageerror", (e) => console.log("pageerror", e.message));
await page.goto(`http://localhost:${port}/composition.html`);
await page.evaluate(() => window.ready);
if (mode === "stills") {
  fs.mkdirSync(`${W}/stills`, { recursive: true });
  for (const t of times) {
    await page.evaluate((x) => window.render(x), Number(t));
    await page.screenshot({ path: `${W}/stills/t${String(t).padStart(5, "0")}.png` });
  }
} else {
  fs.mkdirSync(`${W}/frames`, { recursive: true });
  for (let i = 0; i < FPS * DURATION; i++) {
    await page.evaluate((x) => window.render(x), i / FPS);
    await page.screenshot({ path: `${W}/frames/f${String(i).padStart(4, "0")}.png` });
    if (i % 90 === 0) console.log("frame", i);
  }
}
await browser.close();
server.close();
