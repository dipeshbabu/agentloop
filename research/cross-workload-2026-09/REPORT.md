# Cross-workload usefulness benchmark

Results are separated by workload, evidence kind and candidate. There is no pooled usefulness score.

Protocol: 8b5ec58ad690cd8ecc046c234e858d4274e45d97feadfd54f625d7a543d888a7
Analyzer: 0.7.0 / 8af65da781029a581c6cf01cf8700cdea99da12c

## Intervention outcomes

Runtime/token/cost changes are candidate minus baseline; negative is a reduction. Quality gates and regressions use all planned pairs, including failures.

| Kind | Workload | Phase / variant | Recorded / planned | Candidate quality gate | Quality regressions | Failures / missing quality | Runtime change: mean [95% task interval], n | Unknown cost pairs |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| real_archived | math | evaluation / smaller_model | 12/12 | 4/12 | 2 | 0/0 | -1873.964 ms &#91;-2019.166, -1741.099&#93;, n=6 | 12 |
| real_archived | math | pilot / smaller_model | 2/2 | 1/2 | 1 | 0/0 | -1723.476 ms &#91;-1725.269, -1721.683&#93;, n=2 | 2 |
| real_archived | repository | evaluation / smaller_model | 12/12 | 0/12 | 0 | 0/0 | -5067.060 ms &#91;-5847.833, -4479.028&#93;, n=6 | 12 |
| real_archived | repository | pilot / smaller_model | 2/2 | 0/2 | 0 | 0/0 | -4825.012 ms &#91;-4954.151, -4695.872&#93;, n=2 | 2 |
| real_archived | sql | evaluation / smaller_model | 12/12 | 8/12 | 4 | 0/0 | -2416.583 ms &#91;-3010.420, -1978.766&#93;, n=6 | 12 |
| real_archived | sql | pilot / smaller_model | 2/2 | 2/2 | 0 | 0/0 | -2146.303 ms &#91;-2262.762, -2029.845&#93;, n=2 | 2 |
| synthetic_reference | batch | evaluation / cheap | 16/16 | 0/16 | 16 | 0/0 | -0.423 ms &#91;-0.475, -0.373&#93;, n=8 | 0 |
| synthetic_reference | batch | evaluation / failing | 16/16 | 14/16 | 2 | 2/0 | 0.075 ms &#91;-0.023, 0.236&#93;, n=8 | 2 |
| synthetic_reference | batch | evaluation / optimized | 16/16 | 16/16 | 0 | 0/0 | -0.607 ms &#91;-0.651, -0.564&#93;, n=8 | 0 |
| synthetic_reference | email | development / cheap | 12/12 | 4/12 | 8 | 0/0 | -0.265 ms &#91;-0.407, -0.151&#93;, n=6 | 0 |
| synthetic_reference | email | development / failing | 12/12 | 10/12 | 0 | 2/2 | -0.231 ms &#91;-0.328, -0.146&#93;, n=6 | 2 |
| synthetic_reference | email | development / hybrid | 12/12 | 12/12 | 0 | 0/0 | -0.266 ms &#91;-0.319, -0.230&#93;, n=6 | 0 |
| synthetic_reference | incident | evaluation / cheap | 14/14 | 4/14 | 10 | 0/0 | -0.289 ms &#91;-0.462, -0.191&#93;, n=7 | 0 |
| synthetic_reference | incident | evaluation / failing | 14/14 | 12/14 | 0 | 2/2 | -0.194 ms &#91;-0.375, -0.084&#93;, n=7 | 2 |
| synthetic_reference | incident | evaluation / hybrid | 14/14 | 14/14 | 0 | 0/0 | -0.272 ms &#91;-0.461, -0.153&#93;, n=7 | 0 |
| synthetic_reference | marketplace | evaluation / cheap | 16/16 | 4/16 | 12 | 0/0 | -0.206 ms &#91;-0.244, -0.171&#93;, n=8 | 0 |
| synthetic_reference | marketplace | evaluation / failing | 16/16 | 14/16 | 0 | 2/2 | -0.094 ms &#91;-0.166, -0.008&#93;, n=8 | 2 |
| synthetic_reference | marketplace | evaluation / hybrid | 16/16 | 16/16 | 0 | 0/0 | -0.184 ms &#91;-0.226, -0.141&#93;, n=8 | 0 |
| synthetic_reference | payment | development / cheap | 16/16 | 4/16 | 12 | 0/0 | -0.061 ms &#91;-0.082, -0.041&#93;, n=8 | 0 |
| synthetic_reference | payment | development / failing | 16/16 | 14/16 | 0 | 2/2 | 0.010 ms &#91;-0.072, 0.106&#93;, n=8 | 2 |
| synthetic_reference | payment | development / hybrid | 16/16 | 16/16 | 0 | 0/0 | -0.074 ms &#91;-0.081, -0.066&#93;, n=8 | 0 |

## Finding labels

Labels score the first repetition of each task only. Opportunity labels test exact family/span scope; actual candidate quality is reported above.

