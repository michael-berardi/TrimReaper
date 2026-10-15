# Changelog

## 0.1.0 (2026-10-15)

First public release: UltraTerm's reaper engine as a standalone package.

- The engine (`src/trimreaper/core.py`), identical to what UltraTerm runs, plus a standalone host: a JSON config file,
  a private SQLite job store and the `trimreaper` command line.
- Removes build output of finished jobs, expired clones (archiving unsaved work first), idle agent scratch, expired
  evidence files and expired archives, each through one guarded deletion path with durable receipts.
- A configured reap home is now refused as a root, like the real home and system folders already were.
