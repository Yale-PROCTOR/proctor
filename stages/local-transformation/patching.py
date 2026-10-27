from __future__ import annotations


class PatchError(ValueError):
    pass


def _unique_match(
    lines: list[str], context: list[str], start: int, *, eof: bool = False
) -> int:
    if not context:
        if eof or not lines:
            return len(lines)
        raise PatchError("Include unchanged or removed lines to locate each edit.")
    matches = [
        index
        for index in range(start, len(lines) - len(context) + 1)
        if lines[index : index + len(context)] == context
        and (not eof or index + len(context) == len(lines))
    ]
    if not matches:
        raise PatchError("Patch context does not match the Repair Base exactly.")
    if len(matches) != 1:
        raise PatchError("Patch context is ambiguous; include more unchanged context.")
    return matches[0]


def apply_patch(base: str, patch: str) -> str:
    """Apply update hunks to one virtual file without changing filesystem state."""
    patch_lines = patch.replace("\r\n", "\n").split("\n")
    if patch_lines[-1] == "":
        patch_lines.pop()
    if (
        len(patch_lines) < 4
        or patch_lines[:2] != ["*** Begin Patch", "*** Update File: transformation.rs"]
        or patch_lines[-1] != "*** End Patch"
    ):
        raise PatchError(
            "Return only a Codex-style patch beginning with '*** Begin Patch' and "
            "'*** Update File: transformation.rs', ending with '*** End Patch'. "
            "Only updates to transformation.rs are allowed."
        )
    while patch_lines[-2] == "*** End Patch":
        patch_lines.pop()
    lines = base.split("\n") if base else []
    trailing_newline = bool(lines and lines[-1] == "")
    if trailing_newline:
        lines.pop()
    endings = [
        ("\r\n" if line.endswith("\r") else "\n")
        if index < len(lines) - 1 or trailing_newline
        else ""
        for index, line in enumerate(lines)
    ]
    lines = [
        line[:-1] if ending == "\r\n" else line
        for line, ending in zip(lines, endings, strict=True)
    ]
    original = list(zip(lines, endings, strict=True))
    newline = next((ending for ending in endings if ending), "\n")
    output: list[tuple[str, str]] = []
    cursor = 0
    index = 2
    changed = False
    anchors: dict[str, int] = {}
    while index < len(patch_lines) - 1:
        header = patch_lines[index]
        if header != "@@" and not header.startswith("@@ "):
            raise PatchError(
                "Begin each hunk with @@ or @@ followed by a context anchor."
            )
        anchor = header[3:] if header.startswith("@@ ") else ""
        if anchor:
            # The anchor is unchanged text preceding this hunk's context.
            if anchor not in anchors:
                anchors[anchor] = _unique_match(lines, [anchor], cursor)
            anchor_index = anchors[anchor]
            if anchor_index >= cursor:
                output.extend(original[cursor : anchor_index + 1])
                cursor = anchor_index + 1
        index += 1
        old: list[str] = []
        new: list[tuple[str, int | None]] = []
        while index < len(patch_lines) - 1:
            line = patch_lines[index]
            if line == "@@" or line.startswith("@@ ") or line == "*** End of File":
                break
            if not line or line[0] not in " +-":
                raise PatchError("Prefix hunk lines with a space, '-' or '+'.")
            if line[0] != "+":
                old.append(line[1:])
            if line[0] != "-":
                new.append((line[1:], len(old) - 1 if line[0] == " " else None))
            changed |= line[0] in "+-"
            index += 1
        eof = patch_lines[index] == "*** End of File"
        if eof:
            index += 1
            if index != len(patch_lines) - 1:
                raise PatchError("*** End of File must end the final hunk.")
        if not old and not new:
            # Multiple anchors may narrow the search before an actual edit.
            if not anchor or index == len(patch_lines) - 1:
                raise PatchError("A patch must include edit lines.")
            continue
        match = (
            cursor
            if anchor and not old and not eof
            else _unique_match(lines, old, cursor, eof=eof)
        )
        output.extend(original[cursor:match])
        output.extend(
            (
                line,
                endings[match + offset]
                if offset is not None
                else newline
                if trailing_newline
                else "",
            )
            for line, offset in new
        )
        cursor = match + len(old)
    if not changed:
        raise PatchError("A patch must include additions or removals.")
    output.extend(original[cursor:])
    return "".join(
        line + ((ending or newline) if index < len(output) - 1 else ending)
        for index, (line, ending) in enumerate(output)
    )
