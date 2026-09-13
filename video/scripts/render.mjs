import { bundle } from "@remotion/bundler";
import {
  openBrowser,
  renderMedia,
  renderStill,
  selectComposition,
} from "@remotion/renderer";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const { values } = parseArgs({
  options: {
    stills: { type: "boolean", default: false },
    smoke: { type: "boolean", default: false },
    browser: { type: "string" },
    output: { type: "string", default: "out" },
    concurrency: { type: "string", default: "2" },
  },
});
const concurrency = Number(values.concurrency);
if (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > 16)
  throw new Error("concurrency must be an integer between 1 and 16");
const out = path.resolve(root, values.output);
await mkdir(out, { recursive: true });
const storyboard = JSON.parse(
  await readFile(path.join(root, "src/storyboard.json"), "utf8"),
);
console.log("Bundling AgentLoop demo…");
const serveUrl = await bundle({
  entryPoint: path.join(root, "src/index.ts"),
  publicDir: path.join(root, "public"),
  outDir: path.join(root, ".remotion/bundle"),
});
const browser = await openBrowser("chrome", {
  browserExecutable: values.browser || process.env.REMOTION_BROWSER_EXECUTABLE,
  logLevel: "warn",
});
try {
  const composition = await selectComposition({
    serveUrl,
    id: "AgentLoopDemo",
    puppeteerInstance: browser,
  });
  let from = 0;
  const cues = storyboard.scenes.map((scene) => {
    const cue = { ...scene, from, duration: scene.seconds * storyboard.fps };
    from += cue.duration;
    return cue;
  });
  if (values.stills || values.smoke) {
    for (const cue of cues) {
      const frame = cue.from + Math.floor(cue.duration * 0.78);
      await renderStill({
        serveUrl,
        composition,
        frame,
        puppeteerInstance: browser,
        imageFormat: "png",
        output: path.join(out, `${cue.id}.png`),
        logLevel: "warn",
      });
      console.log(`Checked scene: ${cue.id} (frame ${frame})`);
    }
  }
  if (!values.stills) {
    let lastReport = -1;
    await renderMedia({
      serveUrl,
      composition,
      puppeteerInstance: browser,
      codec: "h264",
      pixelFormat: "yuv420p",
      crf: 19,
      x264Preset: "fast",
      concurrency,
      outputLocation: path.join(
        out,
        values.smoke ? "smoke.mp4" : "agentloop-demo.mp4",
      ),
      frameRange: values.smoke ? [810, 839] : undefined,
      onProgress: ({ progress }) => {
        const percent = Math.floor(progress * 20) * 5;
        if (percent > lastReport) {
          console.log(`Render ${percent}%`);
          lastReport = percent;
        }
      },
      logLevel: "warn",
    });
  }
  const stamp = (frame) => {
    const ms = Math.round((frame / storyboard.fps) * 1000);
    return `${String(Math.floor(ms / 3600000)).padStart(2, "0")}:${String(Math.floor(ms / 60000) % 60).padStart(2, "0")}:${String(Math.floor(ms / 1000) % 60).padStart(2, "0")},${String(ms % 1000).padStart(3, "0")}`;
  };
  await writeFile(
    path.join(out, "agentloop-demo.srt"),
    cues
      .map(
        (cue, index) =>
          `${index + 1}\n${stamp(cue.from)} --> ${stamp(cue.from + cue.duration)}\n${cue.caption}\n`,
      )
      .join("\n"),
  );
  console.log(`Artifacts: ${out}`);
} finally {
  await browser.close({ silent: true });
}
