# Security

TrimReaper deletes files, so a flaw can lose data. Please report anything that could make it remove, read or expose
something it should not: a path that escapes its roots, a protected file that is entered or deleted, a race that
defeats a gate, a receipt or archive that leaks secrets.

Report privately through GitHub: **Security → Report a vulnerability** on this repository. Please do not open a public
issue for a vulnerability. Include the version (`trimreaper --version`), macOS version, and the smallest folder layout
that reproduces it.

Supported: the latest release.
