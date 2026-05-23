#!/usr/bin/env python3
import sys
from pathlib import Path
from collections import Counter
from dataclasses import dataclass


@dataclass
class Counters:
    b01: Counter[str]
    p01: Counter[str]


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} [path]")
        sys.exit(1)

    base_path = Path(sys.argv[1]).resolve()

    dir_counter: "dict[str, Counters]" = {}
    all_strings: "set[str]" = set()

    for dir_path in base_path.iterdir():
        b01_counts: Counter[str] = Counter()
        p01_counts: Counter[str] = Counter()

        for file_path in dir_path.glob("*.txt"):
            lines = [
                rename(line.strip())
                for line in file_path.read_text().splitlines()
                if line.strip()
            ]
            all_strings.update(lines)
            if "B01" in file_path.name:
                b01_counts.update(lines)
            elif "P00" in file_path.name:
                b01_counts.update(lines)
            elif "P01" in file_path.name:
                p01_counts.update(lines)
            else:
                raise ValueError(f"Unknown file name: {file_path}")

        counters = Counters(
            b01=b01_counts,
            p01=p01_counts,
        )
        dir_counter[dir_path.name] = counters

    counters = ["b01", "p01"]
    sorted_dirs = [
        "PUBLIC-Test-Corpus-base",
        "PUBLIC-Test-Corpus-extern",
        "PUBLIC-Test-Corpus-preprocess",
        "PUBLIC-Test-Corpus-outparam",
        "PUBLIC-Test-Corpus-punning",
        "PUBLIC-Test-Corpus-pointer",
        "PUBLIC-Test-Corpus-io",
        "PUBLIC-Test-Corpus-libc",
        "PUBLIC-Test-Corpus-static",
    ]
    sorted_strings = [
        "intra",
        "transmute",
        "union",
        "deref",
        "offset",
        "std",
        "alloc",
        "lib",
        "static",
        "fnptr",
    ]

    for counter_name in counters:
        print(counter_name)
        div = 1
        row = ["name"] + sorted_strings
        print("\t".join(row))
        for dir_name in sorted_dirs:
            counter: Counter[str] = getattr(dir_counter[dir_name], counter_name)
            row = [dir_name] + [
                f"{counter.get(s, 0) / div:.1f}" for s in sorted_strings
            ]
            print("\t".join(row))


def rename(s: str) -> str:
    if s == "DerefOfRawPointer":
        return "deref"
    if s == "UseOfMutableStatic":
        return "static"
    if s == "AccessToUnionField":
        return "union"
    if s == "CallToUnsafeFunction(None)":
        return "fnptr"
    if s == "transmute":
        return "transmute"
    if s == "offset":
        return "offset"
    if s == "offset_from":
        return "offset"
    if s == "add":
        return "add"
    if s == "swap":
        return "swap"
    if s in cryptos:
        return "lib"
    if s in allocs:
        return "alloc"
    if s in maths:
        return "lib"
    if s in libcs:
        return "lib"
    if s in stds:
        return "std"
    if s in arrays:
        return "lib"
    if s in ios:
        return "lib"
    return "intra"


cryptos = {
    "ERR_print_errors_fp",
    "EVP_CIPHER_CTX_free",
    "EVP_CIPHER_CTX_new",
    "EVP_EncryptInit_ex",
    "EVP_EncryptUpdate",
    "EVP_aes_256_ecb",
}
allocs = {
    "calloc",
    "free",
    "malloc",
    "realloc",
}
maths = {
    "div",
    "expf",
    "fabs",
    "fabsf",
    "floorf",
    "fmodf",
    "pow",
    "sqrtf",
    "abs",
    "atan2",
    "cos",
    "sin",
    "sqrt",
}
libcs = {
    "__ctype_b_loc",
    "abort",
    "tolower",
    "toupper",
    "__errno_location",
    "rand",
    "srand",
    "setlocale",
    "__assert_fail",
    "ctime",
    "difftime",
    "time",
    "exit",
    "localeconv",
}
stds = {
    "as_mut",
    "as_ref",
    "from_ptr",
    "from_raw_parts",
    "from_raw_parts_mut",
}
arrays = {
    "atof",
    "atoi",
    "memcmp",
    "memcpy",
    "memmove",
    "memset",
    "memchr",
    "sscanf",
    "snprintf",
    "sprintf",
    "strchr",
    "strcspn",
    "strlen",
    "strncpy",
    "strtod",
    "strtol",
    "strtoul",
    "getenv",
    "strcat",
    "strcmp",
    "strcpy",
    "strdup",
    "strerror",
    "strncat",
    "strncmp",
    "strrchr",
    "strstr",
    "strtok",
    "strtok_r",
    "regcomp",
    "regexec",
    "regfree",
}
ios = {
    "fgets",
    "fprintf",
    "fputs",
    "fread",
    "fscanf",
    "getchar",
    "printf",
    "puts",
    "scanf",
    "clearerr",
    "fclose",
    "feof",
    "ferror",
    "fileno",
    "fopen",
    "fputc",
    "fseek",
    "fstat",
    "ftell",
    "perror",
    "select",
}

if __name__ == "__main__":
    main()
