"""Compare disabled, shadow, and enforced controls without external services."""

from __future__ import annotations

from agentloop.harness import (
    Decision,
    Harness,
    HarnessConfig,
    HarnessDeniedError,
    Hook,
    Policy,
)


def main() -> None:
    for mode in ("disabled", "shadow", "enforce"):
        dispatched = []
        guard = Policy(
            policy_id="reviewed-tool-denial",
            version="1",
            evaluate=lambda context: Decision("deny", "requires_review"),
            hooks={Hook("tool")},
            actions={"deny"},
        )
        run = Harness(HarnessConfig(mode=mode, policies=(guard,))).start_run(f"example-{mode}")

        def tool() -> str:
            dispatched.append(True)
            return "synthetic result"

        protected = run.wrap(tool, boundary="tool")
        try:
            result = protected()
        except HarnessDeniedError:
            result = "denied before dispatch"
        print(f"{mode}: {result}; dispatched={len(dispatched)}; hook_records={len(run.results)}")
        assert len(dispatched) == (0 if mode == "enforce" else 1)


if __name__ == "__main__":
    main()
