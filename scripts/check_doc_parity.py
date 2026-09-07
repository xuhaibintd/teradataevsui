"""Validate maintained English/Japanese public-document pairs.

The checker intentionally uses only the standard library. It proves that a
Japanese document was reviewed against the current English source and that the
two files have the same mechanical Markdown shape. Translation quality still
requires human review.
"""
from __future__ import annotations

import hashlib
import html
import posixpath
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit, urlunsplit


ROOT = Path(__file__).resolve().parents[1]
SOURCE_HASH = re.compile(
    r"<!--\s*Source-SHA256:\s*([0-9a-f]{64})\s*-->", re.IGNORECASE
)
ENGLISH_SWITCH = re.compile(
    r"> \*\*Language:\*\* English \| \[日本語\]\(([^\s()]+)\)"
)
JAPANESE_SWITCH = re.compile(
    r"> \*\*言語:\*\* \[English\]\(([^\s()]+)\) \| 日本語"
)
ATX_HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+|$)(.*?)\s*$")
SETEXT_HEADING = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
LIST_ITEM = re.compile(r"^([ \t]*)([-+*]|\d+[.)])([ \t]+)(.*)$")
TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
FENCE_OPEN = re.compile(r"^( {0,3})(`{3,}|~{3,})([^\n]*)$")
REFERENCE_DEFINITION = re.compile(r"^ {0,3}\[[^]]+]:[ \t]*\S+")
THEMATIC_BREAK = re.compile(
    r"^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$"
)
LICENSE_SECTION = re.compile(r"^(?:#{1,6}[ \t]+)?(\d+)\.[ \t]+\S")
LANGUAGE_LABEL_EN = re.compile(r"(?:\benglish\b|英語)", re.IGNORECASE)
LANGUAGE_LABEL_JA = re.compile(r"(?:\bjapanese\b|日本語)", re.IGNORECASE)
MAX_METADATA_LINE = 11


class MarkdownValidationError(ValueError):
    """A Markdown construct cannot be validated safely."""


@dataclass(frozen=True)
class FenceBlock:
    info: str
    content: str
    start: int
    end: int


@dataclass(frozen=True)
class MarkdownLink:
    label: str
    target: str
    is_image: bool
    line: int


@dataclass(frozen=True)
class LinkSignature:
    is_image: bool
    kind: str
    target: str
    query: str = ""
    fragment: tuple[int, int] | str | None = None


