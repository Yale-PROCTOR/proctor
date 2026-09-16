from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from model import SkeletonError


@dataclass(frozen=True)
class PrintfAdapter:
    name: str
    conversions: str
    call: str
    note: str


PRINTF_ADAPTERS = (
    PrintfAdapter(
        "signed",
        "d/i",
        "proctor_libc::printf::signed(value)",
        "Use for signed decimal conversions.",
    ),
    PrintfAdapter(
        "unsigned",
        "u/o/x/X",
        "proctor_libc::printf::unsigned(value)",
        "Use for unsigned decimal, octal, and hexadecimal conversions.",
    ),
    PrintfAdapter(
        "fixed",
        "f",
        "proctor_libc::printf::fixed(value)",
        "Use for lower-case fixed-point conversion.",
    ),
    PrintfAdapter(
        "fixed_upper",
        "F",
        "proctor_libc::printf::fixed_upper(value)",
        "Use for upper-case fixed-point conversion.",
    ),
    PrintfAdapter(
        "scientific",
        "e/E",
        "proctor_libc::printf::scientific(value)",
        "Use for scientific conversion; the target field selects Rust's e/E formatting trait.",
    ),
    PrintfAdapter(
        "general",
        "g",
        "proctor_libc::printf::general(value)",
        "Use for lower-case general conversion.",
    ),
    PrintfAdapter(
        "general_upper",
        "G",
        "proctor_libc::printf::general_upper(value)",
        "Use for upper-case general conversion.",
    ),
    PrintfAdapter(
        "hex_float",
        "a/A",
        "proctor_libc::printf::hex_float(value)",
        "Use for hexadecimal floating conversion; the target field selects Rust's x/X formatting trait.",
    ),
    PrintfAdapter(
        "byte_string",
        "s",
        "proctor_libc::printf::byte_string(value)",
        "It stops at the first NUL subject to precision, counts bytes for width and precision, and requires the selected bytes to be valid UTF-8.",
    ),
)

_FAMILY_BY_CONVERSION = {
    "d": "signed",
    "i": "signed",
    "u": "unsigned",
    "o": "unsigned",
    "x": "unsigned",
    "X": "unsigned",
    "f": "fixed",
    "F": "fixed_upper",
    "e": "scientific",
    "E": "scientific",
    "g": "general",
    "G": "general_upper",
    "a": "hex_float",
    "A": "hex_float",
    "s": "byte_string",
}
_SPACE_SIGN_FAMILIES = {
    "signed",
    "fixed",
    "fixed_upper",
    "scientific",
    "general",
    "general_upper",
    "hex_float",
}
_FLAGS = frozenset("-+ 0#'")


def classify_printf_specifiers(
    specifiers: Iterable[str],
) -> tuple[tuple[str, ...], bool]:
    families: set[str] = set()
    space_sign = False
    for specifier in specifiers:
        if (
            not specifier
            or not specifier.isascii()
            or not specifier.startswith("%")
            or specifier[-1] not in _FAMILY_BY_CONVERSION
        ):
            raise SkeletonError(
                f"printf_format_specifiers contains unsupported value {specifier!r}"
            )
        family = _FAMILY_BY_CONVERSION[specifier[-1]]
        families.add(family)
        cursor = 1
        while cursor < len(specifier) and specifier[cursor] in _FLAGS:
            if specifier[cursor] == " " and family in _SPACE_SIGN_FAMILIES:
                space_sign = True
            cursor += 1
    ordered = tuple(
        adapter.name for adapter in PRINTF_ADAPTERS if adapter.name in families
    )
    return ordered, space_sign


def render_printf_guidance(specifiers: Iterable[str]) -> str:
    families, space_sign = classify_printf_specifiers(specifiers)
    if not families:
        return ""
    selected = {family for family in families}
    accepted_types = []
    if "signed" in selected:
        accepted_types.append(
            "The signed adapter accepts exactly `i8`, `i16`, `i32`, `i64`, and `isize`."
        )
    if "unsigned" in selected:
        accepted_types.append(
            "The unsigned adapter accepts exactly `u8`, `u16`, `u32`, `u64`, and `usize`."
        )
    if selected.intersection(_SPACE_SIGN_FAMILIES - {"signed"}):
        accepted_types.append(
            "The selected floating adapters accept exactly `f32`, `f64`, and "
            "`f128::f128`; this includes promoted floating values, and "
            "the last type is used for `L`."
        )
    if "byte_string" in selected:
        accepted_types.append("The byte-string adapter accepts exactly `&[i8]`.")
    cast_warning = ""
    if selected.intersection({"signed", "unsigned"}):
        cast_warning = " Do not cast values to unsupported `i128` or `u128`."
    entries = []
    for adapter in PRINTF_ADAPTERS:
        if adapter.name not in selected:
            continue
        entries.append(
            f"For `{adapter.conversions}`, fill the slot with "
            f"`{adapter.call}`. {adapter.note}"
        )
    space = ""
    if space_sign:
        space = (
            "\n\nFor an applicable source specifier whose flag prefix contains a "
            "space, chain `.space_sign()` directly on the adapter call result "
            "by appending it to the call expression shown above; Rust + takes "
            "precedence when both flags occur."
        )
    return (
        "The target skeleton's Rust format string, static width, precision, "
        "formatting trait, and number of argument slots are trusted and must not "
        "change; preserve the source order of consuming values: fill the existing "
        "argument slots in order and do not swap slots. Pass each value after the C "
        "length conversion. Use the fully qualified call expressions below and let "
        "Rust infer their return types; do not define or import any item for these "
        "calls, including traits or adapter types."
        + cast_warning
        + "\n\n"
        + "\n\n".join(accepted_types + entries)
        + space
    )
