"""Fix an unbalanced apostrophe in a single-quoted YAML scalar.

In YAML, a single-quoted scalar escapes an internal `'` by doubling it.
`ticket's` inside `'...'` must be written `ticket''s`, otherwise the string
closes early and the parser sees trailing garbage.

Usage:
  python fix_skill_frontmatter.py <path-to-SKILL.md> [--dry-run]
"""

import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

FM = re.compile(r"^(---\r?\n)(.*?)(\r?\n---\r?\n)", re.S)
LINE = re.compile(r"^(\s*[\w-]+:\s*)'(.*)'(.*)$")


def fix_body(body: str) -> tuple[str, list[str]]:
    """Double unbalanced internal quotes in single-quoted scalars."""
    changes: list[str] = []
    out_lines = []
    for line in body.split("\n"):
        stripped = line.rstrip("\r")
        cr = "\r" if line.endswith("\r") else ""
        m = LINE.match(stripped)
        if m:
            prefix, value, suffix = m.groups()
            if value.count("'") % 2 != 0:
                # The final quote is the closing one; the stray one is internal.
                fixed = value.replace("'", "''")
                changes.append(f"{prefix.strip()}: {value[:40]}... -> doubled quote")
                out_lines.append(f"{prefix}'{fixed}'{suffix}{cr}")
                continue
        out_lines.append(line)
    return "\n".join(out_lines), changes


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    path = Path(argv[0])
    dry_run = "--dry-run" in argv

    raw = path.read_bytes()
    text = raw.decode("utf-8")
    m = FM.match(text)
    if not m:
        print(f"{path}: no YAML frontmatter found")
        return 1

    fixed_body, changes = fix_body(m.group(2))
    if not changes:
        print(f"{path}: nothing to fix")
        return 0

    for c in changes:
        print(f"  {c}")

    if dry_run:
        print("(dry run, not written)")
        return 0

    new_text = m.group(1) + fixed_body + m.group(3) + text[m.end():]
    backup = path.with_suffix(path.suffix + ".bak")
    backup.write_bytes(raw)
    path.write_bytes(new_text.encode("utf-8"))
    print(f"fixed: {path}")
    print(f"backup: {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
