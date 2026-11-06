"""diff_eval: the diff_metrics wrapper (report shape, ABI verdict, the
subprocess contract) and the C-header public-API reader — tool stubbed."""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from proctor.testing import diff_eval
from proctor.testing.diff_eval import DiffEvalError, DiffReport, PublicApi, public_api

_SAMPLE = {
    "schema_version": 3,
    "files_changed": 1,
    "lines_added": 12,
    "lines_removed": 37,
    "files": {
        "src/lib.rs": {"status": "modified", "lines_added": 12, "lines_removed": 37},
    },
    "abi": {
        "ok": True,
        "api_restricted": True,
        "removed": [],
        "added": ["new_fn"],
        "changed": [],
        "removed_bin_internal": ["demo"],
        "types_changed": [
            {"name": "u32 -> cc_stat", "kind": "int_to_enum", "breaking": False}
        ],
    },
}


def test_report_takes_the_tools_abi_verdict() -> None:
    r = DiffReport.from_json(_SAMPLE)
    assert (r.files_changed, r.lines_added, r.lines_removed) == (1, 12, 37)
    assert r.abi_ok and r.api_restricted and r.abi_breaking_types == []
    d = r.to_dict()
    assert list(d["per_file"]) == ["src/lib.rs"]
    assert d["abi_removed_bin_internal"] == ["demo"]
    assert d["abi_types_changed"][0]["kind"] == "int_to_enum"


def test_breaking_changes_are_summarized() -> None:
    broke = dict(
        _SAMPLE,
        abi={
            "ok": False,
            "removed": ["f"],
            "changed": [{"name": "g"}],
            "types_changed": [{"name": "Foo", "breaking": True}],
        },
    )
    r = DiffReport.from_json(broke)
    assert not r.abi_ok and r.abi_changed == ["g"] and r.abi_breaking_types == ["Foo"]
    assert "1 removed, 1 changed, 1 layout(s) changed" in r.summary()


def test_missing_verdict_is_not_ok() -> None:
    assert not DiffReport.from_json({"files_changed": 0}).abi_ok


def test_measure_diff_passes_api_file(tmp_path: Path, monkeypatch: Any) -> None:
    seen: dict[str, Any] = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        if "--api" in cmd:
            seen["api"] = Path(cmd[cmd.index("--api") + 1]).read_text()
        return subprocess.CompletedProcess(cmd, 0, json.dumps(_SAMPLE), "")

    monkeypatch.setattr(diff_eval, "ensure_built", lambda: tmp_path / "bin")
    monkeypatch.setattr(diff_eval.subprocess, "run", fake_run)
    api = PublicApi(frozenset({"circle_collide"}), frozenset({"c2Circle"}))
    diff_eval.measure_diff(tmp_path / "a", tmp_path / "b", api=api)
    assert seen["api"] == "symbol circle_collide\ntype c2Circle\n"
    assert seen["cmd"][1:5] == [
        "--before",
        str(tmp_path / "a"),
        "--after",
        str(tmp_path / "b"),
    ]


def test_measure_diff_tool_failure_raises(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(diff_eval, "ensure_built", lambda: tmp_path / "bin")
    monkeypatch.setattr(
        diff_eval.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 2, "", "boom"),
    )
    with pytest.raises(DiffEvalError):
        diff_eval.measure_diff(tmp_path, tmp_path)


def _header(root: Path, text: str, name: str = "lib.h") -> Path:
    inc = root / "include"
    inc.mkdir(parents=True, exist_ok=True)
    (inc / name).write_text(text, encoding="utf-8")
    return root


def test_public_api_reads_functions_globals_and_defined_types(tmp_path: Path) -> None:
    c = _header(
        tmp_path,
        """
        #ifndef LIB_H
        #define LIB_H(x) x          /* macro, not a function */
        // int commented_out(void);
        typedef struct _SListEntry SListEntry;      /* opaque: no body */
        typedef struct { int x; struct { int y; } inner; } Point;
        struct cc_array_conf { size_t capacity; };
        enum cc_stat { CC_OK, CC_ERR };
        extern int slist_debug;
        extern const char *names[4];
        typedef int (*cmp_fn)(const void *, const void *);
        int slist_append(SListEntry **list, void *data);
        void slist_sort(SListEntry **list, int (*compare)(void *, void *));
        size_t
        cc_array_size(CC_Array *ar);
        #endif
        """,
    )
    api = public_api(c)
    assert api is not None
    assert api.symbols == {
        "cc_array_size",
        "slist_append",
        "slist_sort",
        "slist_debug",
        "names",
    }
    assert api.types == {"Point", "cc_array_conf", "cc_stat"}  # not SListEntry


def test_public_api_gives_up_on_token_pasting(tmp_path: Path) -> None:
    c = _header(tmp_path, "#define DECLARE(T) T* T##_create(void);\nDECLARE(int)\n")
    assert public_api(c) is None  # macro-generated API can't be read off source


def test_public_api_none_without_headers(tmp_path: Path) -> None:
    assert public_api(tmp_path) is None


def test_unreadable_header_raises(tmp_path: Path) -> None:
    c = _header(tmp_path, "int f(void);\n")
    (c / "include" / "broken.h").symlink_to(tmp_path / "missing.h")
    with pytest.raises(OSError):
        public_api(c)
