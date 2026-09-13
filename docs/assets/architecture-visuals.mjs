// Original AgentLoop architecture artwork; no media from the visual reference is embedded.
import { theme, brandMarkSvg } from "./brand.mjs";
import {
  glyphSvg,
  brandSvg,
  iconForLabel,
  nodeTitleSize,
  frameworkLabels,
} from "./diagram-icons.mjs";
const P = theme;
const esc = (s) =>
  String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
const label = (
  x,
  y,
  s,
  size = 23,
  color = P.ink,
  weight = 500,
  anchor = "start",
) =>
  `<text x="${x}" y="${y}" fill="${color}" font-family="Arial,Helvetica,sans-serif" font-size="${size}" font-weight="${weight}" text-anchor="${anchor}">${esc(s)}</text>`;
const box = (
  x,
  y,
  w,
  h,
  { fill = P.paper, stroke = P.line, r = 9, dash = false } = {},
) =>
  `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="${r}" fill="${fill}" stroke="${stroke}" stroke-width="1.4" ${dash ? 'stroke-dasharray="6 5"' : ""}/>`;
const path = (d, color = P.wire, width = 1.7, dash = "") =>
  `<path d="${d}" fill="none" stroke="${color}" stroke-width="${width}" stroke-linecap="round" stroke-linejoin="round" ${dash ? `stroke-dasharray="${dash}"` : ""}/>`;
const port = (x, y, color = P.line) =>
  `<circle cx="${x}" cy="${y}" r="3.1" fill="white" stroke="${color}" stroke-width="1.3"/>`;
const arrow = (x, y, color = P.accent) =>
  path(`M${x - 6} ${y - 6}l6 6-6 6`, color, 2);
const mark = (x, y, size = 55) => brandMarkSvg(x, y, size);
const node = (x, y, w, h, title, subtitle = "", accent = false) =>
  box(x, y, w, h, {
    fill: accent ? P.tint : P.paper,
    stroke: accent ? "#c9b7ea" : P.line,
  }) +
  glyphSvg(
    iconForLabel(title),
    x + 18,
    y + (subtitle ? h / 2 - 24 : h / 2 - 14),
    26,
    accent ? P.accent : P.secondary,
  ) +
  label(
    iconForLabel(title) ? x + 54 : x + w / 2,
    y + (subtitle ? h / 2 - 2 : h / 2 + 8),
    title,
    nodeTitleSize(title, w),
    accent ? P.accent : P.ink,
    600,
    iconForLabel(title) ? "start" : "middle",
  ) +
  (subtitle
    ? label(
        x + w / 2,
        y + h / 2 + 27,
        subtitle,
        w < 240 && subtitle.length > 18 ? 17 : 18,
        P.muted,
        400,
        "middle",
      )
    : "");
const pulse = (d, frame, offset = 0) =>
  `<path d="${d}" fill="none" stroke="${P.accent}" stroke-width="2.8" pathLength="100" stroke-dasharray="9 91" stroke-dashoffset="${(-((frame + offset) % 70) * 100) / 70}"/>`;
const svg = (w, h, title, desc, body) =>
  `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-labelledby="title desc"><title id="title">${esc(title)}</title><desc id="desc">${esc(desc)}</desc><defs><pattern id="dots" width="20" height="20" patternUnits="userSpaceOnUse"><circle cx="10" cy="10" r=".65" fill="${P.grid}"/></pattern></defs>${box(0.75, 0.75, w - 1.5, h - 1.5, { r: 12 })}<rect x="2" y="70" width="${w - 4}" height="${h - 142}" fill="${P.canvas}"/><rect x="2" y="70" width="${w - 4}" height="${h - 142}" fill="url(#dots)"/>${body}</svg>\n`;
const diagramDescription =
  "Your agent supplies execution evidence. Without AgentLoop, custom timing, token parsing, retry analysis, output checks and report code must be connected. With AgentLoop, decorators and adapters record a trace for analysis, replay with your fixtures, and reviewable reports.";

