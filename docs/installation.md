---
search:
  boost: 2
---

# Installation

AgentLoop requires Python 3.11 or newer. The package is `agentloop-profiler` and
the Python import and CLI command are `agentloop`.

## Published package

Create an environment for your project and install the package.

=== "Linux or macOS"

    ```bash
    python -m venv .venv
    source .venv/bin/activate
    python -m pip install --upgrade agentloop-profiler
    ```

=== "Windows PowerShell"

    ```powershell
    python -m venv .venv
    .\.venv\Scripts\Activate.ps1
    python -m pip install --upgrade agentloop-profiler
    ```

Check the installed version and create a local example trace.

```bash
agentloop --version
agentloop doctor
agentloop quickstart
```

`quickstart` records a synthetic example and writes `runs/agentloop_quickstart.json`.
Continue with [your first useful trace](FIRST_USE.md).

Published versions are listed on [PyPI](https://pypi.org/project/agentloop-profiler/)
and [GitHub Releases](https://github.com/dipeshbabu/agentloop/releases).

## Current source

The guided `improve` commands require 0.2.0 or newer. If your package index
supplies 0.1.1, install the current source into your environment.

```bash
python -m pip install "git+https://github.com/dipeshbabu/agentloop.git"
agentloop improve --help
```

For a development checkout with examples and validation scripts, use Git and uv.

```bash
git clone https://github.com/dipeshbabu/agentloop.git
cd agentloop
uv sync --locked --dev
uv run --frozen agentloop improve --help
```

The [guided optimization guide](GUIDED_OPTIMIZATION.md) walks through a complete
offline example and connecting your own Python agent.

## Optional components

Install the extras your application uses.

| Extra | Included component |
| --- | --- |
| `dashboard` | Streamlit dashboard |
| `server` | HTTP API server |
| `postgres` | PostgreSQL storage driver |
| `instrumentation` | OpenAI SDK instrumentation |
| `all` | All application extras |

```bash
python -m pip install --upgrade "agentloop-profiler[dashboard,server]"
```

Use the [dashboard guide](DASHBOARD.md) or [server deployment guide](PRODUCTION.md)
to configure these components. [Upgrade notes](UPGRADING.md) cover stored data,
client compatibility and optional dependency updates.

## TypeScript

The separately versioned TypeScript SDK supports Node.js 22 and newer. Follow
its [source installation instructions](../sdk/typescript/README.md).
Its initial npm publication is pending.
