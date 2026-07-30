# Known Pre-Commit Fixes

Quick-lookup table for recurring pre-commit hook failures. `/fix-pre-commit` reads this
before investigating anything, applies a matching row as a one-shot, and bumps **Hits**
and **Last used**.

**This file is per-project and starts empty on purpose.** It is deliberately *not* in
`sync-harness.py`'s `MANIFEST`: the rows are this repo's accumulated experience, and the
hit counts are what `scripts/hooks/normalize-known-fixes.py` prunes against. Vendoring it
byte-identical would reset every project's learning on each `--pull` and hand every repo
another repo's error patterns.

Add a row only for a pattern likely to recur. Rows with **Hits = 0** older than 90 days
from **Added** are pruned automatically.

| Error pattern (substring) | Root cause | Fix | Hits | Last used | Added |
|---|---|---|---|---|---|
