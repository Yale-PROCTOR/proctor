from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class FunctionReference:
    documentation: str
    signature: str


@dataclass(frozen=True)
class LibcGuidance:
    foreign_names: tuple[str, ...]
    replacement: str
    references: tuple[FunctionReference, ...]


def _reference(documentation: str, signature: str) -> tuple[FunctionReference, ...]:
    return (FunctionReference(documentation, signature),)


LIBC_GUIDANCE = (
    LibcGuidance(
        ("fgetc", "getc"),
        "fgetc",
        _reference(
            "Reads the next byte from `r`.\n\n"
            "Returns the byte and success, `-1` and success at end-of-file, or `-1` and\n"
            "the I/O error.",
            "pub fn fgetc<R: Read + ?Sized>(r: &mut R) -> (i32, io::Result<()>);",
        ),
    ),
    LibcGuidance(
        ("fgets",),
        "fgets",
        _reference(
            "Reads a line from `r` into `buf`, including the newline and a trailing null byte.\n\n"
            "Returns the buffer and success when it writes a null terminator, `None` and\n"
            "success at end-of-file before any input, or `None` and an error.",
            "pub fn fgets<'buf, R: BufRead + ?Sized>(\n"
            "    buf: &'buf mut [i8],\n"
            "    r: &mut R,\n"
            ") -> (Option<&'buf mut [i8]>, io::Result<()>);",
        ),
    ),
    LibcGuidance(
        ("fputc", "putc"),
        "fputc",
        _reference(
            "Writes `c`, converted to an unsigned byte, to `w`.\n\n"
            "Returns the written byte and success, or `-1` and the I/O error.",
            "pub fn fputc<W: Write + ?Sized>(c: i32, w: &mut W) -> (i32, io::Result<()>);",
        ),
    ),
    LibcGuidance(
        ("fputs",),
        "fputs",
        _reference(
            "Writes the bytes in `buf` preceding the first null byte to `w`.\n\n"
            "Returns zero and success, or `-1` and the I/O error.",
            "pub fn fputs<W: Write + ?Sized>(buf: &[i8], w: &mut W) -> (i32, io::Result<()>);",
        ),
    ),
    LibcGuidance(
        ("fread",),
        "fread",
        _reference(
            "Reads binary input from `r` into `buf`.\n\n"
            "Returns the number of complete elements read and any I/O error. A short\n"
            "count without an error means end-of-file.",
            "pub fn fread<T: bytemuck::AnyBitPattern, R: BufRead + ?Sized>(\n"
            "    buf: &mut [T],\n"
            "    r: &mut R,\n"
            ") -> (usize, io::Result<()>);",
        ),
    ),
    LibcGuidance(
        ("fseek",),
        "fseek",
        _reference(
            "Sets the position of `s` according to `pos`.\n\n"
            "Returns zero and success, or `-1` and the seek or position-conversion error.",
            "pub fn fseek<S: Seek + ?Sized>(s: &mut S, pos: SeekFrom) -> (i32, io::Result<()>);",
        ),
    ),
    LibcGuidance(
        ("ftell",),
        "ftell",
        _reference(
            "Returns the current position of `s` as a byte offset from its beginning.\n\n"
            "Returns the offset and success, or `-1` and the seek or position-conversion\n"
            "error.",
            "pub fn ftell<S: Seek + ?Sized>(s: &mut S) -> (i64, io::Result<()>);",
        ),
    ),
    LibcGuidance(
        ("fwrite",),
        "fwrite",
        _reference(
            "Writes binary output from `buf` to `w`.\n\n"
            "Returns the number of complete elements written and any I/O error.",
            "pub fn fwrite<T: bytemuck::NoUninit, W: Write + ?Sized>(\n"
            "    buf: &[T],\n"
            "    w: &mut W,\n"
            ") -> (usize, io::Result<()>);",
        ),
    ),
    LibcGuidance(
        ("getchar",),
        "getchar",
        _reference(
            "Reads the next byte from standard input.\n\n"
            "Returns the byte and success, `-1` and success at end-of-file, or `-1` and\n"
            "the I/O error.",
            "pub fn getchar() -> (i32, io::Result<()>);",
        ),
    ),
    LibcGuidance(
        ("putchar",),
        "putchar",
        _reference(
            "Writes `c`, converted to an unsigned byte, to standard output.\n\n"
            "Returns the written byte and success, or `-1` and the I/O error.",
            "pub fn putchar(c: i32) -> (i32, io::Result<()>);",
        ),
    ),
    LibcGuidance(
        ("puts",),
        "puts",
        _reference(
            "Writes the bytes in `buf` preceding the first null byte, followed by a\n"
            "newline, to standard output.\n\n"
            "Returns zero and success, or `-1` and the I/O error.",
            "pub fn puts(buf: &[i8]) -> (i32, io::Result<()>);",
        ),
    ),
    LibcGuidance(
        ("remove",),
        "remove",
        _reference(
            "Removes the file or empty directory named by `path`.\n\n"
            "Returns zero and success, or `-1` and the filesystem error.",
            "pub fn remove(path: &[i8]) -> (i32, io::Result<()>);",
        ),
    ),
    LibcGuidance(
        ("rename",),
        "rename",
        _reference(
            "Renames the file or directory named by `old` to `new`.\n\n"
            "Returns zero and success, or `-1` and the filesystem error.",
            "pub fn rename(old: &[i8], new: &[i8]) -> (i32, io::Result<()>);",
        ),
    ),
    LibcGuidance(
        ("rewind",),
        "rewind",
        _reference(
            "Sets the position of `s` to its beginning.",
            "pub fn rewind<S: Seek + ?Sized>(s: &mut S) -> io::Result<()>;",
        ),
    ),
    LibcGuidance(
        ("atof",),
        "atof",
        _reference(
            "Converts the initial floating-point number in `buf` to an `f64`.",
            "pub fn atof(buf: &[i8]) -> f64;",
        ),
    ),
    LibcGuidance(
        ("atoi",),
        "atoi",
        _reference(
            "Converts the initial decimal integer in `buf` to an `i32`.",
            "pub fn atoi(buf: &[i8]) -> i32;",
        ),
    ),
    LibcGuidance(
        ("atol",),
        "atol",
        _reference(
            "Converts the initial decimal integer in `buf` to an `i64`.",
            "pub fn atol(buf: &[i8]) -> i64;",
        ),
    ),
    LibcGuidance(
        ("strtod",),
        "strtod",
        _reference(
            "Converts the initial floating-point number in `buf` to an `f64`.\n\n"
            "Returns the converted value, the unconsumed suffix, and the conversion\n"
            "status. `StrtoFloatError::OutOfRange` is the only error variant and reports\n"
            "overflow or inexact underflow.",
            "pub fn strtod(buf: &[i8]) -> ((f64, &[i8]), Result<(), StrtoFloatError>);",
        ),
    ),
    LibcGuidance(
        ("strtof",),
        "strtof",
        _reference(
            "Converts the initial floating-point number in `buf` to an `f32`.\n\n"
            "Returns the converted value, the unconsumed suffix, and the conversion\n"
            "status. `StrtoFloatError::OutOfRange` is the only error variant and reports\n"
            "overflow or inexact underflow.",
            "pub fn strtof(buf: &[i8]) -> ((f32, &[i8]), Result<(), StrtoFloatError>);",
        ),
    ),
    LibcGuidance(
        ("strtol",),
        "strtol",
        _reference(
            "Converts the initial integer in `buf` using `base`.\n\n"
            "Returns the converted value, the unconsumed suffix, and the conversion\n"
            "status. `StrtoIntError::InvalidBase` and `StrtoIntError::OutOfRange` are the\n"
            "only error variants; they report an unsupported base and overflow,\n"
            "respectively.",
            "pub fn strtol(buf: &[i8], base: i32) -> ((i64, &[i8]), Result<(), StrtoIntError>);",
        ),
    ),
    LibcGuidance(
        ("strtold",),
        "strtold",
        _reference(
            "Converts the initial floating-point number in `buf` to [`struct@f128::f128`].\n\n"
            "Returns the converted value, the unconsumed suffix, and the conversion\n"
            "status. `StrtoFloatError::OutOfRange` is the only error variant and reports\n"
            "overflow or inexact underflow.",
            "pub fn strtold(buf: &[i8]) -> ((f128::f128, &[i8]), Result<(), StrtoFloatError>);",
        ),
    ),
    LibcGuidance(
        ("strtoul",),
        "strtoul",
        _reference(
            "Converts the initial unsigned integer in `buf` using `base`.\n\n"
            "Returns the converted value, the unconsumed suffix, and the conversion\n"
            "status. `StrtoIntError::InvalidBase` and `StrtoIntError::OutOfRange` are the\n"
            "only error variants; they report an unsupported base and overflow,\n"
            "respectively.",
            "pub fn strtoul(buf: &[i8], base: i32) -> ((u64, &[i8]), Result<(), StrtoIntError>);",
        ),
    ),
    LibcGuidance(
        ("memchr",),
        "memchr",
        (
            FunctionReference(
                "Finds `c`, converted to `u8`, in `buf` and returns its suffix, or `None` if not found.",
                "pub fn memchr(buf: &[u8], c: i32) -> Option<&[u8]>;",
            ),
            FunctionReference(
                "Finds `c`, converted to `u8`, in `buf` and returns its mutable suffix, or `None` if not found.",
                "pub fn memchr_mut(buf: &mut [u8], c: i32) -> Option<&mut [u8]>;",
            ),
        ),
    ),
    LibcGuidance(
        ("memcmp",),
        "memcmp",
        _reference(
            "Compares the first `n` bytes of two memory regions.",
            "pub fn memcmp(buf1: &[u8], buf2: &[u8], n: usize) -> i32;",
        ),
    ),
    LibcGuidance(
        ("strcat",),
        "strcat",
        _reference(
            "Appends the null-terminated byte string `s2` to `s1`.",
            "pub fn strcat<'s>(s1: &'s mut [i8], s2: &[i8]) -> &'s mut [i8];",
        ),
    ),
    LibcGuidance(
        ("strchr",),
        "strchr",
        (
            FunctionReference(
                "Finds `c`, converted to `i8`, in null-terminated `s` and returns its suffix, or `None` if not found.",
                "pub fn strchr(s: &[i8], c: i32) -> Option<&[i8]>;",
            ),
            FunctionReference(
                "Finds `c`, converted to `i8`, in null-terminated `s` and returns its mutable suffix, or `None` if not found.",
                "pub fn strchr_mut(s: &mut [i8], c: i32) -> Option<&mut [i8]>;",
            ),
        ),
    ),
    LibcGuidance(
        ("strcmp",),
        "strcmp",
        _reference(
            "Compares two null-terminated byte strings.",
            "pub fn strcmp(s1: &[i8], s2: &[i8]) -> i32;",
        ),
    ),
    LibcGuidance(
        ("strcpy",),
        "strcpy",
        _reference(
            "Copies the null-terminated byte string `s2`, including its null byte, into `s1`.",
            "pub fn strcpy<'s>(s1: &'s mut [i8], s2: &[i8]) -> &'s mut [i8];",
        ),
    ),
    LibcGuidance(
        ("strcspn",),
        "strcspn",
        _reference(
            "Returns the length of the initial segment of null-terminated `s1` containing no bytes from null-terminated `s2`.",
            "pub fn strcspn(s1: &[i8], s2: &[i8]) -> usize;",
        ),
    ),
    LibcGuidance(
        ("strdup",),
        "strdup",
        _reference(
            "Duplicates the null-terminated byte string `s`.",
            "pub fn strdup(s: &[i8]) -> Box<[i8]>;",
        ),
    ),
    LibcGuidance(
        ("strlen",),
        "strlen",
        _reference(
            "Returns the number of bytes preceding the first null byte in `s`.",
            "pub fn strlen(s: &[i8]) -> usize;",
        ),
    ),
    LibcGuidance(
        ("strncat",),
        "strncat",
        _reference(
            "Appends at most `n` bytes from `s2` to the null-terminated byte string `s1`.",
            "pub fn strncat<'s>(s1: &'s mut [i8], s2: &[i8], n: usize) -> &'s mut [i8];",
        ),
    ),
    LibcGuidance(
        ("strncmp",),
        "strncmp",
        _reference(
            "Compares at most `n` bytes of two byte strings.",
            "pub fn strncmp(s1: &[i8], s2: &[i8], n: usize) -> i32;",
        ),
    ),
    LibcGuidance(
        ("strncpy",),
        "strncpy",
        _reference(
            "Copies at most `n` bytes from `s2` into `s1`, padding with null bytes when needed.",
            "pub fn strncpy<'s>(s1: &'s mut [i8], s2: &[i8], n: usize) -> &'s mut [i8];",
        ),
    ),
    LibcGuidance(
        ("strndup",),
        "strndup",
        _reference(
            "Duplicates at most `n` bytes from `s` and appends a null byte.",
            "pub fn strndup(s: &[i8], n: usize) -> Box<[i8]>;",
        ),
    ),
    LibcGuidance(
        ("strrchr",),
        "strrchr",
        (
            FunctionReference(
                "Finds the last `c`, converted to `i8`, in null-terminated `s` and returns its suffix, or `None` if not found.",
                "pub fn strrchr(s: &[i8], c: i32) -> Option<&[i8]>;",
            ),
            FunctionReference(
                "Finds the last `c`, converted to `i8`, in null-terminated `s` and returns its mutable suffix, or `None` if not found.",
                "pub fn strrchr_mut(s: &mut [i8], c: i32) -> Option<&mut [i8]>;",
            ),
        ),
    ),
    LibcGuidance(
        ("strspn",),
        "strspn",
        _reference(
            "Returns the length of the initial segment of null-terminated `s1` containing only bytes from null-terminated `s2`.",
            "pub fn strspn(s1: &[i8], s2: &[i8]) -> usize;",
        ),
    ),
    LibcGuidance(
        ("strstr",),
        "strstr",
        (
            FunctionReference(
                "Finds null-terminated `s2` in null-terminated `s1` and returns its suffix, or `None` if not found.",
                "pub fn strstr<'s>(s1: &'s [i8], s2: &[i8]) -> Option<&'s [i8]>;",
            ),
            FunctionReference(
                "Finds null-terminated `s2` in null-terminated `s1` and returns its mutable suffix, or `None` if not found.",
                "pub fn strstr_mut<'s>(s1: &'s mut [i8], s2: &[i8]) -> Option<&'s mut [i8]>;",
            ),
        ),
    ),
    LibcGuidance(
        ("strcasecmp",),
        "strcasecmp",
        _reference(
            "Compares two null-terminated byte strings while ignoring ASCII case.",
            "pub fn strcasecmp(s1: &[i8], s2: &[i8]) -> i32;",
        ),
    ),
    LibcGuidance(
        ("strncasecmp",),
        "strncasecmp",
        _reference(
            "Compares at most `n` bytes of two byte strings while ignoring ASCII case.",
            "pub fn strncasecmp(s1: &[i8], s2: &[i8], n: usize) -> i32;",
        ),
    ),
)

