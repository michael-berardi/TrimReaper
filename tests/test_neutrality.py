"""TrimReaper ships no built-in model, provider, fallback or personal-name choice.

Those choices belong in the operator's own configuration. This test keeps the shipped
source, documentation and README free of them. Exceptions are limited to provider
adapter modules that name a provider's own catalog and to legal attribution files.
"""

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Concrete model ids that only an operator should choose. Adapter modules for a provider
# may name that provider's own catalog, but must not set defaults.
MODEL_CHOICE = re.compile(
    r"\b(?:gpt-[56][a-z0-9.-]*|deepseek[a-z0-9.-]*|opencode-go/|openrouter/|:exacto|"
    r"mimo-v[0-9][a-z0-9.-]*|glm-[0-9][a-z0-9.-]*|claude-[a-z0-9.-]*-5-5|"
    r"(?:haiku|sonnet|opus)[ -]?5[.-]5)",
    re.IGNORECASE,
)
# Names of people and organisations the tool was first built for: never in the shipped tool.
# Case-insensitive: a lowercase handle (e.g. a GitHub owner in a URL) must not
# slip past a capitalized pattern name.
PERSONAL = re.compile(
    r"\b(?:Mike|Berardi|Dana|Felix|acme|acme|Mouseia|Liberty Design|"
    r"libertydesign|acme)\b|/Users/[a-z]|overseer(?!-reap-archive\")",
    re.IGNORECASE,
)

# Documented exceptions: legal attribution and package metadata are not shipped behaviour.
EXCLUDED_NAMES = {"LICENSE", "pyproject.toml", "test_neutrality.py"}
# Provider-adapter modules may name their own provider's catalog but may not set defaults.
ADAPTER_DIRS = {os.path.join("src", "trimreaper", "adapters")}


def _files(directory):
    results = []
    for root, _dirs, filenames in os.walk(directory):
        for name in filenames:
            if name.endswith((".py", ".md", ".sh", ".toml", ".txt", ".json", ".yaml", ".yml")):
                results.append(os.path.join(root, name))
    return results


def _shipped_files():
    paths = []
    src = os.path.join(ROOT, "src")
    if os.path.isdir(src):
        paths.extend(_files(src))
    docs = os.path.join(ROOT, "docs")
    if os.path.isdir(docs):
        paths.extend(_files(docs))
    readme = os.path.join(ROOT, "README.md")
    if os.path.isfile(readme):
        paths.append(readme)
    return [
        path
        for path in paths
        if os.path.basename(path) not in EXCLUDED_NAMES
        and not any(part in ADAPTER_DIRS for part in os.path.relpath(path, ROOT).split(os.sep))
    ]


def _scan(pattern):
    hits = []
    for filepath in _shipped_files():
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as handle:
                for lineno, line in enumerate(handle, 1):
                    if pattern.search(line):
                        rel = os.path.relpath(filepath, ROOT)
                        hits.append(f"{rel}:{lineno}: {line.strip()[:120]}")
        except OSError:
            continue
    return hits


class NeutralityTest(unittest.TestCase):
    def test_shipped_source_names_no_model_or_provider_choice(self):
        hits = _scan(MODEL_CHOICE)
        self.assertEqual(hits, [], "Model/provider choices found in shipped files:\n" + "\n".join(hits))

    def test_shipped_source_names_no_person_or_project(self):
        hits = _scan(PERSONAL)
        self.assertEqual(hits, [], "Personal/project names found in shipped files:\n" + "\n".join(hits))


if __name__ == "__main__":
    unittest.main()