def _normalize_string(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if normalized.startswith("\ufeff"):
        normalized = normalized[1:]
    return unicodedata.normalize("NFC", normalized)


def normalized_text(path: Path) -> str:
    return _normalize_string(path.read_text(encoding="utf-8-sig"))


def _relative_path(path: Path, root: Path) -> Path:
    """Return a stable repository-relative path across Windows path aliases."""

    return path.resolve().relative_to(root.resolve())


def source_digest(text: str) -> str:
    canonical = _normalize_string(text).rstrip("\n") + "\n"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def japanese_path(source: Path, root: Path = ROOT) -> Path:
    root = root.resolve()
    if source.resolve() == (root / "README.md").resolve():
        return root / "README_ja.md"
    if source.resolve() == (root / "LICENSE").resolve():
        return root / "LICENSE_ja.md"
    return source.with_name(f"{source.stem}_ja{source.suffix}")


def public_pairs(root: Path = ROOT) -> list[tuple[Path, Path]]:
    root = root.resolve()
    sources = [root / "README.md"]
    docs = root / "docs"
    if docs.is_dir():
        sources.extend(
            sorted(
                path
                for path in docs.rglob("*")
                if path.is_file()
                and path.suffix.lower() == ".md"
                and not path.stem.lower().endswith("_ja")
            )
        )
    return [(source, japanese_path(source, root)) for source in sources]


def _metadata_indices(text: str) -> set[int]:
    indices: set[int] = set()
    normalized = _normalize_string(text)
    fenced_lines = {
        line
        for block in _parse_fences(normalized, remove_metadata=False)
        for line in range(block.start, block.end + 1)
    }
    for index, line in enumerate(normalized.splitlines()):
        if index > MAX_METADATA_LINE:
            continue
        if index in fenced_lines:
            continue
        if (
            ENGLISH_SWITCH.fullmatch(line)
            or JAPANESE_SWITCH.fullmatch(line)
            or SOURCE_HASH.fullmatch(line.strip())
        ):
            indices.add(index)
    return indices


def without_translation_header(text: str) -> str:
    lines = _normalize_string(text).splitlines()
    metadata = _metadata_indices(text)
    return "\n".join(line for index, line in enumerate(lines) if index not in metadata)


def _is_fence_close(line: str, character: str, minimum: int) -> bool:
    if len(line) - len(line.lstrip(" ")) > 3:
        return False
    candidate = line.lstrip(" ").rstrip(" \t")
    return len(candidate) >= minimum and set(candidate) == {character}


def _parse_fences(text: str, *, remove_metadata: bool = True) -> list[FenceBlock]:
    lines = _normalize_string(text).splitlines()
    metadata = _metadata_indices(text) if remove_metadata else set()
    blocks: list[FenceBlock] = []
    index = 0
    while index < len(lines):
        if index in metadata:
            index += 1
            continue
        opener = FENCE_OPEN.fullmatch(lines[index])
        if not opener:
            index += 1
            continue
        token = opener.group(2)
        info = opener.group(3).strip()
        if token[0] == "`" and "`" in info:
            raise MarkdownValidationError(
                f"line {index + 1}: a backtick fence info string contains a backtick"
            )
        end = index + 1
        while end < len(lines) and not _is_fence_close(lines[end], token[0], len(token)):
            end += 1
        if end == len(lines):
            raise MarkdownValidationError(
                f"line {index + 1}: unterminated {token[0] * len(token)} fence"
            )
        blocks.append(
            FenceBlock(
                info=info,
                content="\n".join(lines[index + 1 : end]),
                start=index,
                end=end,
            )
        )
        index = end + 1
    return blocks


def fenced_blocks(text: str) -> list[str]:
    """Return normalized, strictly balanced fenced code blocks."""

    return [f"{block.info}\0{block.content}" for block in _parse_fences(text)]


def _table_columns(line: str) -> int:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|") and not stripped.endswith(r"\|"):
        stripped = stripped[:-1]
    separators = 0
    escaped = False
    code_ticks = 0
    index = 0
    while index < len(stripped):
        char = stripped[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\":
            escaped = True
            index += 1
            continue
        if char == "`":
            end = index
            while end < len(stripped) and stripped[end] == "`":
                end += 1
            run = end - index
            if code_ticks == 0:
                code_ticks = run
            elif code_ticks == run:
                code_ticks = 0
            index = end
            continue
        if char == "|" and code_ticks == 0:
            separators += 1
        index += 1
    return separators + 1


def _is_table_separator(line: str) -> bool:
    stripped = line.strip().strip("|")
    cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", stripped)]
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def _blockquote_inner(line: str) -> str | None:
    match = re.match(r"^ {0,3}>[ \t]?(.*)$", line)
    return match.group(1) if match else None


def _structure_from_lines(lines: list[str]) -> list[tuple[object, ...]]:
    text = "\n".join(lines)
    fences = _parse_fences(text, remove_metadata=False)
    fence_by_start = {block.start: block for block in fences}
    signature: list[tuple[object, ...]] = []
    index = 0

    def starts_block(position: int) -> bool:
        if position >= len(lines) or not lines[position].strip():
            return True
        line = lines[position]
        return bool(
            position in fence_by_start
            or ATX_HEADING.fullmatch(line)
            or LIST_ITEM.fullmatch(line)
            or TABLE_ROW.fullmatch(line)
            or _blockquote_inner(line) is not None
            or THEMATIC_BREAK.fullmatch(line)
        )

    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        fence = fence_by_start.get(index)
        if fence:
            signature.append(("fence", fence.info))
            index = fence.end + 1
            continue
        heading = ATX_HEADING.fullmatch(line)
        if heading:
            signature.append(("heading", len(heading.group(1))))
            index += 1
            continue
        if index + 1 < len(lines) and SETEXT_HEADING.fullmatch(lines[index + 1]):
            level = 1 if lines[index + 1].lstrip().startswith("=") else 2
            signature.append(("heading", level))
            index += 2
            continue
        quote = _blockquote_inner(line)
        if quote is not None:
            inner: list[str] = []
            while index < len(lines):
                value = _blockquote_inner(lines[index])
                if value is None:
                    break
                inner.append(value)
                index += 1
            signature.append(("blockquote", tuple(_structure_from_lines(inner))))
            continue
        if TABLE_ROW.fullmatch(line):
            columns: list[int] = []
            separators: list[int] = []
            while index < len(lines) and TABLE_ROW.fullmatch(lines[index]):
                if _is_table_separator(lines[index]):
                    separators.append(len(columns))
                columns.append(_table_columns(lines[index]))
                index += 1
            signature.append(("table", tuple(columns), tuple(separators)))
            continue
        item = LIST_ITEM.fullmatch(line)
        if item:
            marker = item.group(2)
            kind = "ordered" if marker[0].isdigit() else "unordered"
            indent = len(item.group(1).expandtabs(4))
            task = bool(re.match(r"\[[ xX]\](?:[ \t]+|$)", item.group(4)))
            signature.append(("list", kind, indent, task))
            index += 1
            while index < len(lines) and lines[index].strip():
                if (
                    LIST_ITEM.fullmatch(lines[index])
                    or index in fence_by_start
                    or ATX_HEADING.fullmatch(lines[index])
                    or TABLE_ROW.fullmatch(lines[index])
                    or _blockquote_inner(lines[index]) is not None
                    or THEMATIC_BREAK.fullmatch(lines[index])
                ):
                    break
                index += 1
            continue
        if THEMATIC_BREAK.fullmatch(line):
            signature.append(("thematic-break",))
            index += 1
            continue
        signature.append(("paragraph",))
        index += 1
        while index < len(lines) and not starts_block(index):
            if index + 1 < len(lines) and SETEXT_HEADING.fullmatch(lines[index + 1]):
                break
            index += 1
    return signature


def structure_signature(text: str) -> list[tuple[object, ...]]:
    lines = _normalize_string(text).splitlines()
    metadata = _metadata_indices(text)
    body = ["" if index in metadata else line for index, line in enumerate(lines)]
    return _structure_from_lines(body)


def _parse_bracket(text: str, start: int) -> tuple[str, int] | None:
    depth = 1
    index = start + 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == "[":
            depth += 1
        elif text[index] == "]":
            depth -= 1
            if depth == 0:
                return text[start + 1 : index], index
        index += 1
    return None


def _unescape_markdown(value: str) -> str:
    return re.sub(r"\\([!\"#$%&'()*+,./:;<=>?@\[\\\]^_`{|}~-])", r"\1", value)


def _parse_inline_destination(text: str, opening: int) -> tuple[str, int]:
    index = opening + 1
    while index < len(text) and text[index] in " \t\n":
        index += 1
    if index >= len(text):
        raise MarkdownValidationError("unterminated inline link")
    if text[index] == "<":
        index += 1
        start = index
        while index < len(text) and text[index] != ">":
            if text[index] == "\\":
                index += 2
            else:
                index += 1
        if index >= len(text):
            raise MarkdownValidationError("unterminated angle-bracket link destination")
        target = text[start:index]
        index += 1
    else:
        start = index
        depth = 0
        while index < len(text):
            char = text[index]
            if char == "\\":
                index += 2
                continue
            if char == "(":
                depth += 1
            elif char == ")":
                if depth == 0:
                    break
                depth -= 1
            elif char in " \t\n" and depth == 0:
                break
            index += 1
        target = text[start:index]
    while index < len(text) and text[index] in " \t\n":
        index += 1
    if index < len(text) and text[index] in "\"'":
        quote = text[index]
        index += 1
        while index < len(text) and text[index] != quote:
            index += 2 if text[index] == "\\" else 1
        if index >= len(text):
            raise MarkdownValidationError("unterminated inline link title")
        index += 1
        while index < len(text) and text[index] in " \t\n":
            index += 1
    elif index < len(text) and text[index] == "(" and target:
        title_end = text.find(")", index + 1)
        if title_end < 0:
            raise MarkdownValidationError("unterminated inline link title")
        index = title_end + 1
        while index < len(text) and text[index] in " \t\n":
            index += 1
    if index >= len(text) or text[index] != ")":
        raise MarkdownValidationError("malformed or unterminated inline link")
    return _unescape_markdown(target), index


def _extract_links(text: str) -> list[MarkdownLink]:
    normalized = _normalize_string(text)
    lines = normalized.splitlines()
    blocks = _parse_fences(normalized, remove_metadata=False)
    skipped_lines = {
        line for block in blocks for line in range(block.start, block.end + 1)
    }
    for line_number, line in enumerate(lines):
        if line_number not in skipped_lines and REFERENCE_DEFINITION.match(line):
            raise MarkdownValidationError(
                f"line {line_number + 1}: reference-style links are unsupported; use inline links"
            )
    offsets: list[int] = []
    cursor = 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line) + 1
    masked = list(normalized)
    for line_number in skipped_lines:
        start = offsets[line_number]
        for position in range(start, start + len(lines[line_number])):
            masked[position] = " "
    value = "".join(masked)
    links: list[MarkdownLink] = []
    index = 0
    while index < len(value):
        if value[index] == "\\":
            index += 2
            continue
        if value[index] == "`":
            end = index
            while end < len(value) and value[end] == "`":
                end += 1
            marker = value[index:end]
            close = value.find(marker, end)
            if close >= 0:
                index = close + len(marker)
                continue
        if value[index] == "<":
            close = value.find(">", index + 1)
            if close >= 0:
                candidate = value[index + 1 : close]
                candidate_uri = urlsplit(candidate)
                if candidate_uri.scheme or candidate_uri.netloc:
                    links.append(
                        MarkdownLink(
                            label=candidate,
                            target=candidate,
                            is_image=False,
                            line=value.count("\n", 0, index) + 1,
                        )
                    )
                    index = close + 1
                    continue
        image = value[index] == "!" and index + 1 < len(value) and value[index + 1] == "["
        bracket = index + 1 if image else index
        if value[bracket : bracket + 1] != "[":
            index += 1
            continue
        parsed = _parse_bracket(value, bracket)
        if not parsed:
            index += 1
            continue
        label, closing = parsed
        following = closing + 1
        if following < len(value) and value[following] == "[":
            raise MarkdownValidationError(
                f"line {value.count(chr(10), 0, bracket) + 1}: reference-style links are unsupported; use inline links"
            )
        if following >= len(value) or value[following] != "(":
            index = closing + 1
            continue
        target, destination_end = _parse_inline_destination(value, following)
        links.append(
            MarkdownLink(
                label=_unescape_markdown(label),
                target=target,
                is_image=image,
                line=value.count("\n", 0, bracket) + 1,
            )
        )
        index = destination_end + 1
    return links


def _relative_display(path: Path, root: Path) -> str:
    try:
        return _relative_path(path, root).as_posix()
    except ValueError:
        return str(path)


def _walk_exact(root: Path, relative: PurePosixPath) -> Path:
    cursor = root
    for part in relative.parts:
        if part in {"", "."}:
            continue
        try:
            entries = list(cursor.iterdir())
        except OSError as error:
            raise MarkdownValidationError(f"cannot inspect local link path: {error}") from error
        exact = next((entry for entry in entries if entry.name == part), None)
        if exact is None:
            differently_cased = next(
                (entry.name for entry in entries if entry.name.casefold() == part.casefold()),
                None,
            )
            if differently_cased:
                raise MarkdownValidationError(
                    f"local link has wrong case: requested {part!r}, actual {differently_cased!r}"
                )
            raise MarkdownValidationError(f"local link target does not exist: {relative.as_posix()}")
        cursor = exact
    resolved_root = root.resolve()
    resolved = cursor.resolve()
    if not resolved.is_relative_to(resolved_root):
        raise MarkdownValidationError("local link resolves outside the repository")
    if not resolved.is_file():
        raise MarkdownValidationError("local link target is not a file")
    return cursor


def _resolve_local_target(document: Path, target: str, root: Path) -> tuple[Path, str, str]:
    if "\\" in target:
        raise MarkdownValidationError("Markdown link destinations must use forward slashes")
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc:
        raise MarkdownValidationError("not a local target")
    path_part = unquote(parsed.path)
    if path_part.startswith("/"):
        raise MarkdownValidationError("repository-local links must be relative")
    document_relative = _relative_path(document, root)
    base = PurePosixPath(document_relative.parent.as_posix())
    if path_part:
        combined = posixpath.normpath((base / PurePosixPath(path_part)).as_posix())
    else:
        combined = document_relative.as_posix()
    relative = PurePosixPath(combined)
    if relative.is_absolute() or relative.parts[:1] == ("..",):
        raise MarkdownValidationError("local link escapes the repository")
    resolved = _walk_exact(root, relative)
    return resolved, parsed.query, unquote(parsed.fragment)


def _plain_heading(value: str) -> str:
    value = re.sub(r"!\[([^]]*)]\([^)]*\)", r"\1", value)
    value = re.sub(r"\[([^]]+)]\([^)]*\)", r"\1", value)
    value = re.sub(r"`+([^`]*)`+", r"\1", value)
    value = re.sub(r"<[^>]+>", "", value)
    return html.unescape(re.sub(r"[*_~]", "", value)).strip()


def _github_slug(value: str) -> str:
    value = unicodedata.normalize("NFC", _plain_heading(value)).lower().strip()
    value = re.sub(r"[^\w\-\s]", "", value, flags=re.UNICODE)
    return re.sub(r"\s+", "-", value)


def _heading_records(path: Path) -> list[tuple[str, int]]:
    text = normalized_text(path)
    lines = text.splitlines()
    blocks = _parse_fences(text)
    skipped = {line for block in blocks for line in range(block.start, block.end + 1)}
    metadata = _metadata_indices(text)
    records: list[tuple[str, int]] = []
    used: set[str] = set()
    index = 0
    while index < len(lines):
        if index in skipped or index in metadata:
            index += 1
            continue
        match = ATX_HEADING.fullmatch(lines[index])
        if match:
            raw_title = re.sub(r"[ \t]+#+[ \t]*$", "", match.group(2).strip())
            depth = len(match.group(1))
        elif index + 1 < len(lines) and SETEXT_HEADING.fullmatch(lines[index + 1]):
            raw_title = lines[index].strip()
            depth = 1 if lines[index + 1].lstrip().startswith("=") else 2
            index += 1
        else:
            index += 1
            continue
        base = _github_slug(raw_title)
        slug = base
        suffix = 0
        while slug in used:
            suffix += 1
            slug = f"{base}-{suffix}"
        used.add(slug)
        records.append((slug, depth))
        index += 1
    return records


def _fragment_key(path: Path, fragment: str) -> tuple[int, int] | str | None:
    if not fragment:
        return None
    if path.suffix.lower() != ".md":
        return fragment
    records = _heading_records(path)
    for index, (slug, depth) in enumerate(records):
        if fragment == slug:
            return index, depth
    raise MarkdownValidationError(f"Markdown heading fragment does not exist: #{fragment}")


def _canonical_document(relative: PurePosixPath) -> PurePosixPath:
    if relative.as_posix() == "README_ja.md":
        return PurePosixPath("README.md")
    if relative.as_posix() == "LICENSE_ja.md":
        return PurePosixPath("LICENSE")
    if relative.suffix.lower() == ".md" and relative.stem.lower().endswith("_ja"):
        return relative.with_name(f"{relative.stem[:-3]}.md")
    return relative


def _target_locale(path: Path, root: Path) -> tuple[str | None, bool]:
    relative = PurePosixPath(_relative_path(path, root).as_posix())
    if relative.as_posix() == "LICENSE_ja.md" or (
        relative.suffix.lower() == ".md" and relative.stem.lower().endswith("_ja")
    ):
        return "ja", True
    if relative.as_posix() == "LICENSE":
        return "en", (root / "LICENSE_ja.md").is_file()
    if relative.suffix.lower() == ".md":
        return "en", japanese_path(path, root).is_file()
    return None, False


def _external_signature(link: MarkdownLink, parsed) -> LinkSignature:
    normalized = urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, parsed.fragment)
    )
    return LinkSignature(link.is_image, "external", normalized)


