from __future__ import annotations

from agentloop.client import AgentLoopClient
from agentloop.demo import run_baseline


def main() -> None:
    path = run_baseline()
    client = AgentLoopClient.from_env()
    print(client.health())
    response = client.upload_trace(path)
    print(f"Uploaded {response['run_id']}")
    plan = client.get_optimization_plan(response["run_id"])
    print(f"Optimization cards: {len(plan['optimization_cards'])}")


if __name__ == "__main__":
    main()
