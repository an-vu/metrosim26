# Simulation regression fixture

`preoptimization_hashes.json` contains exact canonical hashes for the serial
`performance_case()` in `test_performance.py`. The test compares every expected
checkpoint, the version history, and the summary; it does not skip mismatches or
regenerate its expected values during a test run.

The fixture was corrected after the originally bundled hashes failed against both
the initial repository commit and the current engine. The corrected values were
generated independently from initial commit
`d67bd50`, using Python 3.11.17 and NumPy 2.0.2 on macOS arm64.
All checkpoint files, `versions.json`, and `summary.csv` were verified byte-identical
between that initial engine and the reorganized engine. NumPy 1.26.4 produced the
same canonical hashes in this environment.

These are exact numerical regression checks, not a guarantee of identical floating
point output on every platform. If another environment differs, inspect its actual
states and geometry before accepting any fixture change. A failure must never be
fixed by automatically rewriting expected hashes from the implementation under test.

For an intentional model change, retain a reviewed reference run, compare the
behavior and capacity ledgers, and explicitly update the fixture and its provenance.
The serial-versus-spawn tests separately require identical hashes and file bytes
within the same environment.
