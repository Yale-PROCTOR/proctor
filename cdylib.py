#!/usr/bin/env python3

import json
import sys
import os
import shutil
import toml

from pathlib import Path


def copy_rust_sources(rust_root: Path, crate_dir: Path) -> None:
    generated_root = rust_root / "crates"
    for source_path in rust_root.rglob("*.rs"):
        if generated_root in source_path.parents:
            continue
        relative_path = source_path.relative_to(rust_root)
        destination_path = crate_dir / relative_path
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, destination_path)


if __name__ == "__main__":
    build_root = Path(sys.argv[1])
    source_root = Path(sys.argv[2])
    rust_root = Path(sys.argv[3])

    cmake_reply_root = build_root / ".cmake" / "api" / "v1" / "reply"

    index_path = tuple(cmake_reply_root.glob("index-*.json"))[0]
    with open(index_path, "r") as f:
        index = json.load(f)

    codemodel_filename = index["reply"]["codemodel-v2"]["jsonFile"]
    codemodel_path = cmake_reply_root / codemodel_filename
    with open(codemodel_path, "r") as f:
        codemodel = json.load(f)

    targets = codemodel["configurations"][0]["targets"]

    dependencies: "dict[Path, list[Path]]" = {}
    executable: "Path | None" = None

    for target in targets:
        json_filename = target["jsonFile"]
        json_path = cmake_reply_root / json_filename
        sources_: "list[Path]" = []
        with open(json_path, "r") as f:
            target_data = json.load(f)

            if "link" in target_data:
                for fragment in target_data["link"]["commandFragments"]:
                    if fragment["role"] == "flags":
                        continue
                    if fragment["fragment"].startswith("-"):
                        continue
                    sources_.append(build_root / fragment["fragment"])

            if target_data["type"] == "UTILITY":
                continue
            if target_data["type"] == "OBJECT_LIBRARY":
                assert len(target_data["artifacts"]) == len(target_data["sources"])
                for artifact, source in zip(
                    target_data["artifacts"], target_data["sources"]
                ):
                    sources = sources_.copy()
                    sources.append(source_root / source["path"])
                    artifact_path = build_root / artifact["path"]
                    dependencies[artifact_path] = sources
            else:
                assert len(target_data["artifacts"]) == 1
                artifact = target_data["artifacts"][0]
                artifact_path = build_root / artifact["path"]
                if target_data["type"] == "EXECUTABLE":
                    assert executable is None
                    executable = artifact_path
                sources = sources_
                for source in target_data["sources"]:
                    sources.append(source_root / source["path"])
                dependencies[artifact_path] = sources

    if executable is None:
        cargo_toml_path = rust_root / "Cargo.toml"
        with open(cargo_toml_path, "r") as f:
            cargo_toml = toml.load(f)
        cargo_toml["lib"]["crate-type"] = ["cdylib"]
        with open(cargo_toml_path, "w") as f:
            toml.dump(cargo_toml, f)
        sys.exit(0)

    artifact_worklist: "list[Path]" = [executable]
    artifact_visited: "set[Path]" = set()
    all_sources: "list[str]" = []
    artifacts: "dict[Path, tuple[set[Path], set[Path]]]" = {}

    while artifact_worklist:
        artifact = artifact_worklist.pop()
        if artifact in artifact_visited:
            continue
        artifact_visited.add(artifact)
        source_worklist = dependencies[artifact]
        sources = set()
        libs = set()
        while source_worklist:
            path = source_worklist.pop()
            if path.suffix == ".c":
                sources.add(path)
                all_sources.append(str(path))
            elif path.suffix == ".so":
                libs.add(path)
                artifact_worklist.append(path)
            else:
                deps = dependencies[path]
                source_worklist.extend(deps)
        artifacts[artifact] = (sources, libs)

    common_prefix = Path(os.path.commonprefix(all_sources))

    workspaces: "list[str]" = []
    for artifact, (sources, libs) in artifacts.items():
        if artifact.suffix != ".so":
            continue
        name = artifact.stem[3:]
        workspaces.append(name)
        crate_dir = rust_root / "crates" / name
        crate_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(rust_root / "Cargo.toml", crate_dir / "Cargo.toml")
        copy_rust_sources(rust_root, crate_dir)

        cargo_toml_path = crate_dir / "Cargo.toml"
        with open(cargo_toml_path, "r") as f:
            cargo_toml = toml.load(f)
        if "bin" in cargo_toml:
            del cargo_toml["bin"]
        if "workspace" in cargo_toml:
            del cargo_toml["workspace"]
        cargo_toml["lib"]["crate-type"] = ["cdylib", "lib"]
        cargo_toml["lib"]["name"] = name
        cargo_toml["package"]["name"] = name
        with open(cargo_toml_path, "w") as f:
            toml.dump(cargo_toml, f)

    cargo_toml_path = rust_root / "Cargo.toml"

    with open(cargo_toml_path, "r") as f:
        cargo_toml = toml.load(f)

    if workspaces:
        cargo_toml["workspace"]["members"] = [f"crates/{name}" for name in workspaces]

        for name in workspaces:
            if "dependencies" not in cargo_toml:
                cargo_toml["dependencies"] = {}
            cargo_toml["dependencies"][name] = {"path": f"crates/{name}"}

    cargo_toml["lib"]["crate-type"] = ["lib"]

    with open(cargo_toml_path, "w") as f:
        toml.dump(cargo_toml, f)
