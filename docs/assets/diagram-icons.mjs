import { brandIcons } from "./brand-icons.mjs";
export { brandIcons };

// Original outline icons, shared by the static SVG and Remotion renderers.
export const glyphPaths = {
  agent: [
    "M8 5h8v3H8zM12 2v3M5 8h14v12H5zM2 12v4M22 12v4M9 12v2M15 12v2M9 17h6",
  ],
  task: ["M9 4H6v17h12V4h-3M9 2h6v4H9zM8 12l2 2 5-5M8 18h7"],
  model: [
    "M6 6h12v12H6zM9 9h6v6H9zM9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4",
  ],
  tool: ["M14 3l-3 3v5L3 19l2 2 8-8h5l3-3-6 1-2-2z"],
  trace: ["M14 2H5v20h15V8zM14 2v6h6M9 12l-2 2 2 2M15 12l2 2-2 2"],
  analyze: ["M4 3v17h16M8 15v-4M12 15V7M16 15V9"],
  replay: ["M8 3v17M4 7l4-4 4 4M16 4v17M12 17l4 4 4-4"],
  report: ["M14 2H5v20h15V8zM14 2v6h6M8 15l3 3 5-6"],
  clock: ["M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0M12 7v5l3 2"],
  tokens: [
    "M4 6c0-4 12-4 12 0s-12 4-12 0M4 6v6c0 4 12 4 12 0V6M8 16v3c0 3 12 3 12 0v-7M16 9c3 0 4 1 4 3s-4 4-8 3",
  ],
  retry: [
    "M19 7a8 8 0 0 0-13-1L3 9M3 4v5h5M5 17a8 8 0 0 0 13 1l3-3M21 20v-5h-5",
  ],
  check: ["M4 4h16v16H4zM8 12l3 3 5-6"],
  settings: ["M3 7h18M3 17h18M8 3v8M16 13v8"],
};
const titleIcons = {
  "Your agent": "agent",
  Task: "task",
  Models: "model",
  Tools: "tool",
  "Trace JSON": "trace",
  Analyze: "analyze",
  Replay: "replay",
  "Review / CI": "report",
  "Run metrics": "analyze",
  "Compare changes": "replay",
  "Review evidence": "report",
  "Review the change": "report",
  "Metrics · comparisons · reports": "report",
  "Timing code": "clock",
  "Token / cost parsing": "tokens",
  "Retry analysis": "retry",
  "Output checks": "check",
  "Report formatting": "report",
  "Custom profiling": "settings",
};
export const iconForLabel = (title) => titleIcons[title];
export const nodeTitleSize = (title, width) =>
  title.length > 24 ? 20 : title.length > 16 ? 22 : width < 200 ? 22 : 25;
export const glyphSvg = (name, x, y, size = 26, color = "#d96559") =>
  name
    ? `<svg x="${x}" y="${y}" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="${color}" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${glyphPaths[name].map((d) => `<path d="${d}"/>`).join("")}</svg>`
    : "";
export const brandSvg = (name, x, y, size = 24) => {
  const icon = brandIcons[name];
  return `<svg x="${x}" y="${y}" width="${size}" height="${size}" viewBox="${icon.viewBox}" fill="${icon.color}" fill-rule="${icon.fillRule}" aria-hidden="true">${icon.paths.map((d) => `<path d="${d}"/>`).join("")}</svg>`;
};
export function frameworkLabels(mobile = false) {
  return mobile
    ? [
        {
          name: "Python",
          icon: "python",
          x: 65,
          y: 268,
          labelX: 97,
          labelY: 287,
        },
        {
          name: "OpenAI SDK",
          icon: "openai",
          x: 255,
          y: 268,
          labelX: 287,
          labelY: 287,
        },
        {
          name: "LangGraph",
          icon: "langgraph",
          x: 65,
          y: 301,
          labelX: 97,
          labelY: 320,
        },
        {
          name: "CrewAI",
          icon: "crewai",
          x: 255,
          y: 301,
          labelX: 287,
          labelY: 320,
        },
      ]
    : [
        {
          name: "Custom Python",
          icon: "python",
          x: 375,
          y: 201,
          labelX: 413,
          labelY: 221,
        },
        {
          name: "OpenAI SDK",
          icon: "openai",
          x: 590,
          y: 201,
          labelX: 628,
          labelY: 221,
        },
        {
          name: "LangGraph",
          icon: "langgraph",
          x: 375,
          y: 237,
          labelX: 413,
          labelY: 257,
        },
        {
          name: "CrewAI",
          icon: "crewai",
          x: 590,
          y: 237,
          labelX: 628,
          labelY: 257,
        },
      ];
}
