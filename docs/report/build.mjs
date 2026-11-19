// Turn report.md into report.pdf (A4), using a browser already on the machine.
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { marked } from "marked";
import puppeteer from "puppeteer-core";

const here = dirname(fileURLToPath(import.meta.url));
const candidates = [
	process.env.REPORT_BROWSER,
	"C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
	"C:/Program Files/Google/Chrome/Application/chrome.exe",
	"/usr/bin/chromium",
	"/usr/bin/google-chrome",
	"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
].filter(Boolean);
const executablePath = candidates.find((path) => existsSync(path));
if (!executablePath) throw new Error("No Chrome/Edge found; set REPORT_BROWSER to a Chromium-based browser");

const css = `
@page { size: A4; margin: 13mm 15mm; }
body { font-family: "Noto Sans SC", "Source Han Sans SC", "Microsoft YaHei", sans-serif; font-size: 9.6pt; line-height: 1.55; color: #1a1a1a; }
strong, th { font-weight: 700; }
h1 { font-size: 15pt; margin: 0 0 4pt; text-align: center; }
h2 { font-size: 11pt; margin: 8pt 0 3pt; break-after: avoid; }
p, li { text-align: justify; }
p { margin: 3pt 0; }
ul, ol { margin: 3pt 0; padding-left: 18pt; }
li { margin: 1.5pt 0; }
code { font-family: Consolas, "Source Code Pro", monospace; font-size: 0.92em; background: #f3f4f6; padding: 0 2pt; border-radius: 2pt; }
a { color: inherit; text-decoration: none; }
table { border-collapse: collapse; margin: 4pt 0; width: 100%; }
th, td { border-top: 0.6pt solid #999; border-bottom: 0.6pt solid #999; padding: 2.5pt 5pt; text-align: left; }
figure { margin: 4pt 0; text-align: center; break-inside: avoid; }
figure img { width: 100%; }
figure.wide img { width: 88%; }
figure.side { float: right; width: 54%; margin: 0 0 4pt 10pt; }
figcaption { font-size: 8.5pt; color: #333; margin-top: 2pt; }
`;

// The page has no address, so the browser would not load local image files; embed them instead.
const embed = (_match, path) =>
	`src="data:image/png;base64,${readFileSync(join(here, path)).toString("base64")}"`;
const body = marked
	.parse(readFileSync(join(here, "report.md"), "utf8"))
	.replace(/src="([^":]+\.png)"/g, embed);
const html = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><style>${css}</style></head><body>${body}</body></html>`;

const browser = await puppeteer.launch({ executablePath, headless: true });
const page = await browser.newPage();
await page.setContent(html, { waitUntil: "networkidle0" });
await page.pdf({ path: join(here, "report.pdf"), format: "A4", printBackground: true, preferCSSPageSize: true });
const pages = await page.evaluate(() => Math.ceil(document.documentElement.scrollHeight / ((297 - 26) * 96 / 25.4)));
await browser.close();
console.log(`wrote report.pdf (about ${pages} pages)`);