function beforeAfterLayer(withAgentLoop, frame) {
  const source = node(
    38,
    224,
    228,
    86,
    "Your agent",
    "models · tools · retries",
  );
  const input = "M266 267H450";
  const outputs = [
    ["Run metrics", "latency · tokens · cost"],
    ["Compare changes", "baseline · candidate · fixtures"],
    ["Review evidence", "HTML · Markdown · CI"],
  ];
  let s = path(input, withAgentLoop ? "#b5a0d8" : P.wire) + source;
  s += label(356, 248, "run evidence", 17, P.muted, 400, "middle");
  if (withAgentLoop) {
    s += box(450, 178, 330, 180, { stroke: "#c9b7ea", fill: "#fcfaff" });
    s += mark(485, 205, 68) + label(568, 252, "AgentLoop", 36, P.ink, 700);
    s += label(
      615,
      306,
      "Record · analyze · replay",
      24,
      P.accent,
      600,
      "middle",
    );
    s += pulse(input, frame);
  } else {
    s +=
      box(450, 98, 330, 340) +
      glyphSvg("settings", 474, 115, 26, P.muted) +
      label(515, 136, "Custom profiling", 27, P.ink, 700) +
      path("M450 151H780", P.line, 1);
    [
      "Timing code",
      "Token / cost parsing",
      "Retry analysis",
      "Output checks",
      "Report formatting",
    ].forEach((name, i) => {
      const y = 187 + i * 51;
      s += glyphSvg(iconForLabel(name), 475, y - 21, 25, P.muted);
      s += label(514, y, name, 24, P.muted, 400);
      if (i < 4) s += path(`M450 ${y + 14}H780`, "#edf1f6", 1);
    });
  }
  outputs.forEach(([title, subtitle], i) => {
    const y = 120 + i * 117,
      cy = y + 43;
    if (withAgentLoop) {
      const d = `M780 267C843 267 852 ${cy} 908 ${cy}`;
      s += path(d, "#b5a0d8", 1.8) + pulse(d, frame, i * 12);
    } else {
      const rows = i === 0 ? [0, 1, 2] : i === 1 ? [0, 1, 2, 3] : [3, 4];
      for (const row of rows) {
        const sy = 178 + row * 51;
        s +=
          path(`M780 ${sy}C839 ${sy} 843 ${cy} 908 ${cy}`, P.wire, 1.1) +
          port(780, sy);
      }
    }
    s += node(908, y, 330, 86, title, subtitle) + port(908, cy);
  });
  return s + port(266, 267) + port(450, 267);
}

export function workflowSvg(frame = 100, mobile = false) {
  if (mobile) return workflowStaticSvg(true);
  const f = ((frame % 210) + 210) % 210;
  // Pause on each architecture; the divider reveals the same scene's counterpart.
  const progress =
    f < 49
      ? 0
      : f < 77
        ? (f - 49) / 28
        : f < 154
          ? 1
          : f < 182
            ? 1 - (f - 154) / 28
            : 0;
  const eased = progress * progress * (3 - 2 * progress);
  const divider = 1278 - 1276 * eased;
  let s = box(366, 15, 548, 43, { fill: "#f8f5fc", r: 8 });
  s += box(368 + (eased > 0.5 ? 274 : 0), 17, 270, 39, {
    fill: eased > 0.5 ? "#eee3fb" : "#f0eaf6",
    stroke: "none",
    r: 6,
  });
  s += label(
    505,
    44,
    "Without",
    24,
    eased > 0.5 ? P.muted : P.ink,
    eased > 0.5 ? 400 : 700,
    "middle",
  );
  s += label(
    780,
    44,
    "With AgentLoop",
    24,
    eased > 0.5 ? P.accent : P.muted,
    eased > 0.5 ? 700 : 400,
    "middle",
  );
  s += beforeAfterLayer(false, frame);
  s += `<defs><clipPath id="with-reveal"><rect x="${divider}" y="72" width="${1279 - divider}" height="392"/></clipPath></defs><g clip-path="url(#with-reveal)"><rect x="2" y="72" width="1276" height="392" fill="${P.canvas}"/><rect x="2" y="72" width="1276" height="392" fill="url(#dots)"/>${beforeAfterLayer(true, frame)}</g>`;
  if (progress > 0 && progress < 1) {
    s +=
      path(`M${divider} 72V464`, P.accent, 1.7) +
      box(divider - 17, 249, 34, 36, {
        fill: P.accent,
        stroke: P.accent,
        r: 8,
      }) +
      path(
        `M${divider - 5} 260l-4 7 4 7M${divider + 5} 260l4 7-4 7`,
        "white",
        2,
      );
  }
  s +=
    path("M1 466H1279", P.line, 1) +
    label(
      27,
      501,
      "Custom profiling work",
      26,
      eased > 0.5 ? P.muted : P.ink,
      700,
    ) +
    label(
      27,
      526,
      "You connect the measurements and checks.",
      18,
      P.muted,
      400,
    );
  s +=
    label(
      1253,
      501,
      "A shared evidence workflow",
      26,
      eased > 0.5 ? P.accent : P.muted,
      700,
      "end",
    ) +
    label(
      1253,
      526,
      "Your agent. Your runs. Your quality fixtures.",
      18,
      P.muted,
      400,
      "end",
    );
  return svg(1280, 548, "Without and with AgentLoop", diagramDescription, s);
}