def _link_signatures(
    document: Path,
    text: str,
    *,
    language: str,
    root: Path,
    switch_line: int | None,
    problems: list[str],
) -> list[LinkSignature]:
    display = _relative_display(document, root)
    try:
        links = _extract_links(text)
    except MarkdownValidationError as error:
        problems.append(f"invalid Markdown links in {display}: {error}")
        return []
    signatures: list[LinkSignature] = []
    for link in links:
        parsed = urlsplit(link.target)
        if parsed.scheme or parsed.netloc:
            if len(parsed.scheme) == 1 or "\\" in link.target:
                problems.append(f"invalid link in {display} at line {link.line}: {link.target}")
                continue
            signatures.append(_external_signature(link, parsed))
            continue
        try:
            resolved, query, fragment = _resolve_local_target(document, link.target, root)
            fragment_key = _fragment_key(resolved, fragment)
        except MarkdownValidationError as error:
            problems.append(
                f"invalid local link in {display} at line {link.line}: {link.target} ({error})"
            )
            continue
        if switch_line is not None and link.line == switch_line:
            continue
        locale, has_pair = _target_locale(resolved, root)
        if language == "en" and locale == "ja" and not LANGUAGE_LABEL_JA.search(link.label):
            problems.append(
                "English document links to Japanese content without a Japanese label: "
                f"{display} -> {link.target}"
            )
        if language == "ja" and locale == "en" and has_pair and not LANGUAGE_LABEL_EN.search(link.label):
            problems.append(
                "Japanese document links to English content without an English label: "
                f"{display} -> {link.target}"
            )
        relative = PurePosixPath(_relative_path(resolved, root).as_posix())
        signatures.append(
            LinkSignature(
                link.is_image,
                "local",
                _canonical_document(relative).as_posix(),
                query,
                fragment_key,
            )
        )
    return signatures


