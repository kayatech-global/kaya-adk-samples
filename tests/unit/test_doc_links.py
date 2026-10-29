"""Every relative link in the Markdown files points at a file, and at a heading when it names one."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FILES = sorted([*ROOT.glob("docs/**/*.md"), ROOT / "README.md", ROOT / "AGENTS.md"])
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")


def slug(heading: str) -> str:
    """GitHub's anchor of a heading: lower case, punctuation dropped, spaces as hyphens."""
    text = re.sub(r"[`*_]", "", heading.strip().lower())
    return re.sub(r"\s", "-", re.sub(r"[^\w\s-]", "", text))


def anchors(path: Path) -> set[str]:
    in_code = False
    found: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code
        elif not in_code and (m := re.match(r"#{1,6}\s+(.*)", line)):
            found.add(slug(m.group(1)))
    return found


@pytest.mark.parametrize("doc", FILES, ids=[str(f.relative_to(ROOT)) for f in FILES])
def test_relative_links_resolve(doc: Path):
    text = re.sub(r"```.*?```", "", doc.read_text(encoding="utf-8"), flags=re.DOTALL)
    broken = []
    for target in LINK.findall(text):
        if re.match(r"[a-z]+:", target):
            continue
        name, _, anchor = target.partition("#")
        path = (doc.parent / name).resolve() if name else doc
        if not path.exists() or anchor and path.suffix == ".md" and anchor not in anchors(path):
            broken.append(target)
    assert not broken, f"{doc.relative_to(ROOT)}: {broken}"
