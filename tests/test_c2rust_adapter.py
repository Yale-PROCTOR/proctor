"""Focused tests for the C2Rust adapter."""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path
from types import ModuleType
from typing import Any, BinaryIO

import pytest

REPO = Path(__file__).parent.parent
ADAPTER_MAIN = REPO / "stages" / "c2rust-adapter" / "main.py"


def _load_adapter() -> ModuleType:
    spec = importlib.util.spec_from_file_location("c2rust_adapter_main", ADAPTER_MAIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ADAPTER = _load_adapter()


def _write_project(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.10)\n", encoding="utf-8"
    )


def test_prepare_c_root_keeps_directory_input(tmp_path: Path) -> None:
    project = tmp_path / "input" / "test_case"
    _write_project(project)

    assert ADAPTER.prepare_c_root(tmp_path / "input", tmp_path / "work") == project
    assert not (tmp_path / "work").exists()


@pytest.mark.parametrize("project_prefix", [Path(), Path("bundle"), Path("test_case")])
def test_prepare_c_root_extracts_tar_input_without_extension(
    tmp_path: Path, project_prefix: Path
) -> None:
    source = tmp_path / "source"
    _write_project(source / project_prefix)
    archive_path = tmp_path / "recorded-input"
    with tarfile.open(archive_path, mode="w:gz") as archive:
        for path in source.rglob("*"):
            archive.add(path, arcname=path.relative_to(source), recursive=False)

    root = ADAPTER.prepare_c_root(archive_path, tmp_path / "work")

    assert root == tmp_path / "work" / "c-project" / project_prefix
    assert (root / "CMakeLists.txt").is_file()