function mobileLane(y, withAgentLoop) {
  let s = label(
    28,
    y + 33,
    withAgentLoop ? "WITH AGENTLOOP" : "WITHOUT AGENTLOOP",
    19,
    withAgentLoop ? P.accent : P.muted,
    700,
  );
  s += node(127, y + 60, 226, 69, "Your agent", "models · tools · retries");
  s +=
    path(`M240 ${y + 129}V${y + 169}`, P.wire) +
    path(`M234 ${y + 162}l6 7 6-7`, P.wire, 2);
  if (withAgentLoop) {
    s +=
      box(65, y + 170, 350, 125, { stroke: "#c9b7ea", fill: P.tint }) +
      mark(115, y + 186, 48) +
      label(175, y + 224, "AgentLoop", 29, P.ink, 700) +
      label(
        240,
        y + 264,
        "Record · analyze · replay",
        22,
        P.accent,
        500,
        "middle",
      );
  } else {
    s +=
      box(65, y + 170, 350, 125) +
      glyphSvg("settings", 95, y + 185, 26, P.muted) +
      label(135, y + 207, "Custom profiling", 25, P.ink, 600) +
      label(
        240,
        y + 242,
        "Timing · usage · retries",
        20,
        P.muted,
        400,
        "middle",
      ) +
      label(
        240,
        y + 271,
        "Output checks · reports",
        20,
        P.muted,
        400,
        "middle",
      );
  }
  s +=
    path(`M240 ${y + 295}V${y + 334}`, P.wire) +
    path(`M234 ${y + 327}l6 7 6-7`, P.wire, 2);
  s += node(
    52,
    y + 335,
    376,
    76,
    "Metrics · comparisons · reports",
    "Use the quality checks your task needs.",
  );
  return s;
}

