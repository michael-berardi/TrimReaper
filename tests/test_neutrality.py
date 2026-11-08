"""TrimReaper ships no built-in model, provider, fallback or personal-name choice.

Those choices belong in the operator's own configuration. This test keeps the shipped
source, tests, documentation and README free of them. Exceptions are limited to provider
adapter modules that name a provider's own catalog and to legal attribution files.
"""

import os
import re
import tempfile
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
    r"\b(?:Mike|Berardi|Felix|Mouseia|Liberty Design|"
    r"libertydesign)\b|/Users/[a-z]|overseer(?!-reap-archive\")",
    re.IGNORECASE,
)


def _private_terms(path=None):
    path = path or os.environ.get("OSS_PRIVATE_TERMS") or os.path.expanduser("~/.config/oss-audit/names.txt")
    try:
        with open(path, encoding="utf-8") as handle:
            terms = [line.strip() for line in handle if line.strip() and not line.lstrip().startswith("#")]
    except FileNotFoundError:
        return None
    patterns = []
    for term in terms:
        escaped = re.escape(term)
        patterns.append((r"\b" if re.match(r"\w", term[0]) else "") + escaped
                        + (r"\b" if re.match(r"\w", term[-1]) else ""))
    return re.compile("|".join(patterns), re.IGNORECASE) if patterns else None

# The neutrality test contains the denylist itself.
NEUTRALITY_TEST = os.path.join(ROOT, "tests", "test_neutrality.py")
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
    tests = os.path.join(ROOT, "tests")
    if os.path.isdir(tests):
        paths.extend(_files(tests))
    docs = os.path.join(ROOT, "docs")
    if os.path.isdir(docs):
        paths.extend(_files(docs))
    readme = os.path.join(ROOT, "README.md")
    if os.path.isfile(readme):
        paths.append(readme)
    return [
        path
        for path in paths
        if path != NEUTRALITY_TEST
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
    def test_private_terms_loader(self):
        with tempfile.TemporaryDirectory() as directory:
            terms = os.path.join(directory, "terms.txt")
            sample = os.path.join(directory, "sample.txt")
            with open(terms, "w", encoding="utf-8") as handle:
                handle.write("# comment\n\nzzqx-private-term\n")
            pattern = _private_terms(terms)
            with open(sample, "w", encoding="utf-8") as handle:
                handle.write("A ZZQX-private-term appears here")
            with open(sample, encoding="utf-8") as handle:
                self.assertIsNotNone(pattern.search(handle.read()))
            with open(sample, "w", encoding="utf-8") as handle:
                handle.write("A neutral sample")
            with open(sample, encoding="utf-8") as handle:
                self.assertIsNone(pattern.search(handle.read()))

    def test_shipped_source_names_no_model_or_provider_choice(self):
        hits = _scan(MODEL_CHOICE)
        self.assertEqual(hits, [], "Model/provider choices found in shipped files:\n" + "\n".join(hits))

    def test_shipped_source_names_no_person_or_project(self):
        hits = _scan(PERSONAL)
        private = _private_terms()
        if private is not None:
            hits.extend(_scan(private))
        self.assertEqual(hits, [], "Personal/project names found in shipped files:\n" + "\n".join(hits))


if __name__ == "__main__":
    unittest.main()