def test_prepare_c_root_prefers_tar_root_over_test_case(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_project(source)
    _write_project(source / "test_case")
    archive_path = tmp_path / "input.tar.gz"
    with tarfile.open(archive_path, mode="w:gz") as archive:
        for path in source.rglob("*"):
            archive.add(path, arcname=path.relative_to(source), recursive=False)

    root = ADAPTER.prepare_c_root(archive_path, tmp_path / "work")

    assert root == tmp_path / "work" / "c-project"


def test_prepare_c_root_rejects_unsafe_tar_input(tmp_path: Path) -> None:
    archive_path = tmp_path / "input.tar"
    with tarfile.open(archive_path, mode="w") as archive:
        member = tarfile.TarInfo("../outside")
        member.size = 1
        archive.addfile(member, io.BytesIO(b"x"))

    with pytest.raises(ADAPTER.StageFailure, match="not a readable tar archive"):
        ADAPTER.prepare_c_root(archive_path, tmp_path / "work")
    assert not (tmp_path / "outside").exists()


def test_read_targets_decodes_absolute_library_arguments(tmp_path: Path) -> None:
    reply = tmp_path / ".cmake" / "api" / "v1" / "reply"
    reply.mkdir(parents=True)
    fragments = [
        {"role": "libraries", "fragment": '"/opt/native libs/libtcl.so.8.6"'},
        {"role": "libraries", "fragment": r"/opt/native\ libs/libprobe.a"},
        {"role": "libraries", "fragment": r'"/opt/native\$$/lib\`probe\`.a"'},
        {"role": "libraries", "fragment": "-ldl -lm"},
        {"role": "libraries", "fragment": "libgenerated.a"},
        {"role": "flags", "fragment": "-lignored"},
        {"role": "libraries", "fragment": '"/opt/native libs/libtcl.so.8.6"'},
    ]
    documents = {
        "index-local.json": {"reply": {"codemodel-v2": {"jsonFile": "codemodel.json"}}},
        "codemodel.json": {
            "paths": {"source": str(tmp_path)},
            "configurations": [
                {"targets": [{"id": "driver", "jsonFile": "target.json"}]}
            ],
        },
        "target.json": {
            "id": "driver",
            "name": "driver",
            "type": "EXECUTABLE",
            "link": {"commandFragments": fragments},
        },
    }
    for name, document in documents.items():
        (reply / name).write_text(json.dumps(document))

    (target,) = ADAPTER.read_targets(tmp_path)

    assert target.link_args == [
        "/opt/native libs/libtcl.so.8.6",
        "/opt/native libs/libprobe.a",
        "/opt/native$/lib`probe`.a",
        "-ldl",
        "-lm",
        "/opt/native libs/libtcl.so.8.6",
    ]


def test_restore_c2rust_module_source_with_children(tmp_path: Path) -> None:
    src = tmp_path / "src"
    (src / "type").mkdir(parents=True)
    (src / "type" / "child.rs").touch()
    omitted = src / "type.rs"
    original_source = "pub type child = u32;\n"
    omitted.write_text(original_source, encoding="utf-8")
    occupied = src / "type_c2rust.rs"
    occupied.write_text("pub fn occupied() {}\n", encoding="utf-8")
    (src / "type_c2rust_2").mkdir()
    lib = tmp_path / "lib.rs"
    original = (
        "pub mod src {\n"
        "pub mod r#type {\n"
        "pub mod child;\n"
        "} // mod r#type\n"
        "} // mod src\n"
    )
    lib.write_text(original, encoding="utf-8")

    ADAPTER.restore_c2rust_module_sources(tmp_path)

    assert not omitted.exists()
    assert (src / "type_c2rust_3.rs").read_text(encoding="utf-8") == original_source
    assert occupied.read_text(encoding="utf-8") == "pub fn occupied() {}\n"
    expected = original.replace(
        "pub mod r#type {", "pub mod type_c2rust_3;\npub mod r#type {"
    )
    assert lib.read_text(encoding="utf-8") == expected
    ADAPTER.restore_c2rust_module_sources(tmp_path)
    assert lib.read_text(encoding="utf-8") == expected


@pytest.mark.parametrize(
    "declaration",
    ["pub mod hash;\n", 'pub mod hash {\ninclude!("hash.rs");\n} // mod hash\n'],
)
def test_keep_c2rust_module_source_already_in_tree(
    tmp_path: Path, declaration: str
) -> None:
    (tmp_path / "hash").mkdir()
    source = tmp_path / "hash.rs"
    source.touch()
    lib = tmp_path / "lib.rs"
    lib.write_text(declaration, encoding="utf-8")

    ADAPTER.restore_c2rust_module_sources(tmp_path)

    assert source.is_file()
    assert lib.read_text(encoding="utf-8") == declaration


BROKEN_SOURCE = "pub static BYTE: i8 = 0xe2 as i8;\n"
REPAIRED_SOURCE = "pub static BYTE: i8 = 0xe2u8 as i8;\n"
REPAIR_MODEL = "openrouter/openai/gpt-6.1-sol"


class FakeTranslation:
    def __init__(self, root: Path) -> None:
        source = root / "c"
        _write_project(source)
        self.work = root / "work"
        self.output = root / "out" / "rust"
        self.artifacts = root / "out" / "artifacts"
        self.initial_source = BROKEN_SOURCE
        self.error_origin = "rust"
        self.agent_edits: dict[str, str | None] = {"lib.rs": REPAIRED_SOURCE}
        self.agent_exit = 0
        self.agent_target: Path | None = None
        self.commands: list[list[str]] = []
        self.request: dict[str, Any] | None = None
        self.envelope = {
            "inputs": {"c_project": str(source)},
            "outputs": {
                "rust_project": str(self.output),
                "artifacts_dir": str(self.artifacts),
            },
            "framework": {"workdir": str(self.work)},
            "config": {"repair_model": REPAIR_MODEL},
        }

    def run(
        self,
        command: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        input: bytes | None = None,
        stderr: BinaryIO,
        **_: Any,
    ) -> subprocess.CompletedProcess:
        self.commands.append(command)
        match command[0]:
            case "cmake":
                (self.work / "cmake-build/compile_commands.json").write_text("[]")
            case "c2rust-transpile":
                project = Path(command[command.index("-o") + 1])
                (project / "lib.rs").write_text(self.initial_source)
                (project / "Cargo.toml").write_text(
                    '[lib]\npath = "lib.rs"\ncrate-type = ["rlib"]\n'
                )
                (project / "build.rs").write_text("fn main() {\n}\n")
            case "cargo":
                assert cwd is not None and env is not None
                if "repair_model" in self.envelope["config"]:
                    assert Path(env["CARGO_TARGET_DIR"]).parent == self.work
                    assert env["CARGO_BUILD_BUILD_DIR"] == env["CARGO_TARGET_DIR"]
                if (cwd / "lib.rs").read_text() == BROKEN_SOURCE:
                    if "--message-format=json" not in command:
                        stderr.write(b"error: literal out of range for `i8`\n")
                        return subprocess.CompletedProcess(command, 101)
                    file_name = "build.rs" if self.error_origin == "build" else "lib.rs"
                    target_dir = (
                        cwd.parent if self.error_origin == "dependency" else cwd
                    )
                    event = {
                        "reason": "compiler-message",
                        "target": {
                            "src_path": str(target_dir / file_name),
                            "kind": [
                                "custom-build" if file_name == "build.rs" else "lib"
                            ],
                        },
                        "message": {
                            "level": "error",
                            "rendered": "error: literal out of range for `i8`\n",
                            "spans": []
                            if self.error_origin == "linker"
                            else [{"file_name": file_name, "is_primary": True}],
                        },
                    }
                    return subprocess.CompletedProcess(command, 101, json.dumps(event))
                return subprocess.CompletedProcess(command, 0, "")
            case "uv":
                assert cwd is not None and input is not None and env is not None
                self.agent_target = Path(env["CARGO_TARGET_DIR"])
                assert self.agent_target != self.work / "cargo-target"
                assert env["CARGO_BUILD_BUILD_DIR"] == str(self.agent_target)
                self.request = json.loads(input)
                for path, contents in self.agent_edits.items():
                    entry = cwd / path
                    if entry.is_dir():
                        entry.chmod(entry.stat().st_mode ^ 0o010)
                    elif contents is None:
                        entry.unlink()
                    else:
                        entry.write_text(contents)
                return subprocess.CompletedProcess(command, self.agent_exit)
            case _:
                raise AssertionError(command)
        return subprocess.CompletedProcess(command, 0)


@pytest.fixture
def translation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeTranslation:
    case = FakeTranslation(tmp_path)
    monkeypatch.delenv("PROCTOR_STAGE_DEADLINE_MONOTONIC", raising=False)
    monkeypatch.setattr(ADAPTER.subprocess, "run", case.run)
    monkeypatch.setattr(ADAPTER, "pick_generator", lambda: "Ninja")
    monkeypatch.setattr(
        ADAPTER, "resolve_transpiler", lambda log: (Path("c2rust-transpile"), {})
    )
    monkeypatch.setattr(
        ADAPTER,
        "read_targets",
        lambda directory: [ADAPTER.Artifact("example", "EXECUTABLE", [], [])],
    )
    return case


def test_stage_preserves_order_and_escapes_native_link_arguments(
    translation: FakeTranslation, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = '/opt/native libs/lib"probe"{v}\x08.a'
    monkeypatch.setattr(
        ADAPTER,
        "read_targets",
        lambda directory: [
            ADAPTER.Artifact("example", "EXECUTABLE", [], [archive, "-lm", archive]),
            ADAPTER.Artifact("shared", "SHARED_LIBRARY", [], ["-lm", "-ldl"]),
            ADAPTER.Artifact("shared_copy", "SHARED_LIBRARY", [], ["-lm", "-ldl"]),
        ],
    )
    translation.envelope["config"]["check_build"] = False

    output = ADAPTER.run_stage(translation.envelope)

    archive_directive = r'    println!("cargo:rustc-link-arg=/opt/native libs/lib\x22probe\x22{{v}}\x08.a");'
    assert output["status"] == "success"
    assert (translation.output / "build.rs").read_text().splitlines() == [
        "fn main() {",
        archive_directive,
        '    println!("cargo:rustc-link-arg=-lm");',
        archive_directive,
        '    println!("cargo:rustc-link-arg=-lm");',
        '    println!("cargo:rustc-link-arg=-ldl");',
        "}",
    ]


def test_cache_variables_override_preset(
    translation: FakeTranslation, tmp_path: Path
) -> None:
    (tmp_path / "c" / "CMakePresets.json").touch()
    variables = {"DEBUG": False, "ASSERTIONS": True, "SIZE": 64, "MODE": "fast"}
    translation.envelope["config"].update(
        check_build=False, preset="test", cmake_cache_variables=variables
    )

    output = ADAPTER.run_stage(translation.envelope)

    assert translation.commands[0][-6:] == [
        "--preset",
        "test",
        "-DDEBUG=OFF",
        "-DASSERTIONS=ON",
        "-DSIZE=64",
        "-DMODE=fast",
    ]
    assert output["config_used"]["cmake_cache_variables"] == variables


@pytest.mark.parametrize(
    "variables",
    [["DEBUG"], {"DEBUG": 1.5}, {"DEBUG=ON": False}],
)
def test_rejects_invalid_cache_variables(
    translation: FakeTranslation, variables: object
) -> None:
    translation.envelope["config"]["cmake_cache_variables"] = variables

    with pytest.raises(
        ADAPTER.StageFailure, match="cache variable|cmake_cache_variables"
    ):
        ADAPTER.run_stage(translation.envelope)
    assert translation.commands == []


@pytest.mark.parametrize("stage_time_remaining", [None, 60])
def test_repairs_once_then_builds_before_publishing(
    translation: FakeTranslation,
    monkeypatch: pytest.MonkeyPatch,
    stage_time_remaining: float | None,
) -> None:
    if stage_time_remaining is not None:
        monkeypatch.setenv(
            "PROCTOR_STAGE_DEADLINE_MONOTONIC",
            str(ADAPTER.time.monotonic() + stage_time_remaining),
        )

    output = ADAPTER.run_stage(translation.envelope)

    assert [command[0] for command in translation.commands] == [
        "cmake",
        "c2rust-transpile",
        "cargo",
        "uv",
        "cargo",
    ]
    assert translation.commands[-1] == ["cargo", "build"]
    assert translation.request is not None
    assert translation.request["agent"] == "opencode"
    assert translation.request["model"] == REPAIR_MODEL
    if stage_time_remaining is None:
        assert translation.request["timeout_s"] == ADAPTER.REPAIR_TIMEOUT_S
    else:
        assert 0 < translation.request["timeout_s"] < stage_time_remaining
    for expected in (
        "smallest edits",
        "program behavior",
        "C-facing ABI",
        "out of range",
    ):
        assert expected in translation.request["prompt"]
    assert output["metrics"]["compilation_repair_calls"] == 1
    assert (translation.output / "lib.rs").read_text() == REPAIRED_SOURCE
    assert (
        translation.agent_target is not None and not translation.agent_target.exists()
    )
    assert sorted(path.name for path in translation.artifacts.iterdir()) == [
        "c2rust.log"
    ]


@pytest.mark.parametrize("error_origin", ["linker", "build", "dependency"])
def test_repairs_only_generated_rust_diagnostics(
    translation: FakeTranslation, error_origin: str
) -> None:
    translation.error_origin = error_origin

    with pytest.raises(ADAPTER.StageFailure, match="cargo build"):
        ADAPTER.run_stage(translation.envelope)

    assert translation.request is None
    assert not translation.output.exists()


@pytest.mark.parametrize(
    ("path", "contents"),
    [
        ("build.rs", "changed\n"),
        ("Cargo.toml", "changed\n"),
        ("config.toml", "changed\n"),
        ("other.rs", "added\n"),
        ("lib.rs", None),
        (".", None),
    ],
)
def test_rejects_changes_outside_existing_generated_sources(
    translation: FakeTranslation, path: str, contents: str | None
) -> None:
    translation.agent_edits[path] = contents

    with pytest.raises(ADAPTER.StageFailure, match="changed protected project paths"):
        ADAPTER.run_stage(translation.envelope)

    assert translation.commands[-1][0] == "uv"
    assert not translation.output.exists()


@pytest.mark.parametrize("agent_exit", [0, 124])
def test_failed_repair_never_publishes(
    translation: FakeTranslation, agent_exit: int
) -> None:
    translation.agent_edits = {}
    translation.agent_exit = agent_exit

    with pytest.raises(ADAPTER.StageFailure):
        ADAPTER.run_stage(translation.envelope)

    assert sum(command[0] == "uv" for command in translation.commands) == 1
    assert not translation.output.exists()


@pytest.mark.parametrize(
    ("config", "initial_source", "success"),
    [
        ({}, BROKEN_SOURCE, False),
        ({"repair_model": REPAIR_MODEL}, REPAIRED_SOURCE, True),
        ({"repair_model": REPAIR_MODEL, "check_build": False}, BROKEN_SOURCE, True),
    ],
)
def test_agent_runs_only_on_enabled_failing_build(
    translation: FakeTranslation, config: dict, initial_source: str, success: bool
) -> None:
    translation.envelope["config"] = config
    translation.initial_source = initial_source

    if success:
        output = ADAPTER.run_stage(translation.envelope)
        assert output["metrics"]["compilation_repair_calls"] == 0
        assert translation.output.is_dir()
    else:
        with pytest.raises(ADAPTER.StageFailure, match="cargo build"):
            ADAPTER.run_stage(translation.envelope)
        assert not translation.output.exists()

    assert translation.request is None
    if not config.get("check_build", True):
        assert all(command[0] != "cargo" for command in translation.commands)