def local_markdown_links(
    document: Path, text: str, root: Path = ROOT
) -> list[tuple[str, str, Path]]:
    """Return validated local inline links, including image destinations."""

    found: list[tuple[str, str, Path]] = []
    for link in _extract_links(text):
        parsed = urlsplit(link.target)
        if parsed.scheme or parsed.netloc:
            continue
        resolved, _, _ = _resolve_local_target(document, link.target, root)
        found.append((link.label, link.target, resolved))
    return found


def _validate_switch(
    text: str,
    *,
    language: str,
    expected_target: str,
    display: str,
    problems: list[str],
) -> int | None:
    lines = text.splitlines()
    try:
        fenced_lines = {
            line
            for block in _parse_fences(text, remove_metadata=False)
            for line in range(block.start, block.end + 1)
        }
    except MarkdownValidationError:
        fenced_lines = set()
    pattern = ENGLISH_SWITCH if language == "en" else JAPANESE_SWITCH
    expected = (
        f"> **Language:** English | [日本語]({expected_target})"
        if language == "en"
        else f"> **言語:** [English]({expected_target}) | 日本語"
    )
    matches = [
        (index, match)
        for index, line in enumerate(lines)
        if index not in fenced_lines and (match := pattern.fullmatch(line)) is not None
    ]
    all_switches = [
        index
        for index, line in enumerate(lines)
        if index not in fenced_lines
        and (ENGLISH_SWITCH.fullmatch(line) or JAPANESE_SWITCH.fullmatch(line))
    ]
    if len(matches) != 1 or len(all_switches) != 1:
        problems.append(f"{display} must contain exactly one top language switch: {expected}")
        return None
    index, match = matches[0]
    if index > MAX_METADATA_LINE or match.group(1) != expected_target:
        problems.append(f"{display} has an invalid language switch; expected: {expected}")
    return index + 1


