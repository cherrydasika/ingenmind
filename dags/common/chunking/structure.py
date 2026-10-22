"""Structure-aware chunking (R21): cut along headings, paragraphs and list
items instead of fixed lengths, so a chunk holds whole ideas and says which
section it sits in.

The text is markdown-like: `#`-headings (trafilatura's markdown output, or an
ad-hoc document), blocks separated by blank lines, list items (`- `, `* `,
`1. `) kept with their list. Text without headings (a PDF, plain text) is
cut along its paragraphs and lines the same way.

- Each block carries its heading path ("Refunds > Delay Repay").
- Blocks of one section are packed together up to the size; a chunk never
  spans two sections, except that a section smaller than MIN_CHARS joins
  the next one under the same top heading (the heading the chunk shares
  becomes its path, the other headings stay in its text).
- A block longer than the room is cut at sentences, and a sentence longer
  than that at spaces (fixed_size).
- The chunk's text starts with its heading path, so both searches see it;
  the path is also returned for the chunk's payload.
No overlap: the pieces are whole paragraphs or sentences."""

import re

from .fixed_size import chunk_text as fixed_chunks

MIN_CHARS = 250           # a smaller section joins the next one
MAX_PATH_CHARS = 200      # a longer heading path is shortened
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")


def _clean_heading(text: str) -> str:
    return re.sub(r"[*_`]+", "", text).strip()


def blocks(text: str) -> list[tuple[tuple[str, ...], str]]:
    """(heading path, block) in order: paragraphs, whole lists, lone lines."""
    by_blank_lines = "\n\n" in text      # else (plain extracted text) each line is a block
    path: list[tuple[int, str]] = []
    out: list[tuple[tuple[str, ...], str]] = []
    current: list[str] = []
    in_list = False

    def flush():
        nonlocal current, in_list
        block = "\n".join(current).strip()
        if block:
            out.append((tuple(name for _, name in path), block))
        current, in_list = [], False

    for line in text.splitlines():
        heading = HEADING.match(line)
        if heading:
            flush()
            level = len(heading.group(1))
            path = [(lvl, name) for lvl, name in path if lvl < level] + [(level, _clean_heading(heading.group(2)))]
            continue
        if not line.strip():
            flush()
            continue
        is_item = bool(LIST_ITEM.match(line))
        continues = in_list and line[:1] in (" ", "\t")      # a list item's wrapped line
        if current:
            if by_blank_lines:
                if (is_item and not in_list) or (in_list and not (is_item or continues)):
                    flush()                                    # a list starts or ends
            elif not (in_list and (is_item or continues)):
                flush()
        current.append(line.rstrip())
        in_list = is_item or continues
    flush()
    return out


def _path_line(path: tuple[str, ...]) -> str:
    line = " > ".join(path)
    return line if len(line) <= MAX_PATH_CHARS else "… > " + line[-(MAX_PATH_CHARS - 4):].split(" > ", 1)[-1]


def _pieces(block: str, room: int) -> list[str]:
    """A block cut to fit `room`: whole if it fits, else by sentences, else by spaces."""
    if len(block) <= room:
        return [block]
    out, current = [], ""
    for sentence in SENTENCE_END.split(block):
        for part in ([sentence] if len(sentence) <= room else fixed_chunks(sentence, room, 0)):
            if current and len(current) + 1 + len(part) > room:
                out.append(current)
                current = ""
            current = f"{current} {part}".strip() if current else part
    if current:
        out.append(current)
    return out


def chunk_sections(text: str, chunk_size: int) -> list[dict]:
    """[{"text": heading path + body, "headings": [...]}] in order."""
    chunks: list[dict] = []          # {"path", "parts", "size"}

    def room(path: tuple[str, ...]) -> int:
        return max(chunk_size // 2, chunk_size - (len(_path_line(path)) + 2 if path else 0))

    for path, block in blocks(text):
        for piece in _pieces(block, room(path)):
            last = chunks[-1] if chunks else None
            if last and last["path"] == path and last["size"] + 2 + len(piece) <= room(path):
                last["parts"].append(piece)
                last["size"] += 2 + len(piece)
                continue
            if last and last.get("open_path") == path and last["size"] + 2 + len(piece) <= room(last["path"]):
                last["parts"].append(piece)            # the joined section goes on
                last["size"] += 2 + len(piece)
                continue
            if last and last["path"] != path and last["size"] < MIN_CHARS and path[:1] == last["path"][:1]:
                shared = _common(last["path"], path)
                own = [p for p in (" > ".join(last["path"][len(shared):]),) if p]
                new = [p for p in (" > ".join(path[len(shared):]),) if p]
                parts = own + last["parts"] + new + [piece]
                size = sum(len(p) for p in parts) + 2 * (len(parts) - 1)
                if size <= room(shared):
                    last.update(path=shared, parts=parts, size=size, open_path=path)
                    continue
            chunks.append({"path": path, "parts": [piece], "size": len(piece)})
    out = []
    for c in chunks:
        body = "\n\n".join(c["parts"])
        out.append({"text": f"{_path_line(c['path'])}\n\n{body}" if c["path"] else body, "headings": list(c["path"])})
    return out


def _common(a: tuple[str, ...], b: tuple[str, ...]) -> tuple[str, ...]:
    n = 0
    while n < min(len(a), len(b)) and a[n] == b[n]:
        n += 1
    return a[:n]


def chunk_text(text: str, chunk_size: int, overlap: int = 0) -> list[str]:
    """The same signature as the other strategies (overlap is not used)."""
    return [c["text"] for c in chunk_sections(text, chunk_size)]
