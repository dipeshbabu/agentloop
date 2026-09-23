"""Text projection of saved judgment evidence; never executes a backend."""

from agentloop.markdown import markdown_table_cell


def judgment_markdown(evidence):
    lines = [
        "",
        "## Offline semantic judgments",
        "",
        "Recorded execution facts, deterministic rule inferences, and semantic judge answers "
        "are separate evidence categories. Judge answers do not establish task quality or "
        "calibrated confidence. Judge latency and cost are analysis overhead, separate from workflow totals.",
        "",
        f"Evidence status: {markdown_table_cell(evidence['status'])}",
        "",
        "| Judge / version | Kind | Status | Value | Confidence basis | Cache hit | Incurred cost USD |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for record in evidence["records"]:
        judge, result = record["judge"], record["evaluation"]
        cells = [
            f"{judge['implementation']} / {judge['version']}",
            record["request"]["spec"]["kind"],
            record["effective_status"],
            record["effective_value"],
            result["uncertainty"]["calibration_status"],
            record["invocation"]["cache_hit"],
            record["invocation"]["usage"]["cost_usd"],
        ]
        lines.append(
            "| "
            + " | ".join(
                markdown_table_cell("unavailable" if item is None else str(item)) for item in cells
            )
            + " |"
        )
    return lines
