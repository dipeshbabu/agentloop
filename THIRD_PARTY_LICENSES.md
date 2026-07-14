# Third-party software

AgentLoop is licensed under Apache-2.0, but its dependencies remain under their
own licenses. Installing optional extras or using the container image does not
relicense those components under the AgentLoop license.

The source archive and wheel do not vendor dependency source code. Python
installers resolve those packages separately from the constraints in
`pyproject.toml` and the exact resolution in `uv.lock`. The container image does
include installed Python dependencies and operating-system components, so anyone
redistributing that image must review and satisfy the licenses shipped in the
final image.

## Direct runtime dependencies

This table records the licenses reported by the direct dependencies in the
locked environment reviewed on 2026-07-13. It is a review aid, not a replacement
for the complete license text and notices distributed by each dependency.

| Dependency | Used by | Reported license |
| --- | --- | --- |
| [Typer](https://github.com/fastapi/typer) | Core CLI | MIT |
| [Rich](https://github.com/Textualize/rich) | Core terminal output | MIT |
| [Streamlit](https://streamlit.io/) | `dashboard` extra | Apache-2.0 |
| [pandas](https://pandas.pydata.org/) | `dashboard` extra | BSD |
| [FastAPI](https://github.com/fastapi/fastapi) | `server` extra | MIT |
| [Uvicorn](https://www.uvicorn.org/) | `server` extra | BSD-3-Clause |
| [psycopg](https://www.psycopg.org/) and `psycopg-binary` | `postgres` extra | LGPL-3.0-only |
| [OpenAI Python](https://github.com/openai/openai-python) | `instrumentation` extra | Apache-2.0 |

The locked transitive graph also contains packages under permissive licenses and
components under MPL-2.0, including Certifi and tqdm. Consult `uv.lock` and the
installed distributions for the complete version-specific dependency graph,
license texts, copyright notices, and attribution requirements.

## Reviewing a dependency change

Use a clean, exact runtime environment so previously installed development tools
do not contaminate the report:

```bash
uv sync --locked --all-extras --no-dev
uv run --no-dev --with pip-licenses pip-licenses --format=markdown --with-urls
```

Review both direct and transitive changes. In particular, inspect copyleft or
source-available terms, bundled native binaries, required notices, patent terms,
and whether the package will be redistributed in the container. Restore the
development environment afterward with `uv sync --locked --all-extras --dev`.

If a dependency's metadata and upstream license disagree, treat the upstream
license files for the locked version as authoritative and resolve the ambiguity
before release. Licensing questions with material distribution implications
should be reviewed by qualified counsel.
