"""The engine can be vendored byte for byte: it is self-contained between its markers and names every host dependency."""
from __future__ import annotations

import ast
import builtins
import unittest
from pathlib import Path

from trimreaper import cli, core, host

STDLIB = {"errno", "fcntl", "hashlib", "json", "os", "plistlib", "pwd", "re", "shutil", "sqlite3", "stat", "subprocess", "tarfile",
          "tempfile", "threading", "time", "unicodedata", "uuid", "ExitStack", "contextmanager", "Path"}


def free_names(tree: ast.Module) -> set:
    defined, local = set(), set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Assign):
            defined.update(t.id for t in node.targets if isinstance(t, ast.Name))
    loaded = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            (loaded if isinstance(node.ctx, ast.Load) else local).add(node.id)
        elif isinstance(node, ast.arg):
            local.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            local.add(node.name)
        elif isinstance(node, ast.FunctionDef):
            local.add(node.name)
    return loaded - defined - local - set(dir(builtins))


class Embedding(unittest.TestCase):
    def setUp(self):
        self.text = Path(core.__file__).read_text(encoding="utf-8")
        self.body = cli.core_body()
        self.tree = ast.parse(self.body)

    def test_markers_appear_once(self):
        self.assertEqual(self.text.count(cli.BEGIN), 1)
        self.assertEqual(self.text.count(cli.END), 1)

    def test_the_body_imports_nothing(self):
        imports = [n for n in ast.walk(self.tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
        self.assertEqual(imports, [], "the host provides every module and name; the body must not import")

    def test_every_free_name_is_stdlib_or_a_declared_host_name(self):
        missing = free_names(self.tree) - STDLIB - set(core.HOST_NAMES)
        self.assertEqual(missing, set())

    def test_every_declared_host_name_is_used_and_provided_by_the_standalone_host(self):
        used = free_names(self.tree)
        self.assertEqual(set(core.HOST_NAMES) - used, set(), "HOST_NAMES lists names the engine does not read")
        self.assertEqual([n for n in core.HOST_NAMES if not hasattr(host, n)], [])

    def test_the_body_carries_no_private_paths_or_names(self):
        for needle in ("/Users/", "ut-cleanup-guard", "Mike", "LDS", "lds-", "build_queue.py", "UltraTerm", "~/dev"):
            self.assertNotIn(needle, self.body, needle)

    def test_the_digest_is_of_the_body_only(self):
        import hashlib
        self.assertEqual(cli.core_digest(), hashlib.sha256(self.body.encode()).hexdigest())