| Kind | Workload | Rule | Cases | TP | FP | Ambiguous / unknown cases | Abstained cases |
| --- | --- | --- | --- | --- | --- | --- | --- |
| synthetic_reference | batch | batch_model_calls | 8 | 0 | 0 | 8 | 0 |
| synthetic_reference | batch | route_to_smaller_model | 8 | 0 | 0 | 8 | 0 |
| synthetic_reference | email | batch_model_calls | 6 | 0 | 0 | 0 | 6 |
| synthetic_reference | email | context_relevance | 6 | 6 | 0 | 0 | 0 |
| synthetic_reference | email | route_to_smaller_model | 6 | 0 | 0 | 6 | 0 |
| synthetic_reference | email | semantic_redundancy | 6 | 5 | 0 | 0 | 1 |
| synthetic_reference | incident | batch_model_calls | 7 | 0 | 0 | 0 | 7 |
| synthetic_reference | incident | context_relevance | 7 | 7 | 0 | 0 | 0 |
| synthetic_reference | incident | route_to_smaller_model | 7 | 0 | 0 | 7 | 0 |
| synthetic_reference | incident | semantic_redundancy | 7 | 5 | 0 | 0 | 2 |
| synthetic_reference | marketplace | batch_model_calls | 8 | 0 | 0 | 0 | 8 |
| synthetic_reference | marketplace | route_to_smaller_model | 8 | 0 | 0 | 8 | 0 |
| synthetic_reference | marketplace | semantic_redundancy | 8 | 7 | 0 | 0 | 1 |
| synthetic_reference | payment | batch_model_calls | 8 | 0 | 0 | 0 | 8 |
| synthetic_reference | payment | route_to_smaller_model | 8 | 0 | 0 | 8 | 0 |
| real_archived | real:math | batch_model_calls | 6 | 0 | 0 | 0 | 6 |
| real_archived | real:math | route_to_smaller_model | 6 | 0 | 0 | 6 | 0 |
| real_archived | real:repository | batch_model_calls | 6 | 0 | 6 | 0 | 0 |
| real_archived | real:repository | route_to_smaller_model | 6 | 0 | 0 | 6 | 0 |
| real_archived | real:sql | batch_model_calls | 6 | 0 | 0 | 0 | 6 |
| real_archived | real:sql | route_to_smaller_model | 6 | 0 | 0 | 6 | 0 |

## Recording and analysis overhead

| Kind | Workload | Paired recording controls | Mean recording delta (ms) | Mean current analysis (ms) |
| --- | --- | --- | --- | --- |
| synthetic_reference | batch | 16 | 0.839 | 4.175 |
| synthetic_reference | email | 12 | 0.307 | 4.181 |
| synthetic_reference | incident | 14 | 0.320 | 3.626 |
| synthetic_reference | marketplace | 16 | 0.270 | 2.576 |
| synthetic_reference | payment | 16 | 0.178 | 1.608 |
| real_archived | repository | 2 | 262.826 | 3.465 |
| real_archived | sql | 2 | 231.683 | 1.521 |
| real_archived | math | 2 | 109.069 | 1.471 |

Reference controls isolate part of span/payload recording; they retain root lifecycle, hashing and fixture tokenization. Historical real wall differences include model variation. Negative deltas are retained. Source-specific scopes and raw observations are in JSON.

## Historical predictions and incomplete cases

The retired onboarding protocol retains 6 baseline attempts and 6 unexecuted candidate slots. They are separate from the revised pilot and evaluation cohorts.
The original calibration retains 184 finding registrations across 9 cohorts. Its original estimator versions, selected/rejected registrations, quality-rejected outcomes and task-cluster intervals are preserved in the result bundle. No new diagnosis is assigned those historical effects, and no runtime coefficient is changed.

Attributable original latency diagnostics (milliseconds; quality acceptance remains separate):

| Original estimator / version | Split | Independent tasks | Predicted saving | Realized saving | Signed error [95% task interval] | Accepted quality / selected |
| --- | --- | --- | --- | --- | --- | --- |
| route_to_smaller_model / 1.0 | fit | 2 | 289.920 | 381.827 | -91.907 &#91;-258.144, 74.329&#93; | 0/4 |
| route_to_smaller_model / 1.0 | held_out | 6 | 301.081 | 253.683 | 47.398 &#91;-294.242, 414.101&#93; | 0/12 |

## Limits and effort

- No new provider inference. Reference billing is synthetic fixture accounting; real self-hosted operating cost stays unknown. Do not credit new diagnoses with historical candidate outcomes or fit combined-intervention effects per finding.
- Whole reference workloads are assigned once to development/evaluation; no fitting or tuning is done. These public maintained fixtures and retrospective archives are not blind unseen data.
- First repetition only per distinct task; all repetitions remain in emission, quality and resource inventories.
- Finding opportunity labels describe justified scope, not a guaranteed successful intervention. Unknown/ambiguous labels are unscored.
- Small fixed/public task sets and fixture correlations do not support a universal precision or performance claim.
- Human effort was not timed; model weights/runtime binaries are not redistributed. Original archive failures and unmatched onboarding slots remain available.
- Three explicit orchestration steps are recorded; operator minutes and subjective review effort were not timed.