LIBC_FOREIGN_FUNCTION_NAMES = frozenset(
    name for guidance in LIBC_GUIDANCE for name in guidance.foreign_names
)


def render_libc_guidance(foreign_names: Iterable[str]) -> str:
    selected_names = set(foreign_names)
    selected = [
        guidance
        for guidance in LIBC_GUIDANCE
        if selected_names.intersection(guidance.foreign_names)
    ]
    if not selected:
        return ""

    entries = []
    for guidance in selected:
        names = " or ".join(f"`{name}`" for name in guidance.foreign_names)
        replacements = [f"`proctor_libc::{guidance.replacement}`"]
        if len(guidance.references) == 2:
            replacements.append(f"`proctor_libc::{guidance.replacement}_mut`")
        replacement_text = " or ".join(replacements)
        qualifier = ", as appropriate" if len(replacements) == 2 else ""
        references = "\n\n".join(
            f"{reference.documentation}\n\n```rust\n{reference.signature}\n```"
            for reference in guidance.references
        )
        entries.append(
            f"For a listed foreign reference named {names}, replace its call with "
            f"{replacement_text}{qualifier}.\n\n{references}"
        )

    return (
        "The following `proctor_libc` equivalents are available. Their signatures "
        "are reference material only; do not define or import any item for these "
        "calls. Call each function through its fully qualified path.\n\n"
        + "\n\n".join(entries)
    )
