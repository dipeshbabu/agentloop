import React from "react";
import { Composition } from "remotion";
import { AgentLoopDemo } from "./Video";
import storyboard from "./storyboard.json";

export const Root: React.FC = () => (
  <Composition
    id="AgentLoopDemo"
    component={AgentLoopDemo}
    width={storyboard.width}
    height={storyboard.height}
    fps={storyboard.fps}
    durationInFrames={storyboard.scenes.reduce(
      (sum, scene) => sum + scene.seconds * storyboard.fps,
      0,
    )}
  />
);
