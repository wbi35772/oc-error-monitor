"""Find SKILL.md files whose YAML frontmatter fails to parse.

OpenChamber logs a warning for each one it loads, which floods main.log.
The usual cause is an unescaped apostrophe inside a single-quoted YAML
scalar (e.g. `ticket's`), which closes the string early and leaves the
remainder as an unexpected scalar.
"""

import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

ROOTS = [Path.home() / ".config/opencode/skills"]

FM = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n", re.S)


def scalar_quote_problems(body: str) -> list[str]:
    """Return descriptions whose single-quoted scalar is broken."""
    problems = []
    for line in body.split("\n"):
        m = re.match(r"^(\w+):\s*'(.*)'\s*$", line.rstrip("\r"))
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        # A single-quoted YAML scalar escapes an internal quote by doubling it.
        inner_quotes = value.count("'")
        if inner_quotes % 2 != 0:
            problems.append(f"{key}: unbalanced quote at {value.find(chr(39))}")
    return problems


def main() -> int:
    hits = 0
    for root in ROOTS:
        if not root.exists():
            continue
        for path in sorted(root.rglob("SKILL.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except Exception as exc:
                print(f"[read-error] {path}: {exc}")
                continue
            m = FM.match(text)
            if not m:
                continue
            problems = scalar_quote_problems(m.group(1))
            if problems:
                hits += 1
                print(f"[broken] {path}")
                for p in problems:
                    print(f"         {p}")
    print(f"\n{hits} file(s) with unbalanced single-quoted scalars")
    return 0


if __name__ == "__main__":
    sys.exit(main())
