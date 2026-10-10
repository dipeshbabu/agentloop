# Offline OTLP JSONL import

This unreleased checkout reads bounded OTLP JSONL exports and creates qualified
native traces, immutable source receipts and a record inventory. It accepts
multiple traces on one line and joins a trace split over several lines using
the existing `agentloop.otel.traces_from_otel` parser.

```bash
agentloop telemetry import-jsonl harbor-traces.jsonl --source harbor --out runs/harbor-otel
agentloop telemetry import-jsonl trace.json --single-json --out runs/otel-json
uv run --frozen python examples/otlp_jsonl.py --out runs/otlp-example
```

No Harbor, Omnigent, OpenTelemetry SDK, collector or provider runs during import.
The older `agentloop import-otel` command keeps its single-document behavior.
The example uses owned synthetic data, including frozen outputs from an actual
pinned Harbor converter. It establishes no live interoperability or measured
optimization benefit.

## Supported records

Records contain `resourceSpans` and/or `resourceLogs` in OTLP JSON format. The
existing parser's flat `spans` array and attribute-object convenience form are
also supported. Trace/span identifiers must be nonzero hexadecimal IDs; camel
case and the existing snake case aliases are supported. Contradictory aliases,
duplicate attribute keys, ambiguous AnyValues and invalid structural types fail
with payload-free diagnostics.

Missing IDs use explicitly calculated, source-record-scoped identities. They
do not merge across records, deduplicate by appearance or establish a measured
session runtime. Actual trace IDs remain in receipts/event metadata; native run
and event IDs are namespaced by source and supplied job/trial/task context.
Missing session identifiers and ambiguous sessions stay explicit. Caller native
IDs and parent assertions do not override the source span identities.

The importer keeps raw span/log counts separate from deduplicated span counts.
Only an identical span ID within an identified trace, with identical canonical
source span/resource/scope content, is deduplicated. Raw payload fingerprints
are compared before minimization: different omitted text still conflicts.
Same-looking client/server spans with different IDs remain separate observations.

A conflicting span segment retains the first projection and emits a source/line
diagnostic. It never overwrites on last write. Precise trace latency and complete
usage become unavailable for the conflicting group. Parent edges resolve only
within the same source trace, including parents arriving later. Unresolved or
cyclic parentage remains recorded; cycles do not become native graph edges.
Span links stay metadata relationships, without fabricated parent edges.

Late external evaluation logs remain in receipts even when attached to a
duplicate span segment. Their scorer names, dimensions and scales are supplied
by the source; they do not establish an independently verified task pass. A
log-only or empty record does not manufacture a successful execution trace.

## Measurements and provenance

Reports use existing external qualifications. Missing bounds, source status,
usage or cost remain null/unknown. A span without operation semantics stays an
unknown source span; it does not manufacture model inference from a name.
Model-block usage is separate from nonmodel root aggregates. Cached input is
a prompt subset. Recorded aggregate call multiplicity remains separate from
native model-block count and never creates independently timed calls.

Source token provenance is retained in `source_token_provenance`; imported
counts remain externally reported rather than being promoted into exact provider
measurement. Recorded source model cost remains event/receipt metadata. The
batch importer does not apply a price table. Exported spans alone do not prove
execution completeness, so replay/study/value improvement claims require the
separate [trial/verifier path](HARBOR_TRIALS.md).

### Harbor converter boundary