def _validate_source_hash(
    text: str,
    source_text: str,
    *,
    display: str,
    problems: list[str],
) -> None:
    expected = source_digest(source_text)
    marker = f"<!-- Source-SHA256: {expected} -->"
    lines = text.splitlines()
    try:
        fenced_lines = {
            line
            for block in _parse_fences(text, remove_metadata=False)
            for line in range(block.start, block.end + 1)
        }
    except MarkdownValidationError:
        fenced_lines = set()
    matches = [
        (index, match)
        for index, line in enumerate(lines)
        if index not in fenced_lines
        and (match := SOURCE_HASH.fullmatch(line.strip())) is not None
    ]
    if len(matches) != 1:
        problems.append(
            f"{display} must contain exactly one top Source-SHA256 marker; expected: {marker}"
        )
        return
    index, match = matches[0]
    if index > MAX_METADATA_LINE:
        problems.append(f"{display} Source-SHA256 marker must be near the top; expected: {marker}")
    if match.group(1).lower() != expected:
        problems.append(f"{display} is stale; expected: {marker}")


def _first_difference(left: list | tuple, right: list | tuple) -> str:
    for index, (left_item, right_item) in enumerate(zip(left, right, strict=False)):
        if left_item != right_item:
            return f"item {index + 1}: English={left_item!r}, Japanese={right_item!r}"
    return f"item count: English={len(left)}, Japanese={len(right)}"


