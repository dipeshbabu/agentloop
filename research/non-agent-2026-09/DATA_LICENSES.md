# Dataset attribution and transformations

All three source pages were checked on September 30, 2026 and list Creative
Commons Attribution 4.0 International: https://creativecommons.org/licenses/by/4.0/.
The datasets were downloaded from UCI's linked archives. Exact URLs, fetch times,
byte counts and SHA-256 hashes are in dataset-sources.json and the frozen plan.

- Volker Lohweg (2012), Banknote Authentication, UCI Machine Learning Repository,
  DOI https://doi.org/10.24432/C55P57. Source:
  https://archive.ics.uci.edu/dataset/267/banknote%2Bauthentication.
- Irene Schmidtmann, Gael Hammer, Murat Sariyar and Aslihan Gerhold-Ay (2009),
  Record Linkage Comparison Patterns, UCI Machine Learning Repository,
  DOI https://doi.org/10.24432/C51K6B. Source:
  https://archive.ics.uci.edu/dataset/210/record%2Blinkage%2Bcomparison%2Bpatterns.
- Dry Bean (2020), UCI Machine Learning Repository,
  DOI https://doi.org/10.24432/C50S4B. Associated paper by M. Koklu and Ilker Ali
  Özkan, Multiclass classification of dry beans using computer vision and machine
  learning techniques (2020). Source:
  https://archive.ics.uci.edu/dataset/602/dry%2Bbean%2Bdataset.

Changes: feature-vector/record-component hash partitions; deterministic sampling
and task grouping; training-only imputation/scaling; optional missing indicators;
JSON conversion; derived model parameters and predictions. Raw linkage record
identifiers are omitted from model inputs and replaced by references in exported
tasks. Hash references are not asserted to provide anonymity. No underlying
personal names or dates are present in the model's comparison-feature inputs.

The selected training and task samples are retained under these source licenses.
The original full archives are not duplicated here and remain available at the
recorded public URLs. No NumPy/dependency implementation is redistributed.
AgentLoop and these study application sources are Apache-2.0; the project LICENSE
is preserved with both source snapshots. Dataset authors did not approve a
downstream deployment or this study's optimization choices.