Five checked-in JSONL files were produced by the actual reviewed converter
modules at Harbor
[`d5ac1be17f575852eaf4fffc4072fd18481c209b`](https://github.com/harbor-framework/harbor/blob/d5ac1be17f575852eaf4fffc4072fd18481c209b/packages/harbor-atif2otel/src/harbor_atif2otel/convert.py),
using owned ATIF v1.7/v1.8, deterministic, aggregated and embedded-subagent
inputs. [Converter provenance](../tests/fixtures/external/otlp/converter_provenance.json)
pins input/output bytes, source file hashes, Python, protobuf and
`opentelemetry-proto==1.42.1`. The upstream package metadata says 0.1.1; emitted
SDK/scope version is 0.1.0. These are distinct recorded facts.

The pinned converter derives model span endings from neighboring step timestamps
and assigns synthetic tool offsets/durations. It emits OK statuses independently
of a verifier. `harbor-atif2otel` resource/scope identity therefore triggers
`timing_provenance: inferred`, preserves its source intervals, and withholds
measured duration/runtime and successful source status. These output values
must not be presented as measured zero-latency operations or verified success.

Actual v1.8 image/audio references are retained by the converter in text
attributes without binary reads. The default batch import minimizes that text.
Direct [ATIF import](HARBOR_ATIF.md) retains additional interaction/history,
copied-context, source timestamp, media and document relationship semantics;
OTLP receipts identify this conversion loss. Converter-generated traces are not
proof of either upstream runtime execution or task correctness.

For an existing Harbor export, supply `--source harbor`; do not install or run
Harbor merely to analyze a saved file. This importer does not modify upstream
export filtering, scheduling, locks or benchmark rewards.

## Bounds, invalid records and output

The [shared limits](INTEROPERABILITY.md) apply to JSON bytes/depth/nodes, total
bytes, physical lines, source telemetry items, trace groups, per-trace events,
links and source metadata. Reading uses bounded `readline`; an oversized line
is drained in bounded chunks. It remains one invalid record and all bytes still
consume the total budget. Blank lines also consume physical record/byte budgets.
Overall byte/record/span/trace/reference and merged event limits are terminal.

By default, independently malformed UTF-8/JSON/OTLP records and record-local
size/structure errors are retained while later valid records import. `--strict`
stops on those errors. Output reports valid/invalid records, blanks, raw spans,
raw logs, identical duplicates, conflicting segments and source-qualified notices.
Single-document mode uses the bounded existing JSON artifact loader.

Hashes identify exact supplied file bytes and canonical source-span content,
including content omitted from reports. They provide reproducibility against
the supplied export, not source authenticity. Relative input paths stay under
an explicit root; symlinks/junctions below it, remote files, archives and media
reads are unsupported. Snapshot concurrently written artifacts before import.

Native files/receipts/inventory have stable bytes on repeated import. Conflicting
output or trace mutation after receipt capture fails instead of overwriting.
Native trace schema 1.1 and dependency/package versions remain unchanged.
Older readers that ignore qualification metadata must not analyze these files.

## Privacy

Input/output, unknown string attributes, log bodies, exception messages and
reasoning are minimized before native parsing. Numeric/boolean and known
structural attributes retain their semantics. Known credential fields, reasoning
and token-ID fields stay omitted even with the Python-only `capture_content=True`
option. Captured text needs review before sharing. Structural names/identifiers
and attribute keys can still be private.

Unknown content is represented by a size/hash/omission marker. No payloads or
decoder exception text enter diagnostics. Source resources, scopes, events,
links, GenAI/OpenInference/MCP fields and external evaluations retain their
supported noncontent semantics through the existing OTel parser.

## Python and verification

```python
from agentloop.interoperability.otlp_jsonl import OtlpOptions, import_otlp

result = import_otlp("harbor-traces.jsonl", options=OtlpOptions(
    system="harbor", identity={"job_id": "recorded-job-id"},
))
result.write("runs/imported")
print(result.inventory())
```

```bash
uv run --frozen python -m pytest tests/test_otlp_jsonl.py tests/test_otel_interop.py tests/test_harbor_atif.py tests/test_harbor_trials.py tests/test_interoperability_contracts.py -q -ra
```

Ordinary tests read frozen JSON only. Optional regeneration uses Python 3.12+
with the explicitly selected trusted converter source and recorded provenance:

```bash
python tests/fixtures/external/build_converter_fixtures.py --converter-source /path/to/reviewed/source --source-provenance /path/to/source-provenance.json
```

That command deliberately executes the selected upstream conversion modules.
It is separate from ordinary package import, CLI use and tests, and requires no
Harbor runtime, uploader, task verifier or vendor credentials.