def check_pair(
    source: Path,
    translation: Path,
    problems: list[str],
    root: Path = ROOT,
) -> None:
    root = root.resolve()
    relative_source = _relative_display(source, root)
    relative_translation = _relative_display(translation, root)
    try:
        exact_source = _walk_exact(root, PurePosixPath(_relative_path(source, root).as_posix()))
    except MarkdownValidationError as error:
        problems.append(f"missing English document: {relative_source} ({error})")
        return
    if not exact_source.is_file():
        problems.append(f"missing English document: {relative_source}")
        return
    try:
        exact_translation = _walk_exact(
            root, PurePosixPath(_relative_path(translation, root).as_posix())
        )
    except MarkdownValidationError as error:
        problems.append(f"missing Japanese document: {relative_translation} ({error})")
        return
    if not exact_translation.is_file():
        problems.append(f"missing Japanese document: {relative_translation}")
        return

    source_text = normalized_text(source)
    translation_text = normalized_text(translation)
    source_switch = _validate_switch(
        source_text,
        language="en",
        expected_target=translation.name,
        display=relative_source,
        problems=problems,
    )
    translation_switch = _validate_switch(
        translation_text,
        language="ja",
        expected_target=source.name,
        display=relative_translation,
        problems=problems,
    )
    try:
        source_fenced_lines = {
            line
            for block in _parse_fences(source_text, remove_metadata=False)
            for line in range(block.start, block.end + 1)
        }
    except MarkdownValidationError:
        source_fenced_lines = set()
    source_hashes = [
        line
        for index, line in enumerate(source_text.splitlines())
        if index not in source_fenced_lines and SOURCE_HASH.fullmatch(line.strip())
    ]
    if source_hashes:
        problems.append(f"English source must not contain Source-SHA256 metadata: {relative_source}")
    _validate_source_hash(
        translation_text, source_text, display=relative_translation, problems=problems
    )

    source_structure: list[tuple[object, ...]] | None = None
    translation_structure: list[tuple[object, ...]] | None = None
    source_code: list[str] | None = None
    translation_code: list[str] | None = None
    try:
        source_structure = structure_signature(source_text)
        source_code = fenced_blocks(source_text)
    except MarkdownValidationError as error:
        problems.append(f"invalid Markdown in {relative_source}: {error}")
    try:
        translation_structure = structure_signature(translation_text)
        translation_code = fenced_blocks(translation_text)
    except MarkdownValidationError as error:
        problems.append(f"invalid Markdown in {relative_translation}: {error}")
    if source_structure is not None and translation_structure is not None:
        if source_structure != translation_structure:
            problems.append(
                f"Markdown structure differs: {relative_source} <-> {relative_translation} "
                f"({_first_difference(source_structure, translation_structure)})"
            )
    if source_code is not None and translation_code is not None and source_code != translation_code:
        problems.append(
            f"fenced code differs: {relative_source} <-> {relative_translation} "
            f"({_first_difference(source_code, translation_code)})"
        )

    source_links = _link_signatures(
        source,
        source_text,
        language="en",
        root=root,
        switch_line=source_switch,
        problems=problems,
    )
    translation_links = _link_signatures(
        translation,
        translation_text,
        language="ja",
        root=root,
        switch_line=translation_switch,
        problems=problems,
    )
    if source_links != translation_links:
        problems.append(
            f"ordered links differ: {relative_source} <-> {relative_translation} "
            f"({_first_difference(source_links, translation_links)})"
        )


