# TypeScript SDK: first npm release proposal

Proposed package: `agentloop-profiler@0.1.0`. This proposal does not publish it.
The maintainer approved the first publication on 2026-11-14; see
[Releasing](RELEASING.md#typescript-sdk-on-npm) for the npm release flow.
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
- Browser package validation covers esbuild 0.28.2 and Playwright Chromium using
  an installed tarball in an owned offline application. Firefox, Safari, other
  bundlers and cross-origin API deployments remain unvalidated.
- No CommonJS entry point, automatic provider instrumentation or guided CLI
  runner is promised by this release.

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
npx --prefix sdk/typescript playwright install chromium
npm run test:browser --prefix sdk/typescript
uv run --frozen --extra server python scripts/smoke_typescript_sdk.py
```

In `sdk/typescript`, run `npm pack --pack-destination /absolute/package-output`.
In a fresh external application directory, install that tarball with
`npm install --ignore-scripts --no-audit --no-fund /absolute/package-output/agentloop-profiler-0.1.0.tgz`.
Repeat `smoke_typescript_sdk.py --module /absolute/application/node_modules/agentloop-profiler/dist/index.js --out /fresh/evidence`.
The CI job already performs both source and installed-artifact interoperability
against the authenticated local Python API on Node 22 and 24. It checks native
schema, parent relationships, usage, metrics and HTML export.

The browser lane separately checks public package resolution, concurrent parent
relationships, thrown business errors, unavailable usage, same-origin fetch,
trace uploads, cancellation and rejected redirects. Python validates the emitted
native trace. Browser traces and model-free fixture data carry synthetic
provenance. These checks establish package behavior on the selected targets.

Local Node 22 validation passes 25 regression tests, declaration checks and
dependency audit (zero known advisories at review time). Final-head CI, installed
artifact evidence and full repository checks are recorded in the PR. A static
security review of all 15 original SDK files found no reportable vulnerability;
that result is scoped evidence, not a guarantee about every runtime or caller.

Browser validation exposed a receiver error in the API client. The constructor
now binds its fetch implementation to `globalThis`, preserving native browser
Web API invocation. A unit regression and the installed browser application
exercise this path. Credentials, API routes and trace contracts retain their
existing behavior.

## Approval still needed

Confirm registry ownership/name availability, initial public API expectations,
release provenance and the exact reviewed tarball before publication. Keep
browser support and adoption claims limited to demonstrated evidence. No npm
publication or external-user adoption is claimed here.
