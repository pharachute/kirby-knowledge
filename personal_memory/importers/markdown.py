"""Markdown -> capture-ready text (light, structure-preserving cleaning).

The spec for this phase is explicit: the importer must produce *clean, stable text*
for Memory Formation and nothing more.  So the cleaning rules are deliberately
conservative and fully documented -- no summarising, no chunking, no classification,
no importance judgment (all of that belongs to Phase 2's Memory Formation).

Rules applied (in order)
------------------------
1. line endings are normalised (CRLF/CR -> LF);
2. **fenced-code delimiter lines are dropped, their content is kept** -- a ```` ``` ````
   line is pure syntax while the code inside it is content (the spec asks to keep code
   text unless there is an explicit reason to delete it);
3. runs of blank lines collapse to a single blank line (stability across editors);
4. everything else is preserved verbatim: headings **including their ``#`` markers**,
   paragraphs, list markers, block quotes, tables, links, images, inline emphasis,
   inline code, HTML comments and any YAML front matter.

Title extraction order: the caller's explicit title, then the first ATX level-1
heading (``# Title``), then a ``title:`` key inside a leading YAML front-matter block,
and finally the file name (applied by the caller).

What is deliberately **not** done: removing ``#``/``-``/``**`` markers, rewriting or
dropping links, stripping HTML comments, deleting front matter, or "beautifying" the
prose.  Deleting text we cannot prove is noise is exactly what this phase forbids.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["MarkdownDocument", "clean_markdown", "split_front_matter"]

_FENCE_RE = re.compile(r"^\s{0,3}(?:`{3,}|~{3,})")
_H1_RE = re.compile(r"^\s{0,3}#\s+(?P<title>.+?)\s*#*\s*$")
_FRONT_MATTER_TITLE_RE = re.compile(r"^\s*title\s*:\s*(?P<value>.*?)\s*$", re.IGNORECASE)
_FRONT_MATTER_DELIMITER = "---"


@dataclass(frozen=True)
class MarkdownDocument:
    """The two things Memory Formation needs from a Markdown file."""

    title: str | None
    body: str
    #: ``"heading"`` | ``"front_matter"`` | ``"none"``
    title_source: str


def split_front_matter(lines: list[str]) -> tuple[list[str], list[str]]:
    """Identify a leading ``---`` YAML block.

    Returns ``(front_matter_lines, body_lines)`` where ``body_lines`` excludes the
    block.  A file that does not start with a ``---`` line, or whose block is never
    closed, has no front matter: the list is empty and all lines are body lines.

    This is only used to look for a ``title:`` fallback; :func:`clean_markdown` keeps
    the **whole** document in the body, because this phase does not delete text it
    cannot prove is noise.
    """
    if not lines or lines[0].strip() != _FRONT_MATTER_DELIMITER:
        return [], lines
    for index in range(1, len(lines)):
        if lines[index].strip() == _FRONT_MATTER_DELIMITER:
            return lines[1:index], lines[index + 1 :]
    return [], lines


def _front_matter_title(front_matter: list[str]) -> str | None:
    for line in front_matter:
        match = _FRONT_MATTER_TITLE_RE.match(line)
        if match:
            value = match.group("value").strip().strip('"').strip("'").strip()
            if value:
                return value
    return None


def clean_markdown(text: str) -> MarkdownDocument:
    """Turn Markdown source into ``(title, body)`` without deleting real content."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    front_matter, body_lines = split_front_matter(lines)

    title: str | None = None
    title_source = "none"
    for line in body_lines:  # the first ATX level-1 heading wins
        match = _H1_RE.match(line)
        if match:
            candidate = match.group("title").strip()
            if candidate:
                title, title_source = candidate, "heading"
            break
    if title is None:
        front_title = _front_matter_title(front_matter)
        if front_title:
            title, title_source = front_title, "front_matter"

    kept: list[str] = []
    for line in lines:  # the whole document: front matter is kept, not deleted
        if _FENCE_RE.match(line):
            continue  # syntax only: the code between the fences stays
        stripped = line.rstrip()
        if not stripped:
            if kept and kept[-1] != "":
                kept.append("")  # collapse any blank run to one blank line
            continue
        kept.append(stripped)

    return MarkdownDocument(title=title, body="\n".join(kept).strip(), title_source=title_source)
