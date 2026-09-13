import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const data = JSON.parse(
  readFileSync(new URL("../src/demo-data.json", import.meta.url), "utf8"),
);
const storyboard = JSON.parse(
  readFileSync(new URL("../src/storyboard.json", import.meta.url), "utf8"),
);
assert.equal(data.synthetic, true);
assert.match(data.sourceCommit, /^[a-f0-9]{40}$/);
assert.equal(data.baseline.metadata.synthetic, true);
assert.equal(data.candidate.metadata.synthetic, true);
assert.equal(data.baseline.elapsed_ms, data.replay.baseline.runtime_ms);
assert.equal(data.candidate.elapsed_ms, data.replay.candidate.runtime_ms);
assert.equal(
  data.replay.deltas.runtime_ms_delta,
  data.candidate.elapsed_ms - data.baseline.elapsed_ms,
);
assert.equal(
  data.replay.deltas.latency_improvement_pct,
  Number(
    ((1 - data.candidate.elapsed_ms / data.baseline.elapsed_ms) * 100).toFixed(
      2,
    ),
  ),
);
assert.equal(data.finding.estimate.calibrated, false);
assert.equal(data.finding.estimate.method, "heuristic");
assert.equal(data.finding.evidence_level, "declared");
assert.equal(data.finding.affected_spans.length, 3);
assert.equal(data.pairs.length, 6);
assert.equal(new Set(data.pairs.map((pair) => pair.task)).size, 3);
assert.equal(new Set(data.pairs.map((pair) => pair.seed)).size, 2);
assert.equal(
  data.pairs.filter((pair) => pair.passed).length,
  data.outcomes.configured_gates_passed_count,
);
assert.equal(data.pairs.filter((pair) => !pair.qualityPassed).length, 1);
assert.equal(data.pairs.filter((pair) => !pair.costEvaluable).length, 1);
assert.equal(data.replay.quality.passed, true);
assert.equal(
  storyboard.scenes.reduce((sum, scene) => sum + scene.seconds, 0),
  72,
);
assert.equal(storyboard.scenes.length, 8);
assert.equal(new Set(storyboard.scenes.map((scene) => scene.id)).size, 8);
console.log(
  "Verified source provenance, displayed metrics, quality outcomes, and 72-second timeline.",
);
