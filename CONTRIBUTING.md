# Contributing

## Tests

```sh
PYTHONPATH=src:tests python3 -m unittest discover -s tests
```

The tests build real git repositories, remotes and processes in a temporary folder. Nothing outside it is read or
written. They need `git`, `lsof` and `ps`.

## Changing the engine

The engine between the markers in `src/trimreaper/core.py` is vendored, byte for byte, into UltraTerm. A change
there:

1. keeps every gate in [docs/SAFETY.md](docs/SAFETY.md) fail-closed: an unknown, an error or a timeout keeps the folder;
2. comes with a test in `tests/` that fails without it;
3. updates the docs it touches;
4. ships in a tagged release. UltraTerm then re-vendors that tag and runs its own reaper tests against it.

New host dependencies go in `core.HOST_NAMES` and `docs/EMBEDDING.md`; `tests/test_embedding.py` enforces both.

## Style

Standard library only. Every refusal names its cause. No silent fallbacks.