export function workflowStaticSvg(mobile = false) {
  if (mobile)
    return svg(
      480,
      965,
      "Without and with AgentLoop",
      diagramDescription,
      mobileLane(0, false) +
        path("M25 449H455", P.line, 1) +
        mobileLane(464, true),
    );
  let s = "";
  for (const [i, on] of [
    [0, false],
    [1, true],
  ]) {
    const y = i * 257;
    s += label(
      31,
      y + 41,
      on ? "WITH AGENTLOOP" : "WITHOUT AGENTLOOP",
      20,
      on ? P.accent : P.muted,
      700,
    );
    s += node(35, y + 100, 225, 80, "Your agent", "models · tools · retries");
    s +=
      path(`M260 ${y + 140}H440`, on ? P.accent : P.wire) +
      arrow(440, y + 140, on ? P.accent : P.wire);
    s += box(440, y + 76, 376, 130, {
      stroke: on ? "#c9b7ea" : P.line,
      fill: on ? P.tint : P.paper,
    });
    if (on)
      s +=
        mark(475, y + 94, 54) +
        label(543, y + 133, "AgentLoop", 31, P.ink, 700) +
        label(
          628,
          y + 174,
          "Record · analyze · replay",
          23,
          P.accent,
          500,
          "middle",
        );
    else
      s +=
        glyphSvg("settings", 467, y + 90, 26, P.muted) +
        label(628, y + 111, "Custom profiling", 27, P.ink, 700, "middle") +
        label(
          628,
          y + 149,
          "Timing · usage · retries",
          21,
          P.muted,
          400,
          "middle",
        ) +
        label(
          628,
          y + 179,
          "Output checks · report code",
          21,
          P.muted,
          400,
          "middle",
        );
    s +=
      path(`M816 ${y + 140}H981`, on ? P.accent : P.wire) +
      arrow(981, y + 140, on ? P.accent : P.wire);
    s += node(
      983,
      y + 99,
      260,
      82,
      "Review the change",
      "metrics · quality · reports",
    );
  }
  s += path("M25 254H1255", P.line, 1);
  return svg(1280, 540, "Without and with AgentLoop", diagramDescription, s);
}

export function frameworksSvg(frame = 0, mobile = false) {
  if (mobile) return architectureMobile();
  let s = label(
    30,
    45,
    "Your agent runs. AgentLoop records the evidence.",
    28,
    P.ink,
    700,
  );
  s +=
    box(30, 92, 1220, 221, { fill: "none", stroke: P.line, dash: true }) +
    label(49, 119, "YOUR APPLICATION", 17, P.muted, 700);
  s += node(55, 178, 176, 70, "Task");
  s += path("M231 213H337", P.secondary, 2) + arrow(337, 213, P.secondary);
  s +=
    box(340, 143, 443, 143) +
    glyphSvg("agent", 446, 159, 30, P.secondary) +
    label(561, 183, "Your agent", 29, P.ink, 700, "middle");
  for (const item of frameworkLabels())
    s +=
      brandSvg(item.icon, item.x, item.y, 25) +
      label(item.labelX, item.labelY, item.name, 20, P.muted);
  for (const [y, name] of [
    [149, "Models"],
    [238, "Tools"],
  ]) {
    const d = `M783 213C873 213 884 ${y + 26} 976 ${y + 26}`;
    s +=
      path(d, P.secondary, 1.8) +
      node(980, y, 235, 52, name) +
      port(980, y + 26);
  }
  s += label(844, 144, "calls", 17, P.muted);
  const capture = "M490 287V342H194V423";
  s +=
    path(capture, P.accent, 2, "6 6") +
    pulse(capture, frame) +
    label(218, 334, "instrumented spans", 18, P.accent, 500);
  s +=
    box(62, 424, 263, 110, { stroke: "#c9b7ea", fill: P.tint }) +
    mark(81, 442, 61) +
    label(153, 478, "AgentLoop", 29, P.ink, 700) +
    label(194, 513, "decorators · adapters", 19, P.muted, 400, "middle");
  const exportPath = "M325 479H402";
  s +=
    path(exportPath, P.accent, 1.8) +
    pulse(exportPath, frame, 10) +
    arrow(402, 479);
  s += node(405, 439, 188, 80, "Trace JSON", "saved run");
  const upper = "M593 479H617V411H675",
    lower = "M617 479V557H675";
  s +=
    path(upper, P.accent, 1.8) +
    path(lower, P.accent, 1.8) +
    pulse(upper, frame, 20) +
    pulse(lower, frame, 40);
  s += node(679, 371, 235, 82, "Analyze", "metrics + findings");
  s += node(679, 506, 235, 97, "Replay", "baseline + your fixtures");
  s += label(
    576,
    628,
    "Replay compares saved runs; you provide the baseline and quality checks.",
    17,
    P.muted,
    400,
    "middle",
  );
  const reportA = "M914 412C966 412 962 471 1004 471",
    reportB = "M914 555C966 555 962 510 1004 510";
  s +=
    path(reportA, P.accent, 1.8) +
    path(reportB, P.accent, 1.8) +
    pulse(reportA, frame, 40) +
    pulse(reportB, frame, 60);
  s += node(1006, 443, 226, 104, "Review / CI", "HTML · Markdown · JSON", true);
  s +=
    path("M1 653H1279", P.line, 1) +
    label(32, 686, "Solid: execution or saved data", 18, P.muted) +
    path("M400 680h40", P.accent, 2, "6 6") +
    label(453, 686, "Dashed: tracing", 18, P.muted) +
    label(
      1244,
      686,
      "Runs locally · framework choice stays in your app",
      18,
      P.accent,
      500,
      "end",
    );
  s += arrow(976, 175, P.secondary) + arrow(976, 264, P.secondary);
  s += path("M188 417l6 6 6-6", P.accent, 2);
  s += arrow(675, 411) + arrow(675, 557) + arrow(1004, 471) + arrow(1004, 510);
  return svg(
    1280,
    708,
    "AgentLoop architecture",
    "Your task runs in your own agent, which calls its models and tools. Decorators and adapters capture spans into AgentLoop traces. Saved JSON can be analyzed for metrics and findings, or compared with a baseline and quality fixtures using replay. Results are exported for review and CI.",
    s,
  );
}