def _license_signature(text: str, *, japanese: bool) -> tuple[tuple[int, ...], tuple[int, ...]]:
    lines = _normalize_string(text).splitlines()
    metadata = _metadata_indices(text)
    sections: list[int] = []
    paragraph_counts: list[int] = [0]
    in_paragraph = False
    before_sections = True
    for index, line in enumerate(lines):
        if index in metadata:
            continue
        heading = ATX_HEADING.fullmatch(line)
        if japanese and before_sections and heading and len(heading.group(1)) == 1:
            continue
        if japanese and before_sections and _blockquote_inner(line) is not None:
            continue
        section = LICENSE_SECTION.match(line)
        if section:
            if in_paragraph:
                paragraph_counts[-1] += 1
                in_paragraph = False
            sections.append(int(section.group(1)))
            paragraph_counts.append(0)
            before_sections = False
            continue
        if not line.strip():
            if in_paragraph:
                paragraph_counts[-1] += 1
                in_paragraph = False
            continue
        in_paragraph = True
    if in_paragraph:
        paragraph_counts[-1] += 1
    return tuple(sections), tuple(paragraph_counts)


def check_license_pair(root: Path, problems: list[str]) -> None:
    root = root.resolve()
    source = root / "LICENSE"
    translation = root / "LICENSE_ja.md"
    try:
        _walk_exact(root, PurePosixPath("LICENSE"))
    except MarkdownValidationError as error:
        problems.append(f"missing English license: LICENSE ({error})")
        return
    try:
        _walk_exact(root, PurePosixPath("LICENSE_ja.md"))
    except MarkdownValidationError as error:
        problems.append(
            f"missing Japanese license reference translation: LICENSE_ja.md ({error})"
        )
        return
    source_text = normalized_text(source)
    translation_text = normalized_text(translation)
    switch = _validate_switch(
        translation_text,
        language="ja",
        expected_target="LICENSE",
        display="LICENSE_ja.md",
        problems=problems,
    )
    _validate_source_hash(
        translation_text, source_text, display="LICENSE_ja.md", problems=problems
    )
    try:
        source_code = fenced_blocks(source_text)
        translation_code = fenced_blocks(translation_text)
        if source_code != translation_code:
            problems.append("fenced code differs: LICENSE <-> LICENSE_ja.md")
    except MarkdownValidationError as error:
        problems.append(f"invalid Markdown in LICENSE pair: {error}")
    source_signature = _license_signature(source_text, japanese=False)
    translation_signature = _license_signature(translation_text, japanese=True)
    expected_sections = tuple(range(1, len(source_signature[0]) + 1))
    if source_signature[0] != expected_sections:
        problems.append(
            "English LICENSE numbered sections are not contiguous from 1: "
            f"{source_signature[0]!r}"
        )
    if not source_signature[0] or source_signature != translation_signature:
        problems.append(
            "LICENSE numbered sections/body paragraphs differ: LICENSE <-> LICENSE_ja.md "
            f"({_first_difference(source_signature, translation_signature)})"
        )
    source_links = _link_signatures(
        source,
        source_text,
        language="en",
        root=root,
        switch_line=None,
        problems=problems,
    )
    translation_links = _link_signatures(
        translation,
        translation_text,
        language="ja",
        root=root,
        switch_line=switch,
        problems=problems,
    )
    if source_links != translation_links:
        problems.append("ordered links differ: LICENSE <-> LICENSE_ja.md")


