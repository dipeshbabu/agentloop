// Shared AgentLoop identity for the README, SVGs, and Remotion diagrams.
export const theme = {
  paper: "#ffffff",
  canvas: "#fdfbff",
  grid: "#eae2f3",
  ink: "#30263d",
  muted: "#766c83",
  line: "#dfd6e9",
  wire: "#c0b4cf",
  accent: "#7038c9",
  secondary: "#d96559",
  tint: "#f5effd",
  borderAccent: "#c9b7ea",
  accentLine: "#b5a0d8",
  soft: "#fcfaff",
  selected: "#eee3fb",
};

// Two offset, interlocking circuit brackets form a compact loop emblem.
export const markPaths = [
  "M46 10 16 28 16 64 46 82 46 63 32 55 32 37 46 29Z",
  "M54 18 84 36 84 72 54 90 54 71 68 63 68 45 54 37Z",
];
export function brandMarkSvg(x, y, size = 60, dark = false) {
  const colors = dark
    ? ["#c7a1ff", "#ff9c8f"]
    : [theme.accent, theme.secondary];
  return `<g transform="translate(${x} ${y}) scale(${size / 100})">${markPaths.map((d, i) => `<path d="${d}" fill="${colors[i]}"/>`).join("")}</g>`;
}
export function logoSvg(dark = false) {
  const ink = dark ? "#f5efff" : theme.ink;
  return `<svg xmlns="http://www.w3.org/2000/svg" width="610" height="140" viewBox="0 0 610 140" role="img" aria-labelledby="title desc"><title id="title">AgentLoop</title><desc id="desc">Two interlocking violet and coral circuit brackets beside the AgentLoop wordmark.</desc>${brandMarkSvg(8, 10, 120, dark)}<text x="150" y="101" font-family="Arial,Helvetica,sans-serif" font-size="85" font-weight="700" letter-spacing="-4" fill="${ink}">AgentLoop</text></svg>\n`;
}