function architectureMobile() {
  let s = label(26, 44, "Your agent. Recorded evidence.", 26, P.ink, 700);
  s +=
    box(20, 92, 440, 342, { fill: "none", dash: true }) +
    label(37, 118, "YOUR APPLICATION", 16, P.muted, 700);
  s +=
    node(155, 139, 170, 58, "Task") +
    path("M240 197v31", P.secondary, 2) +
    path("M234 221l6 7 6-7", P.secondary, 2);
  s +=
    box(43, 231, 394, 105) +
    glyphSvg("agent", 128, 242, 27, P.secondary) +
    label(240, 263, "Your agent", 26, P.ink, 700, "middle");
  for (const item of frameworkLabels(true))
    s +=
      brandSvg(item.icon, item.x, item.y, 23) +
      label(item.labelX, item.labelY, item.name, 18, P.muted);
  s +=
    path("M139 336v35M341 336v35", P.secondary, 2) +
    node(51, 372, 178, 42, "Models") +
    node(251, 372, 178, 42, "Tools");
  s +=
    path("M240 337v113", P.accent, 2, "6 6") +
    label(253, 448, "spans", 18, P.accent);
  s +=
    box(90, 456, 300, 109, { stroke: "#c9b7ea", fill: P.tint }) +
    mark(121, 470, 56) +
    label(191, 505, "AgentLoop", 29, P.ink, 700) +
    label(240, 542, "decorators · adapters", 20, P.muted, 400, "middle");
  s +=
    path("M240 565v45", P.accent, 2) +
    path("M234 603l6 7 6-7", P.accent, 2) +
    node(117, 615, 246, 74, "Trace JSON", "saved run");
  s += path("M240 689v27H129v32M240 716h111v32", P.accent, 1.8);
  s +=
    node(29, 753, 200, 90, "Analyze", "metrics + findings") +
    node(251, 753, 200, 90, "Replay", "baseline + fixtures");
  s +=
    path("M129 843v41H240v37M351 843v41H240", P.accent, 1.8) +
    path("M234 914l6 7 6-7", P.accent, 2);
  s += node(74, 926, 332, 90, "Review / CI", "HTML · Markdown · JSON", true);
  s += label(
    240,
    1060,
    "Framework choice stays in your app.",
    19,
    P.accent,
    500,
    "middle",
  );
  return svg(
    480,
    1110,
    "AgentLoop architecture",
    "Your agent calls models and tools. AgentLoop records instrumented spans and exports trace JSON. Analyze produces metrics and findings. Replay compares the trace with a baseline and your quality fixtures. Both produce reviewable reports.",
    s,
  );
}