def check_repository(root: Path = ROOT) -> list[str]:
    root = root.resolve()
    problems: list[str] = []
    pairs = public_pairs(root)
    expected_translations = {
        _relative_path(translation, root).as_posix() for _, translation in pairs
    }
    docs = root / "docs"
    actual_translations = {
        _relative_path(path, root).as_posix()
        for path in ([root / "README_ja.md"] if (root / "README_ja.md").is_file() else [])
    }
    if docs.is_dir():
        actual_translations.update(
            _relative_path(path, root).as_posix()
            for path in docs.rglob("*")
            if path.is_file()
            and path.suffix.lower() == ".md"
            and path.stem.lower().endswith("_ja")
        )
    for orphan in sorted(actual_translations - expected_translations):
        problems.append(f"Japanese document has no English source: {orphan}")
    for source, translation in pairs:
        check_pair(source, translation, problems, root)
    check_license_pair(root, problems)
    return problems


def main(root: Path = ROOT) -> int:
    problems = check_repository(root)
    if problems:
        print("Documentation parity check failed:", file=sys.stderr)
        for problem in problems:
            print(f"- {problem}", file=sys.stderr)
        return 1
    pair_count = len(public_pairs(root))
    print(
        f"Documentation parity passed: {pair_count} English/Japanese pairs "
        "plus LICENSE reference translation."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
