// Render every *.mmd next to this file to .svg and .png, using a browser already on the machine.
import { existsSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { run } from "@mermaid-js/mermaid-cli";

const here = dirname(fileURLToPath(import.meta.url));
const candidates = [
	process.env.MERMAID_BROWSER,
	"C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
	"C:/Program Files/Google/Chrome/Application/chrome.exe",
	"/usr/bin/chromium",
	"/usr/bin/google-chrome",
	"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
].filter(Boolean);
const executablePath = candidates.find((path) => existsSync(path));
if (!executablePath) throw new Error("No Chrome/Edge found; set MERMAID_BROWSER to a Chromium-based browser");

for (const file of readdirSync(here).filter((name) => name.endsWith(".mmd"))) {
	for (const ext of ["svg", "png"]) {
		const output = join(here, file.replace(/\.mmd$/, `.${ext}`));
		await run(join(here, file), output, {
			puppeteerConfig: { executablePath, headless: true },
			parseMMDOptions: { backgroundColor: "white", viewport: { width: 1600, height: 1200, deviceScaleFactor: 2 } },
			quiet: true,
		});
		console.log(`rendered ${output}`);
	}
}
