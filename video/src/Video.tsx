import React, { useEffect, useState } from "react";
import {
  AbsoluteFill,
  Img,
  Sequence,
  cancelRender,
  continueRender,
  delayRender,
  interpolate,
  spring,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import "@fontsource/inter/400.css";
import "@fontsource/inter/500.css";
import "@fontsource/inter/600.css";
import "@fontsource/inter/700.css";
import "@fontsource/jetbrains-mono/400.css";
import "@fontsource/jetbrains-mono/600.css";
import data from "./demo-data.json";
import storyboard from "./storyboard.json";

const C = {
  bg: "#08121C",
  panel: "#101F2C",
  line: "#263B4B",
  ink: "#F2F6F8",
  muted: "#91A7B8",
  green: "#70EDC3",
  blue: "#8BB8FF",
  amber: "#F2BD72",
  red: "#FF8693",
};
const mono = '"JetBrains Mono", monospace';
const panel: React.CSSProperties = {
  background: C.panel,
  border: `1px solid ${C.line}`,
  borderRadius: 20,
};
const clamp = {
  extrapolateLeft: "clamp" as const,
  extrapolateRight: "clamp" as const,
};
const totalFrames = storyboard.scenes.reduce(
  (sum, scene) => sum + scene.seconds * storyboard.fps,
  0,
);
const rise = (frame: number, delay = 0) =>
  spring({
    frame: frame - delay,
    fps: 30,
    config: { damping: 200 },
    durationInFrames: 28,
  });

const Reveal: React.FC<{
  children: React.ReactNode;
  delay?: number;
  style?: React.CSSProperties;
}> = ({ children, delay = 0, style }) => {
  const frame = useCurrentFrame();
  const value = rise(frame, delay);
  return (
    <div
      style={{
        opacity: value,
        transform: `translateY(${24 * (1 - value)}px)`,
        ...style,
      }}
    >
      {children}
    </div>
  );
};

const Pill: React.FC<{ children: React.ReactNode; color?: string }> = ({
  children,
  color = C.green,
}) => (
  <span
    style={{
      display: "inline-flex",
      alignItems: "center",
      gap: 10,
      padding: "10px 16px",
      border: `1px solid ${color}55`,
      color,
      background: `${color}0C`,
      borderRadius: 8,
      fontSize: 20,
      fontWeight: 500,
    }}
  >
    {children}
  </span>
);

const Logo: React.FC<{ size?: number }> = ({ size = 36 }) => (
  <div
    style={{
      display: "flex",
      alignItems: "center",
      gap: 16,
      fontSize: size,
      fontWeight: 600,
      letterSpacing: -1.4,
    }}
  >
    <svg width={size + 5} height={size + 5} viewBox="0 0 40 40">
      <path
        d="M10 12H28V28H10Z"
        fill="none"
        stroke={C.green}
        strokeWidth="2.5"
        strokeLinejoin="round"
      />
      <circle
        cx="10"
        cy="12"
        r="5"
        fill={C.bg}
        stroke={C.green}
        strokeWidth="2.5"
      />
      <circle cx="28" cy="28" r="5" fill={C.green} />
    </svg>
    AgentLoop
  </div>
);

const Title: React.FC<{
  eyebrow?: string;
  children: React.ReactNode;
  style?: React.CSSProperties;
  size?: number;
}> = ({ eyebrow, children, style, size = 66 }) => (
  <Reveal style={style}>
    {eyebrow && (
      <div
        style={{
          color: C.green,
          fontSize: 19,
          fontWeight: 600,
          letterSpacing: 2,
          marginBottom: 22,
        }}
      >
        {eyebrow}
      </div>
    )}
    <h1
      style={{
        fontSize: size,
        lineHeight: 1.08,
        letterSpacing: -2.7,
        fontWeight: 600,
        margin: 0,
      }}
    >
      {children}
    </h1>
  </Reveal>
);

const Terminal: React.FC<{
  command: string;
  output?: React.ReactNode;
  title?: string;
  typeFrames?: number;
  style?: React.CSSProperties;
  fontSize?: number;
}> = ({
  command,
  output,
  title = "terminal",
  typeFrames = 75,
  style,
  fontSize = 25,
}) => {
  const frame = useCurrentFrame();
  const count = Math.floor(
    interpolate(frame, [15, 15 + typeFrames], [0, command.length], clamp),
  );
  return (
    <div
      style={{
        ...panel,
        overflow: "hidden",
        boxShadow: "0 28px 80px #0005",
        ...style,
      }}
    >
      <div
        style={{
          height: 58,
          borderBottom: `1px solid ${C.line}`,
          display: "flex",
          alignItems: "center",
          padding: "0 26px",
          gap: 9,
        }}
      >
        {["#FF7C87", "#F2BD72", "#70EDC3"].map((color) => (
          <div
            key={color}
            style={{
              width: 10,
              height: 10,
              borderRadius: 10,
              background: color,
              opacity: 0.75,
            }}
          />
        ))}
        <span
          style={{
            fontFamily: mono,
            fontSize: 17,
            color: C.muted,
            marginLeft: 17,
          }}
        >
          {title}
        </span>
      </div>
      <div style={{ padding: 32, fontFamily: mono, fontSize, lineHeight: 1.7 }}>
        <div style={{ whiteSpace: "pre-wrap", color: C.ink }}>
          <span style={{ color: C.green }}>$ </span>
          {command.slice(0, count)}
          <span
            style={{
              opacity:
                count < command.length || Math.floor(frame / 15) % 2 === 0
                  ? 1
                  : 0,
              color: C.green,
            }}
          >
            ▌
          </span>
        </div>
        <div
          style={{
            marginTop: 30,
            opacity: interpolate(
              frame,
              [typeFrames + 20, typeFrames + 36],
              [0, 1],
              clamp,
            ),
          }}
        >
          {output}
        </div>
      </div>
    </div>
  );
};

const Code: React.FC = () => {
  const lines = [
    "import agentloop",
    "",
    '@agentloop.trace_tool(name="lookup")',
    "def lookup(topic):",
    '    return {"agent": "ok"}[topic]',
    "",
    'with agentloop.trace_agent("research") as trace:',
    '    trace.metadata["output"] = lookup("agent")',
    "",
    'trace.export_json("runs/baseline.json")',
  ];
  return (
    <div style={{ ...panel, width: 1120, overflow: "hidden" }}>
      <div
        style={{
          padding: "20px 30px",
          borderBottom: `1px solid ${C.line}`,
          color: C.muted,
          fontFamily: mono,
          fontSize: 20,
        }}
      >
        example_agent.py
      </div>
      <div style={{ padding: "28px 0" }}>
        {lines.map((line, index) => (
          <Reveal
            key={index}
            delay={index * 3}
            style={{
              display: "flex",
              padding: "4px 25px",
              lineHeight: 1.7,
              fontFamily: mono,
              fontSize: 24,
              background: [2, 6, 9].includes(index)
                ? "#70EDC308"
                : "transparent",
            }}
          >
            <span
              style={{
                width: 48,
                color: "#4C6578",
                fontSize: 19,
                textAlign: "right",
                paddingRight: 24,
              }}
            >
              {index + 1}
            </span>
            <span
              style={{
                whiteSpace: "pre",
                color: line.startsWith("@")
                  ? C.green
                  : line.startsWith("with") ||
                      line.startsWith("import") ||
                      line.startsWith("def")
                    ? C.blue
                    : C.ink,
              }}
            >
              {line}
            </span>
          </Reveal>
        ))}
      </div>
    </div>
  );
};

const Flow: React.FC = () => {
  const frame = useCurrentFrame();
  const nodes = [
    { x: 0, y: 165, label: "agent", kind: "WORKFLOW" },
    { x: 230, y: 20, label: "lookup", kind: "TOOL" },
    { x: 230, y: 165, label: "lookup", kind: "TOOL" },
    { x: 230, y: 310, label: "lookup", kind: "TOOL" },
    { x: 465, y: 165, label: "answer", kind: "MODEL" },
  ];
  return (
    <svg
      width="650"
      height="450"
      viewBox="-10 -10 680 450"
      style={{ overflow: "visible" }}
    >
      {[55, 200, 345].map((y, i) => (
        <g key={y}>
          <path
            d={`M160 200 C190 200 190 ${y} 230 ${y}`}
            fill="none"
            stroke={C.line}
            strokeWidth="2"
          />
          <path
            d={`M390 ${y} C430 ${y} 430 200 465 200`}
            fill="none"
            stroke={C.line}
            strokeWidth="2"
          />
          {[
            `M160 200 C190 200 190 ${y} 230 ${y}`,
            `M390 ${y} C430 ${y} 430 200 465 200`,
          ].map((path, segment) => (
            <path
              key={segment}
              d={path}
              fill="none"
              stroke={C.green}
              strokeWidth="3"
              pathLength={100}
              strokeDasharray="10 90"
              strokeDashoffset={-((frame + i * 20 + segment * 40) % 100)}
              opacity={rise(frame, 15) * 0.8}
            />
          ))}
        </g>
      ))}
      {nodes.map((node, index) => (
        <g key={index} opacity={rise(frame, index * 6)}>
          <rect
            x={node.x}
            y={node.y}
            width="160"
            height="76"
            rx="12"
            fill={C.panel}
            stroke={index === 4 ? C.blue : C.line}
            strokeWidth="1.5"
          />
          <text
            x={node.x + 18}
            y={node.y + 27}
            fill={C.muted}
            fontFamily="Inter"
            fontSize="12"
            letterSpacing="2"
          >
            {node.kind}
          </text>
          <text
            x={node.x + 18}
            y={node.y + 55}
            fill={index === 4 ? C.blue : C.ink}
            fontFamily="JetBrains Mono"
            fontSize="21"
          >
            {node.label}
          </text>
        </g>
      ))}
    </svg>
  );
};

type Trace = typeof data.baseline;
const Timeline: React.FC<{
  trace: Trace;
  width: number;
  compact?: boolean;
  accent?: string;
  delay?: number;
}> = ({ trace, width, compact = false, accent = C.green, delay = 0 }) => {
  const frame = useCurrentFrame();
  const max = data.replay.baseline.runtime_ms;
  const labelWidth = compact ? 128 : 170;
  const chartWidth = width - labelWidth - 30;
  const height = compact ? 52 : 75;
  return (
    <div style={{ width }}>
      <div
        style={{
          marginLeft: labelWidth,
          position: "relative",
          width: chartWidth,
          height: 39,
          fontFamily: mono,
          fontSize: 17,
          color: C.muted,
        }}
      >
        {[0, 200, 400, max].map((v) => (
          <span
            key={v}
            style={{
              position: "absolute",
              left: (v / max) * chartWidth,
              whiteSpace: "nowrap",
              transform:
                v === 0
                  ? undefined
                  : v === max
                    ? "translateX(-100%)"
                    : "translateX(-50%)",
            }}
          >
            {v} ms
          </span>
        ))}
      </div>
      {trace.events.map((event, index) => {
        const offset =
          Date.parse(event.started_at) - Date.parse(trace.started_at);
        const color = event.event_type === "model_call" ? C.blue : accent;
        const reveal = rise(frame, delay + index * 7);
        return (
          <div
            key={event.event_id}
            style={{
              display: "flex",
              alignItems: "center",
              height,
              opacity: reveal,
            }}
          >
            <div
              style={{
                width: labelWidth,
                fontFamily: mono,
                fontSize: compact ? 19 : 23,
                color: event.event_type === "model_call" ? C.blue : C.muted,
              }}
            >
              {event.name}
              {event.name === "lookup" ? ` ${index + 1}` : ""}
            </div>
            <div
              style={{
                width: chartWidth,
                height: compact ? 32 : 44,
                position: "relative",
                background: "#ffffff04",
                borderRadius: 6,
              }}
            >
              <div
                style={{
                  position: "absolute",
                  left: (offset / max) * chartWidth,
                  width: (event.duration_ms / max) * chartWidth * reveal,
                  height: "100%",
                  background: `${color}25`,
                  border: `1px solid ${color}90`,
                  borderRadius: 6,
                  overflow: "hidden",
                }}
              >
                <div
                  style={{
                    paddingLeft: 12,
                    color,
                    fontFamily: mono,
                    fontSize: compact ? 16 : 19,
                    lineHeight: compact ? "30px" : "42px",
                    whiteSpace: "nowrap",
                  }}
                >
                  {event.duration_ms} ms
                </div>
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
};

const Intro: React.FC = () => (
  <>
    <Title
      eyebrow="LOCAL-FIRST AGENT PROFILING"
      size={104}
      style={{ position: "absolute", left: 90, top: 265, width: 1070 }}
    >
      Make every
      <br />
      agent change
      <br />
      <span style={{ color: C.green }}>measurable.</span>
    </Title>
    <Reveal delay={18} style={{ position: "absolute", left: 1180, top: 280 }}>
      <Flow />
    </Reveal>
    <Reveal
      delay={32}
      style={{
        position: "absolute",
        left: 94,
        top: 690,
        display: "flex",
        gap: 22,
        alignItems: "center",
      }}
    >
      {["Trace", "Find", "Change", "Verify"].map((text, i) => (
        <React.Fragment key={text}>
          {i > 0 && <span style={{ color: "#486779", fontSize: 26 }}>→</span>}
          <Pill color={i === 3 ? C.green : C.muted}>{text}</Pill>
        </React.Fragment>
      ))}
    </Reveal>
  </>
);

const Quickstart: React.FC = () => (
  <>
    <Title style={{ position: "absolute", left: 90, top: 220, width: 600 }}>
      Your first trace.
      <br />
      <span style={{ color: C.green }}>No API key.</span>
    </Title>
    <Reveal
      delay={18}
      style={{
        position: "absolute",
        left: 94,
        top: 438,
        maxWidth: 510,
        color: C.muted,
        fontSize: 29,
        lineHeight: 1.55,
      }}
    >
      Start with the local example.
      <br />
      Then use the same tools on your own agent.
    </Reveal>
    <Reveal
      delay={115}
      style={{
        position: "absolute",
        left: 94,
        top: 646,
        display: "flex",
        gap: 18,
      }}
    >
      <Pill>{data.quickstart.spanCount} spans</Pill>
      <Pill color={C.blue}>{data.quickstart.findingCount} findings</Pill>
    </Reveal>
    <Reveal delay={8} style={{ position: "absolute", left: 745, top: 200 }}>
      <Terminal
        title="source checkout · main"
        command={
          "git clone https://github.com/dipeshbabu/agentloop.git\ncd agentloop\nuv sync --locked\nuv run agentloop quickstart"
        }
        typeFrames={135}
        style={{ width: 1085, minHeight: 570 }}
        fontSize={24}
        output={
          <>
            <div style={{ color: C.green }}>✓ Synthetic trace created</div>
            <div style={{ color: C.muted, marginTop: 14 }}>
              Runtime {data.quickstart.runtimeMs.toFixed(0)} ms
              <br />
              Spans {data.quickstart.spanCount}
              <br />
              Findings {data.quickstart.findingCount}
            </div>
          </>
        }
      />
    </Reveal>
  </>
);

const Instrument: React.FC = () => (
  <>
    <Title style={{ position: "absolute", left: 90, top: 160 }}>
      Trace the code you already run.
    </Title>
    <Reveal delay={10} style={{ position: "absolute", left: 90, top: 310 }}>
      <Code />
    </Reveal>
    <div style={{ position: "absolute", left: 1280, top: 352, width: 525 }}>
      {[
        { title: "Wrap a function", body: "Capture duration and status." },
        {
          title: "Keep the output",
          body: "Choose what quality checks evaluate.",
        },
        { title: "Export a trace", body: "One portable JSON file." },
      ].map((item, index) => (
        <Reveal
          key={item.title}
          delay={30 + index * 25}
          style={{ display: "flex", gap: 22, marginBottom: 55 }}
        >
          <div
            style={{
              width: 44,
              height: 44,
              border: `1px solid ${C.green}60`,
              borderRadius: 50,
              color: C.green,
              display: "grid",
              placeItems: "center",
              fontFamily: mono,
              flexShrink: 0,
            }}
          >
            {index + 1}
          </div>
          <div>
            <div style={{ fontSize: 29, fontWeight: 600, marginBottom: 12 }}>
              {item.title}
            </div>
            <div style={{ color: C.muted, fontSize: 24, lineHeight: 1.5 }}>
              {item.body}
            </div>
          </div>
        </Reveal>
      ))}
    </div>
  </>
);

const Finding: React.FC = () => (
  <>
    <Title style={{ position: "absolute", left: 90, top: 160 }}>
      See the bottleneck.
      <br />
      <span style={{ color: C.green }}>Read the assumptions.</span>
    </Title>
    <Reveal
      delay={12}
      style={{
        position: "absolute",
        left: 94,
        top: 355,
        color: C.muted,
        fontFamily: mono,
        fontSize: 23,
      }}
    >
      uv run agentloop analyze runs/video-evidence/baseline.json
    </Reveal>
    <Reveal
      delay={25}
      style={{
        ...panel,
        position: "absolute",
        left: 90,
        top: 438,
        padding: 30,
        width: 1080,
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: 23,
        }}
      >
        <span style={{ fontSize: 24, fontWeight: 500 }}>Recorded baseline</span>
        <span style={{ fontFamily: mono, fontSize: 29, color: C.ink }}>
          {data.replay.baseline.runtime_ms} ms
        </span>
      </div>
      <Timeline trace={data.baseline} width={1015} />
    </Reveal>
    <Reveal
      delay={70}
      style={{
        ...panel,
        position: "absolute",
        left: 1220,
        top: 200,
        width: 610,
        padding: 34,
        borderColor: "#70EDC365",
      }}
    >
      <Pill>Optimization finding</Pill>
      <h2
        style={{
          fontSize: 36,
          lineHeight: 1.25,
          fontWeight: 500,
          margin: "28px 0",
        }}
      >
        Parallelize repeated
        <br />
        <span style={{ fontFamily: mono, color: C.green }}>lookup</span> calls
      </h2>
      <div style={{ display: "flex", gap: 12, marginBottom: 30 }}>
        <Pill color={C.muted}>{data.finding.evidence_level} evidence</Pill>
        <Pill color={C.muted}>{data.finding.confidence} confidence</Pill>
      </div>
      <div style={{ color: C.amber, fontSize: 21 }}>
        PREDICTED SAVINGS · UNCALIBRATED
      </div>
      <div
        style={{
          fontSize: 72,
          fontWeight: 600,
          letterSpacing: -3,
          margin: "10px 0 26px",
        }}
      >
        {data.finding.savings.estimated_latency_savings_ms}
        <span style={{ fontSize: 28, color: C.muted, letterSpacing: 0 }}>
          {" "}
          ms
        </span>
      </div>
      <div
        style={{
          borderTop: `1px solid ${C.line}`,
          paddingTop: 24,
          color: C.muted,
          fontSize: 23,
          lineHeight: 1.5,
        }}
      >
        Check first:
        <br />
        Independent outputs.
        <br />
        No shared-state conflicts.
      </div>
    </Reveal>
  </>
);

const Compare: React.FC = () => {
  const frame = useCurrentFrame();
  const pct = interpolate(
    frame,
    [52, 105],
    [0, data.replay.deltas.latency_improvement_pct],
    clamp,
  );
  return (
    <>
      <Title style={{ position: "absolute", left: 90, top: 155 }}>
        Change one thing. Compare the runs.
      </Title>
      <div
        style={{
          position: "absolute",
          left: 90,
          top: 295,
          display: "flex",
          gap: 26,
        }}
      >
        {[
          {
            trace: data.baseline,
            name: "BASELINE · SERIAL",
            runtime: data.replay.baseline.runtime_ms,
            color: C.muted,
          },
          {
            trace: data.candidate,
            name: "CANDIDATE · CONCURRENT",
            runtime: data.replay.candidate.runtime_ms,
            color: C.green,
          },
        ].map((item, index) => (
          <Reveal
            key={item.name}
            delay={10 + index * 20}
            style={{ ...panel, width: 850, padding: "26px 28px" }}
          >
            <div
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                marginBottom: 25,
              }}
            >
              <span
                style={{ fontSize: 19, letterSpacing: 1, color: item.color }}
              >
                {item.name}
              </span>
              <span style={{ fontSize: 42, fontWeight: 600 }}>
                {item.runtime}
                <span style={{ fontSize: 20, color: C.muted }}> ms</span>
              </span>
            </div>
            <Timeline
              trace={item.trace}
              width={785}
              compact
              accent={item.color}
              delay={index * 15}
            />
          </Reveal>
        ))}
      </div>
      <Reveal
        delay={65}
        style={{
          position: "absolute",
          left: 100,
          top: 712,
          display: "flex",
          gap: 35,
          alignItems: "center",
        }}
      >
        <div
          style={{
            fontSize: 75,
            color: C.green,
            fontWeight: 600,
            letterSpacing: -3,
          }}
        >
          {pct.toFixed(2)}%
        </div>
        <div style={{ fontSize: 26, lineHeight: 1.5 }}>
          lower runtime
          <br />
          <span style={{ color: C.muted, fontSize: 22 }}>
            Measured in this synthetic fixture
          </span>
        </div>
      </Reveal>
      <Reveal delay={90} style={{ position: "absolute", left: 100, top: 835 }}>
        <Pill>✓ Quality checks passed</Pill>
      </Reveal>
      <Reveal
        delay={118}
        style={{
          position: "absolute",
          left: 1020,
          top: 702,
          width: 800,
          fontFamily: mono,
          fontSize: 20,
          lineHeight: 1.65,
          color: C.muted,
        }}
      >
        <span style={{ color: C.ink }}>uv run agentloop replay</span>
        <br /> --baseline runs/video-evidence/baseline.json
        <br /> --candidate runs/video-evidence/candidate.json
        <br /> --quality-fixtures runs/video-evidence/quality.json
      </Reveal>
    </>
  );
};

const Study: React.FC = () => (
  <>
    <Title style={{ position: "absolute", left: 90, top: 190, width: 665 }}>
      Faster is only
      <br />
      <span style={{ color: C.green }}>half the test.</span>
    </Title>
    <Reveal delay={35} style={{ position: "absolute", left: 94, top: 440 }}>
      <span
        style={{
          fontSize: 130,
          fontWeight: 600,
          letterSpacing: -6,
          color: C.green,
        }}
      >
        {data.outcomes.configured_gates_passed_count}
      </span>
      <span style={{ fontSize: 75, color: "#526B7E" }}>
        {" "}
        / {data.outcomes.intervention_count}
      </span>
      <div style={{ fontSize: 28, marginTop: 6 }}>configured gate passes</div>
      <div
        style={{ fontSize: 24, color: C.muted, marginTop: 22, lineHeight: 1.6 }}
      >
        3 tasks × 2 seeds
        <br />
        Keep failed and unknown-cost cases.
      </div>
    </Reveal>
    <Reveal
      delay={10}
      style={{
        ...panel,
        position: "absolute",
        left: 870,
        top: 190,
        width: 950,
        overflow: "hidden",
      }}
    >
      <div
        style={{
          padding: "24px 30px",
          fontSize: 24,
          borderBottom: `1px solid ${C.line}`,
        }}
      >
        Paired intervention study
      </div>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "1fr 1fr 1.4fr 1.4fr",
          padding: "20px 30px",
          color: C.muted,
          fontSize: 17,
          letterSpacing: 1,
        }}
      >
        {["TASK", "SEED", "QUALITY", "GATES"].map((t) => (
          <span key={t}>{t}</span>
        ))}
      </div>
      {data.pairs.map((pair, index) => (
        <Reveal
          key={`${pair.task}-${pair.seed}`}
          delay={22 + index * 10}
          style={{
            display: "grid",
            gridTemplateColumns: "1fr 1fr 1.4fr 1.4fr",
            alignItems: "center",
            height: 71,
            padding: "0 30px",
            borderTop: `1px solid ${C.line}`,
            fontSize: 23,
            background: pair.passed ? "transparent" : "#FF86930B",
          }}
        >
          <span style={{ fontFamily: mono }}>0{pair.task + 1}</span>
          <span style={{ color: C.muted, fontFamily: mono }}>{pair.seed}</span>
          <span style={{ color: pair.qualityPassed ? C.green : C.red }}>
            {pair.qualityPassed ? "✓ Pass" : "× Fail"}
          </span>
          <span style={{ color: pair.passed ? C.green : C.red }}>
            {pair.passed ? "Pass" : "Fail"}
          </span>
        </Reveal>
      ))}
      <Reveal
        delay={98}
        style={{
          padding: "24px 30px",
          borderTop: `1px solid ${C.line}`,
          color: C.amber,
          fontSize: 21,
        }}
      >
        Last pair: quality failed · cost unavailable
      </Reveal>
    </Reveal>
  </>
);

const Share: React.FC = () => (
  <>
    <Title style={{ position: "absolute", left: 90, top: 220, width: 600 }}>
      Keep the evidence.
      <br />
      <span style={{ color: C.green }}>Share one file.</span>
    </Title>
    <Reveal
      delay={25}
      style={{
        position: "absolute",
        left: 94,
        top: 450,
        color: C.muted,
        fontSize: 27,
        lineHeight: 1.65,
        width: 565,
      }}
    >
      Trace links. Estimator assumptions.
      <br />
      Before-and-after quality checks.
      <br />
      An HTML report that opens offline.
    </Reveal>
    <Reveal
      delay={50}
      style={{
        position: "absolute",
        left: 94,
        top: 655,
        fontFamily: mono,
        fontSize: 19,
        lineHeight: 1.7,
        color: C.green,
      }}
    >
      uv run agentloop analyze
      <br />
      <span style={{ color: C.muted }}>
        {" "}
        runs/video-evidence/candidate.json
      </span>
      <br />
      <span style={{ color: C.muted }}> --baseline</span>
      <br />
      <span style={{ color: C.muted }}> runs/video-evidence/baseline.json</span>
      <br />
      <span style={{ color: C.muted }}> --html report.html</span>
    </Reveal>
    <Reveal
      delay={8}
      style={{
        position: "absolute",
        left: 820,
        top: 168,
        width: 1000,
        height: 620,
        borderRadius: 17,
        overflow: "hidden",
        border: `1px solid ${C.line}`,
        boxShadow: "0 25px 80px #0005",
      }}
    >
      <div
        style={{
          height: 48,
          background: "#E3EBEF",
          color: "#526675",
          display: "flex",
          alignItems: "center",
          paddingLeft: 25,
          fontSize: 18,
          fontFamily: mono,
        }}
      >
        report.html{" "}
        <span style={{ marginLeft: "auto", marginRight: 25, fontSize: 15 }}>
          LOCAL FILE
        </span>
      </div>
      <Img
        src={staticFile("report.png")}
        style={{ width: "100%", display: "block" }}
      />
    </Reveal>
    <Reveal
      delay={60}
      style={{
        position: "absolute",
        left: 1000,
        top: 818,
        display: "flex",
        gap: 14,
      }}
    >
      {["intervention.json", "study-results.json", "report.html"].map(
        (file) => (
          <Pill key={file} color={C.muted}>
            {file}
          </Pill>
        ),
      )}
    </Reveal>
  </>
);

const Outro: React.FC = () => (
  <>
    <Reveal
      style={{
        position: "absolute",
        left: 0,
        right: 0,
        top: 235,
        textAlign: "center",
      }}
    >
      <h1
        style={{
          fontSize: 115,
          fontWeight: 600,
          letterSpacing: -5,
          lineHeight: 1.15,
          margin: 0,
        }}
      >
        Trace. Change.
        <br />
        <span style={{ color: C.green }}>Verify.</span>
      </h1>
    </Reveal>
    <Reveal
      delay={22}
      style={{
        position: "absolute",
        left: 0,
        right: 0,
        top: 550,
        display: "flex",
        justifyContent: "center",
      }}
    >
      <div
        style={{
          ...panel,
          padding: "23px 44px",
          color: C.ink,
          fontFamily: mono,
          fontSize: 30,
        }}
      >
        <span style={{ color: C.green }}>$ </span>uv run agentloop quickstart
      </div>
    </Reveal>
    <Reveal
      delay={36}
      style={{
        position: "absolute",
        left: 0,
        right: 0,
        top: 680,
        textAlign: "center",
        fontSize: 28,
        color: C.muted,
      }}
    >
      github.com/dipeshbabu/agentloop
    </Reveal>
  </>
);

const scenes: Record<string, React.FC> = {
  intro: Intro,
  quickstart: Quickstart,
  instrument: Instrument,
  finding: Finding,
  compare: Compare,
  study: Study,
  share: Share,
  outro: Outro,
};

const Scene: React.FC<{ id: string; length: number; last: boolean }> = ({
  id,
  length,
  last,
}) => {
  const frame = useCurrentFrame();
  const Component = scenes[id];
  const opacity = last
    ? interpolate(frame, [0, 12], [0, 1], clamp)
    : interpolate(frame, [0, 12, length, length + 12], [0, 1, 1, 0], clamp);
  return (
    <AbsoluteFill style={{ opacity, background: C.bg }}>
      <div
        style={{
          position: "absolute",
          inset: 0,
          pointerEvents: "none",
          opacity: 0.12,
          backgroundImage: `linear-gradient(${C.line} 1px, transparent 1px), linear-gradient(90deg, ${C.line} 1px, transparent 1px)`,
          backgroundSize: "80px 80px",
          maskImage: "linear-gradient(to bottom, black, transparent 88%)",
        }}
      />
      <Component />
    </AbsoluteFill>
  );
};

export const AgentLoopDemo: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const [fontHandle] = useState(() => delayRender("Load bundled typography"));
  useEffect(() => {
    Promise.all(
      [400, 500, 600, 700]
        .map((weight) => document.fonts.load(`${weight} 24px Inter`))
        .concat([
          document.fonts.load('400 24px "JetBrains Mono"'),
          document.fonts.load('600 24px "JetBrains Mono"'),
        ]),
    )
      .then(() => continueRender(fontHandle))
      .catch(cancelRender);
  }, [fontHandle]);
  let cursor = 0;
  const timed = storyboard.scenes.map((scene) => {
    const from = cursor;
    cursor += scene.seconds * fps;
    return { ...scene, from, frames: scene.seconds * fps };
  });
  const active =
    timed.find(
      (scene) => frame >= scene.from && frame < scene.from + scene.frames,
    ) ?? timed[timed.length - 1];
  return (
    <AbsoluteFill
      style={{
        background: C.bg,
        color: C.ink,
        fontFamily: "Inter, sans-serif",
      }}
    >
      {timed.map((scene, index) => (
        <Sequence
          key={scene.id}
          from={scene.from}
          durationInFrames={scene.frames + 12}
          name={scene.label}
        >
          <Scene
            id={scene.id}
            length={scene.frames}
            last={index === timed.length - 1}
          />
        </Sequence>
      ))}
      <div
        style={{
          position: "absolute",
          left: 80,
          right: 80,
          top: 43,
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
        }}
      >
        <Logo />
        <div
          style={{
            fontSize: 17,
            letterSpacing: 1.7,
            color: C.muted,
            fontWeight: 500,
          }}
        >
          {active.label}
        </div>
      </div>
      <div
        style={{
          position: "absolute",
          left: 80,
          right: 80,
          top: 113,
          height: 1,
          background: C.line,
        }}
      />
      <div
        style={{
          position: "absolute",
          left: 80,
          right: 80,
          bottom: 86,
          paddingTop: 22,
          borderTop: `1px solid ${C.line}`,
          fontSize: 26,
          color: "#D2DDE5",
          lineHeight: 1.5,
        }}
      >
        {active.caption}
      </div>
      <div
        style={{
          position: "absolute",
          left: 80,
          right: 80,
          bottom: 32,
          display: "flex",
          justifyContent: "space-between",
          fontSize: 16,
          letterSpacing: 1.2,
          color: "#7792A6",
        }}
      >
        <span>AGENTLOOP / SOURCE CHECKOUT</span>
        <span>
          {["finding", "compare", "study", "share"].includes(active.id)
            ? "SYNTHETIC DEMONSTRATION · NOT A BENCHMARK CLAIM"
            : "LOCAL FIRST · FRAMEWORK NEUTRAL"}
        </span>
        <span style={{ fontFamily: mono }}>
          {Math.floor(frame / fps)
            .toString()
            .padStart(2, "0")}{" "}
          / 72
        </span>
      </div>
      <div
        style={{
          position: "absolute",
          bottom: 0,
          left: 0,
          width: `${(frame / (totalFrames - 1)) * 100}%`,
          height: 4,
          background: C.green,
        }}
      />
    </AbsoluteFill>
  );
};
