# TypeScript SDK: first npm release proposal

Proposed package: `agentloop-profiler@0.1.0`. This proposal does not publish it.
The Python 0.2.0 preparation proceeds independently. npm publication requires
the maintainer's explicit approval and a working package-owner/publisher setup.
No login, publishing workflow or registry write is needed for this review.

## Package and compatibility

- ESM entry points: root tracing/client exports and `/client`; declarations ship
  alongside JavaScript. Node.js floor: 22. CI runs 22 and 24.
- Native trace schema 1.1 and HTTP API v1. The SDK has no runtime dependencies.
  TypeScript is a pinned development dependency.
- The API client preserves project/admin credential scopes, refuses redirects,
  bounds response bytes and supports timeout and caller cancellation.
- Tracing uses explicit contexts for concurrent work; input, output and error
  retention are opt-in. Missing usage remains unavailable.
- No CommonJS entry point, automatic provider instrumentation, guided CLI runner
  or validated browser/bundler support is promised by this release.

The readiness review identified two JavaScript boundary errors: `runSpan` could
accept an unsupported event kind despite its TypeScript union, and identifiers
equal to `.` or `..` could be normalized by URL handling. Both now fail before
executing the callback or making a request, with regression coverage. Valid
existing typed calls and trace/API schemas are unchanged.

Package metadata names Dipesh Tharu Mahato as author and supplies source,
homepage, issue tracker, license and explicit public access. Inspect `npm pack`
contents and the installed artifact before approving its immutable publication.

## Reproduce validation

From the checkout:

```bash
npm ci --prefix sdk/typescript
npm run build --prefix sdk/typescript
npm test --prefix sdk/typescript
npm run test:types --prefix sdk/typescript
npm audit --prefix sdk/typescript
uv run --frozen --extra server python scripts/smoke_typescript_sdk.py
```

In `sdk/typescript`, run `npm pack --pack-destination /absolute/package-output`.
In a fresh external application directory, install that tarball with
`npm install --ignore-scripts --no-audit --no-fund /absolute/package-output/agentloop-profiler-0.1.0.tgz`.
Repeat `smoke_typescript_sdk.py --module /absolute/application/node_modules/agentloop-profiler/dist/index.js --out /fresh/evidence`.
The CI job already performs both source and installed-artifact interoperability
against the authenticated local Python API on Node 22 and 24. It checks native
schema, parent relationships, usage, metrics and HTML export.

Local Node 22 validation passes 24 regression tests, declaration checks and
dependency audit (zero known advisories at review time). Final-head CI, installed
artifact evidence and full repository checks are recorded in the PR. A static
security review of all 15 original SDK files found no reportable vulnerability;
that result is scoped evidence, not a guarantee about every runtime or caller.

## Approval still needed

Confirm registry ownership/name availability, initial public API expectations,
release provenance and the exact reviewed tarball before publication. Keep
browser support and adoption claims limited to demonstrated evidence. No npm
publication or external-user adoption is claimed here.
