# Example output

This page walks through one real report so you can read the JSON without
opening the source. See `PROVENANCE.md` for why each number is measured the
way it is.

The example is the `binary_heap_lib` case from the B03 corpus. *Before* is the
CRAT output; *after* is the same crate once abstraction recovery has replaced
the hand-written heap with `std::collections::BinaryHeap`.

```
diff_metrics --before stages/01-crat/out/rust \
             --after  stages/02-abstraction_recovery/out/rust
```

```json
{
  "schema_version": 3,
  "files_changed": 1,
  "lines_added": 47,
  "lines_removed": 103,
  "touched_files": [
    "src/binary_heap.rs"
  ],
  "files": {
    "src/binary_heap.rs": {
      "status": "modified",
      "lines_added": 47,
      "lines_removed": 103,
      "rustfmt_ok": true
    }
  },
  "targets": {
    "before": {
      "targets": [
        {
          "kind": "lib",
          "name": "driver",
          "root": "lib.rs"
        }
      ],
      "unreachable_files": []
    },
    "after": {
      "targets": [
        {
          "kind": "lib",
          "name": "driver",
          "root": "lib.rs"
        }
      ],
      "unreachable_files": []
    }
  },
  "abi": {
    "ok": false,
    "api_restricted": false,
    "c_type_model": {
      "target": "x86_64-linux",
      "c_char": "i8",
      "c_long": "i64",
      "c_ulong": "u64",
      "usize": "u64",
      "isize": "i64"
    },
    "removed": [],
    "removed_bin_internal": [],
    "added": [],
    "changed": [],
    "types_changed": [
      {
        "name": "_BinaryHeap",
        "kind": "layout",
        "by_value": false,
        "public_layout": false,
        "breaking": true,
        "before": "#[repr(C)]struct {heap_type: BinaryHeapType, values: *mut *mut c_void, num_values: u32, alloced_size: u32, compare_func: Option<extern \"C\" fn(*mut c_void, *mut c_void) -> i32>}",
        "after": "struct {heap_type: BinaryHeapType, compare_func: Option<extern \"C\" fn(*mut c_void, *mut c_void) -> i32>, values: BinaryHeap<HeapEntry>}"
      }
    ],
    "duplicate_symbols": [],
    "unparsed_files": {
      "before": [],
      "after": []
    }
  }
}
```

The report has three parts: what changed in the code, which files are
compiled, and whether the C interface still holds.

## Code changes

`files_changed`, `lines_added`, `lines_removed` and `touched_files` are totals
over the crate; `files` gives the same numbers per changed file.

Both sides of a file are run through rustfmt before they are compared, so
reformatting is not counted as change. Lines are compared as a set, so moving
a line is not counted either.

`status` is `modified`, `added`, `removed`, or `reordered`. A reordered file
has the same lines in a different order: it counts as changed, with 0 lines
added and removed. `rustfmt_ok: false` means rustfmt failed on one side and
the raw text was compared instead, so the counts may include formatting noise.

Here the stage edited one file and removed more than it added. A stage that
edits files outside the structure it was working on shows up in
`touched_files`.

`schema_version` changes whenever the shape of this JSON changes.

## Compiled files

`targets` lists what `Cargo.toml` builds on each side: each library or binary
target and its root file. The ABI check uses it to tell library exports, which
are the C API, from binary exports, which are not.

`unreachable_files` are `.rs` files no target compiles, such as stale copies.
Their symbols are left out of the ABI check.

## C interface

`abi.ok` is the verdict. It is true only when no exported library function was
removed or changed signature, no type change is breaking, no symbol is
exported twice, and every compiled file parsed.

| field | meaning |
|---|---|
| `api_restricted` | `--api` was given, so only the listed symbols and types count as API. |
| `c_type_model` | The C type sizes assumed, so `c_long` and `i64` compare equal. |
| `removed` | Exported library functions that are gone. Always breaking. |
| `removed_bin_internal` | Functions a binary target stopped exporting. Not breaking: a binary has no C callers. |
| `added` | New exports. Reported, not breaking. |
| `changed` | Same name, different signature, with `before` and `after`. |
| `types_changed` | Types reachable from an exported signature whose definition changed. |
| `duplicate_symbols` | A `#[no_mangle]` name defined twice in the library, which will not link. |
| `unparsed_files` | Compiled files that did not parse. Their exports are unknown, so `ok` is false rather than passing silently. |

Each `types_changed` entry has:

- `kind`: `layout` when the definition changed; `int_to_enum` when an integer
  became a fieldless `#[repr]` enum (or back). That is the same in the C ABI,
  but Rust then assumes C only passes listed values, which the tests check,
  not this tool.
- `by_value`: the type is passed by value from an exported signature, so C
  depends on its layout.
- `public_layout`: the C headers define its fields (`--api` lists it as a
  `type`).
- `breaking`: the layout change is by value, public, or there is no `--api` to
  tell.

## Why `ok` is false here, and what `--api` changes

The stage turned `_BinaryHeap` from a `#[repr(C)]` struct holding a raw array
into a Rust struct holding a `BinaryHeap<HeapEntry>`. Without `--api` the tool
cannot know whether C reads that struct's fields, so it assumes it might and
marks the change breaking.

In this case C only ever holds a `BinaryHeap *`, so the struct is opaque and
changing its fields is the intended result. Passing the case's public API
fixes the verdict. The file has one entry per line: `symbol NAME` or a bare
`NAME` for a function or global, and `type NAME` for a type whose fields the
headers define.

```
binary_heap_free
binary_heap_insert
binary_heap_new
binary_heap_num_entries
binary_heap_pop
```

With it, the `abi` section reports `"ok": true`, `"api_restricted": true`, and
the `_BinaryHeap` entry has `"breaking": false`. Everything else in the report
is unchanged.

The pipeline builds this list from the C project's headers (`public_api` in
`proctor/testing/diff_eval.py`). When it cannot read the API off the headers,
for example when names are built by macros, it omits `--api` and the check
falls back to treating every export as API.
