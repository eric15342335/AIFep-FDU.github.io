// Table-QA tools for one sandboxed session: `zoom` (crop + enlarge a page region) and `answer`
// (validate with the baseline's normalize_response, write /work/answer.json, end the session).
import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const MAX_TURNS = Number(process.env.AGENT_MAX_TURNS ?? 25);
const GRACE = readFileSync(`${process.env.PI_CODING_AGENT_DIR}/grace.md`, "utf8").trim();
const REMINDER = "你还没有调用 answer 工具，任务尚未完成。请用你的答案调用 answer 工具。";
const task = JSON.parse(readFileSync("/task/q/question.json", "utf8")) as {
	question_type: string;
	answer_format: string;
};

function helper(args: string[], input?: string): { ok: boolean; out: string } {
	try {
		const out = execFileSync("python3", ["/opt/tableqa_tools.py", ...args], {
			input,
			encoding: "utf8",
			stdio: ["pipe", "pipe", "pipe"],
		});
		return { ok: true, out: out.trim() };
	} catch (error) {
		const stderr = (error as { stderr?: string }).stderr ?? String(error);
		return { ok: false, out: stderr.trim() };
	}
}

const text = (value: string) => ({ type: "text" as const, text: value });

export default function (pi: ExtensionAPI) {
	let turns = 0;
	let emptyRejected = false;
	let answered = false;
	let reminded = false;

	// Turn cap. Reaching it buys one grace turn with only `answer` available, so the work is not lost.
	// It fires only after the last regular turn: sessions that answer within MAX_TURNS never see it.
	pi.on("turn_end", async (_event, ctx) => {
		turns += 1;
		if (turns === MAX_TURNS) {
			pi.setActiveTools(["answer"]);
			pi.sendUserMessage(GRACE, { deliverAs: "steer" });
		} else if (turns > MAX_TURNS) ctx.abort();
	});

	// A model sometimes ends its reply with the answer in its thinking but no `answer` call, which ends the
	// session. Remind it once, in the same session, so the work so far is kept.
	pi.on("agent_before_settle", async (event) => {
		if (answered || reminded) return;
		reminded = true;
		const reminder = { type: "custom_message" as const, customType: "answer-reminder", content: REMINDER, display: false };
		return { entries: [...event.entries, reminder], continue: true };
	});

	pi.registerTool({
		name: "zoom",
		label: "Zoom",
		description:
			"Crop a rectangular region of a page image and return it enlarged. Coordinates are 0-1000, relative to the page width (x) and height (y).",
		promptSnippet: "Crop and enlarge a page region to read small text and numbers",
		promptGuidelines: ["Use zoom before reading any number or short text from a page image."],
		parameters: Type.Object({
			page: Type.Integer({ minimum: 1, description: "1-based page number" }),
			x0: Type.Number({ minimum: 0, maximum: 1000 }),
			y0: Type.Number({ minimum: 0, maximum: 1000 }),
			x1: Type.Number({ minimum: 0, maximum: 1000 }),
			y1: Type.Number({ minimum: 0, maximum: 1000 }),
		}),
		async execute(toolCallId, p) {
			const out = `/tmp/zoom-${toolCallId.replace(/[^\w-]/g, "")}.jpg`;
			const box = [p.x0, p.y0, p.x1, p.y1].map(String);
			const result = helper(["zoom", String(p.page), ...box, out]);
			if (!result.ok) return { content: [text(`zoom failed: ${result.out}`)], details: {} };
			return {
				content: [
					text(`page ${p.page}, region (${box.join(", ")}), ${result.out}`),
					{ type: "image", data: readFileSync(out).toString("base64"), mimeType: "image/jpeg" },
				],
				details: {},
			};
		},
	});

	pi.registerTool({
		name: "answer",
		label: "Answer",
		description:
			"Submit the final answer exactly once. `answer` is the value of the answer field from the output spec (string, number, array, or the structure object). For thinking questions, put arithmetic in `calculations`.",
		promptSnippet: "Submit the final answer; ends the task",
		promptGuidelines: ["Call answer exactly once, after checking the answer against answer_format."],
		parameters: Type.Object({
			answer: Type.Unknown({ description: "Final answer value" }),
			calculations: Type.Optional(
				Type.Array(
					Type.Object({
						index: Type.Union([Type.Integer(), Type.Null()]),
						expression: Type.String(),
						decimals: Type.Union([Type.Integer(), Type.Null()]),
						suffix: Type.String(),
					}),
				),
			),
		}),
		async execute(_toolCallId, p) {
			const response = JSON.stringify({ answer: p.answer ?? "", calculations: p.calculations ?? [] });
			const checked = helper(["check", task.question_type, task.answer_format], response);
			if (!checked.ok) {
				return { content: [text(`Rejected: ${checked.out}. Fix it and call answer again.`)], details: {} };
			}
			if (checked.out === "" && !emptyRejected) {
				emptyRejected = true;
				return {
					content: [text("Rejected: empty answer. Look again (zoom, text layer). Submit empty only if truly absent.")],
					details: {},
				};
			}
			writeFileSync("/work/answer.json", JSON.stringify({ response, normalized: checked.out, turns }));
			answered = true;
			return { content: [text("Accepted.")], details: {}, terminate: true };
		},
	});
}
