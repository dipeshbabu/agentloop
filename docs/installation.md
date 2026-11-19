---
search:
  boost: 2
---

# Installation

AgentLoop is published as `agentloop-profiler` on PyPI and on npm:

- the Python package (Python 3.11 or newer) provides the Python SDK and the
  `agentloop` CLI, which analyzes traces, writes reports and runs the guided
  workflow. The Python import and CLI command are `agentloop`;
- the npm package (Node.js 22 or newer) provides the
  [TypeScript SDK](#typescript) for recording traces from Node.js and browser
  code. Analyze its traces with the `agentloop` CLI.

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
python -m pip show agentloop-profiler
agentloop doctor
agentloop quickstart
```

`quickstart` records a synthetic example and writes `runs/agentloop_quickstart.json`.
Continue with [your first useful trace](FIRST_USE.md).

Published versions are listed on [PyPI](https://pypi.org/project/agentloop-profiler/)
and [GitHub Releases](https://github.com/dipeshbabu/agentloop/releases).

## Current source

The guided `improve` commands require 0.2.0 or newer. To try development changes,
install the current source into your environment.

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

The TypeScript SDK is versioned separately from the Python package and supports
Node.js 22 and newer. Install it in your application:

```bash
npm install agentloop-profiler
```

Upgrade an existing install with `npm install agentloop-profiler@latest` and check
the installed version with `npm ls agentloop-profiler`.

It is an ESM package with TypeScript declarations and no runtime dependencies.
Import from `agentloop-profiler` for tracing and `agentloop-profiler/client` for
the HTTP API client. Published versions are listed on
[npm](https://www.npmjs.com/package/agentloop-profiler), with provenance linking
each version to the workflow that built it.

The [TypeScript SDK guide](../sdk/typescript/README.md) covers tracing, model usage,
the API client and building the SDK from source.
