from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from proctor.llm.types import AuthError, Response, Usage
from proctor.usage.tracker import read_usage
from stage import run_stage
from tooling import CommandResult, CratTools, StageFailure
from wrapper_bounds import infer_wrapper_bounds
from tests.test_local_transformation import FakeTools, STAGE_DIR, stage_input


INTERNALS = {
    "first": "unsafe fn first_internal(p: &[i32], n: usize) -> i32 { p[n - 1] }",
    "second": "unsafe fn second_internal(a: &[u8], b: &mut [u8], n: usize) { b[..n].copy_from_slice(&a[..n]); }",
}
WRAPPERS = {
    "first": """unsafe fn first(p: *const i32, n: usize) -> i32 {
    first_internal(if p.is_null() { &[] } else { std::slice::from_raw_parts(p, 1_000_000) }, n)
}""",
    "second": """unsafe fn second(a: *const u8, b: *mut u8, n: usize) {
    second_internal(if a.is_null() { &[] } else { std::slice::from_raw_parts(a, 1_000_000) },
                    if b.is_null() { &mut [] } else { std::slice::from_raw_parts_mut(b, 1_000_000) }, n)
}""",
}
SOURCE = (
    "// λ stays unchanged\n"
    + "\n\n".join(
        code for name in INTERNALS for code in (INTERNALS[name], WRAPPERS[name])
    )
    + "\n"
)


class SyntaxTools(FakeTools):
    """A fake compiler protocol; Rust syntax matching has its own in-memory tests."""

    def __init__(self):
        super().__init__(normalized=SOURCE)
        self.syntax_requests = []

    def wrapper_bounds(self, request, output):
        value = json.loads(request.read_text())
        self.syntax_requests.append(value)
        source = value["source"]
        wrappers = []
        for name, internal in INTERNALS.items():
            marker = f"unsafe fn {name}("
            if marker not in source:
                continue
            start = source.index(marker)
            end = source.index("\n}", start) + 2
            wrapper = source[start:end]
            positions = []
            cursor = 0
            while (position := wrapper.find("1_000_000", cursor)) >= 0:
                lo = len(source[: start + position].encode())
                positions.append({"start": lo, "end": lo + 9, "text": "1_000_000"})
                cursor = position + 9
            if positions:
                wrappers.append(
                    {
                        "path": name,
                        "wrapper": wrapper,
                        "internal": internal,
                        "sites": positions,
                    }
                )
        candidate = None
        if "expressions" in value:
            target = next(item for item in wrappers if item["path"] == value["wrapper"])
            if any(
                expression == "invalid expression"
                for expression in value["expressions"]
            ):
                raise StageFailure("not a single Rust expression")
            candidate = source.encode()
            for site, expression in reversed(
                list(zip(target["sites"], value["expressions"], strict=True))
            ):
                if expression != site["text"]:
                    candidate = (
                        candidate[: site["start"]]
                        + f"({expression}\n)".encode()
                        + candidate[site["end"] :]
                    )
            candidate = candidate.decode()
        result = {"wrappers": wrappers, "candidate": candidate}
        output.write_text(json.dumps(result))
        return result


def response(text, finish_reason="stop"):
    return Response(text, finish_reason, "replay", "cheap-model", 0.1, Usage(20, 5, 10))


def project(tmp_path, source=SOURCE):
    path = tmp_path / "project"
    path.mkdir()
    (path / "lib.rs").write_text(source)
    return path


def infer(tmp_path, path, complete, build=None, tools=None, dump=False):
    return infer_wrapper_bounds(
        path,
        workdir=tmp_path / "work",
        stage_dir=STAGE_DIR,
        tools=tools or SyntaxTools(),
        complete=complete,
        build=build or (lambda _: CommandResult(0)),
        run_id="bound-run",
        dump_exchanges=dump,
    )


def test_no_wrappers_avoids_llm_and_build_even_with_unrelated_literal_patterns(
    tmp_path,
):
    path = project(tmp_path, 'const TEXT: &str = "from_raw_parts(p, 1_000_000)";\n')

    def unexpected(*args):
        pytest.fail("no LLM or build is needed")

    report = infer(tmp_path, path, unexpected, build=unexpected)
    assert report == {"status": "no_wrappers", "wrappers": [], "errors": []}


def test_one_request_per_wrapper_uses_only_its_two_functions_and_ordered_bounds(
    tmp_path,
):
    path = project(tmp_path)
    requests = []
    builds = []

    def complete(request):
        requests.append(request)
        text = request.messages[0].content
        name = "first" if len(requests) == 1 else "second"
        other = "second" if name == "first" else "first"
        assert WRAPPERS[name] in text and INTERNALS[name] in text
        assert WRAPPERS[other] not in text and INTERNALS[other] not in text
        assert "fn ignored_helper" not in text
        assert request.model is None
        assert request.metadata.prompt_id == "wrapper_bound_inference"
        assert request.metadata.prompt_version == 1
        assert len(request.metadata.prompt_hash) == 64
        return response('["n as usize"]' if name == "first" else '["n", "n + 1"]')

    def build(project):
        builds.append((project / "lib.rs").read_text())
        return CommandResult(0)

    report = infer(tmp_path, path, complete, build=build, dump=True)
    assert len(requests) == len(builds) == 2
    expected = SOURCE
    for expression in ("n as usize", "n", "n + 1"):
        expected = expected.replace("1_000_000", f"({expression}\n)", 1)
    assert (path / "lib.rs").read_text() == expected
    assert [item["status"] for item in report["wrappers"]] == ["applied", "applied"]
    assert report["wrappers"][1]["proposed_bounds"] == ["n", "n + 1"]
    assert (tmp_path / "work/wrapper-bounds/wrappers/000000/prompt.md").is_file()
    assert (tmp_path / "work/wrapper-bounds/wrappers/000001/response.txt").is_file()


def test_later_compilation_failure_restores_only_that_wrapper(tmp_path):
    path = project(tmp_path)
    calls = []
    builds = []

    def complete(request):
        calls.append(request)
        return response('["n"]' if len(calls) == 1 else '["n + 1", "n + 2"]')

    def build(project):
        builds.append((project / "lib.rs").read_text())
        return (
            CommandResult(0) if len(builds) == 1 else CommandResult(1, "", "bad bound")
        )

    report = infer(tmp_path, path, complete, build=build)
    assert (path / "lib.rs").read_text() == SOURCE.replace("1_000_000", "(n\n)", 1)
    assert [item["status"] for item in report["wrappers"]] == ["applied", "reverted"]
    assert report["wrappers"][1]["build_diagnostics"] == "bad bound"


@pytest.mark.parametrize(
    "answer",
    [
        "not json",
        '```json\n["n"]\n```',
        "null",
        "{}",
        "[]",
        '["n", "n"]',
        "[1]",
        '[""]',
        '["   "]',
        '["invalid expression"]',
    ],
)
def test_invalid_reply_keeps_bounds_and_does_not_retry_or_build(tmp_path, answer):
    path = project(tmp_path, INTERNALS["first"] + "\n" + WRAPPERS["first"])
    before = (path / "lib.rs").read_bytes()
    calls = []

    def complete(request):
        calls.append(request)
        return response(answer)

    def build(_):
        pytest.fail("invalid reply must not be compiled")

    report = infer(tmp_path, path, complete, build=build)
    assert len(calls) == 1
    assert report["wrappers"][0]["status"] == "unchanged"
    assert (path / "lib.rs").read_bytes() == before


@pytest.mark.parametrize("finish", ["length", "refusal", "error"])
def test_incomplete_response_is_rejected_even_if_json_parses(tmp_path, finish):
    path = project(tmp_path)
    report = infer(
        tmp_path,
        path,
        lambda _: response('["n"]', finish),
        build=lambda _: pytest.fail("no build"),
    )
    assert all(entry["reason"] == "incomplete_response" for entry in report["wrappers"])
    assert (path / "lib.rs").read_text() == SOURCE


def test_unchanged_bounds_skip_replacement_and_build(tmp_path):
    path = project(tmp_path)
    tools = SyntaxTools()
    answers = iter(['["1_000_000"]', '["1_000_000", "1_000_000"]'])
    report = infer(
        tmp_path,
        path,
        lambda _: response(next(answers)),
        tools=tools,
        build=lambda _: pytest.fail("unchanged bounds need no build"),
    )
    assert all(
        item["reason"] == "original_bounds_returned" for item in report["wrappers"]
    )
    assert not any("expressions" in request for request in tools.syntax_requests)
    assert (path / "lib.rs").read_text() == SOURCE


@pytest.mark.parametrize("failure", ["exception", "source_rewrite"])
def test_failed_or_source_rewriting_build_reverts_and_continues(tmp_path, failure):
    path = project(tmp_path)
    answers = iter(['["n"]', '["n", "n"]'])

    def build(project):
        if failure == "exception":
            raise OSError("build unavailable")
        (project / "lib.rs").write_text("rewritten by build\n")
        return CommandResult(0)

    report = infer(tmp_path, path, lambda _: response(next(answers)), build=build)
    assert [item["status"] for item in report["wrappers"]] == ["reverted", "reverted"]
    assert (path / "lib.rs").read_text() == SOURCE


def test_scanning_ignores_test_harnesses_targets_build_scripts_and_symlink_aliases(
    tmp_path,
):
    path = project(tmp_path, "fn main() {}\n")
    for name in (
        "tests/test.rs",
        "runner/src/main.rs",
        "target/other.rs",
        ".hidden/x.rs",
        "build.rs",
    ):
        file = path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(SOURCE)
    (path / "translated_rust").symlink_to(".", target_is_directory=True)
    report = infer(
        tmp_path, path, lambda _: pytest.fail("no wrappers in crate sources")
    )
    assert report["status"] == "no_wrappers"
    assert not (tmp_path / "work").exists()


def test_unreadable_incidental_source_is_reported_without_failing_inference(tmp_path):
    path = project(tmp_path, "fn main() {}\n")
    (path / "incidental.rs").write_bytes(b"\xff")
    report = infer(tmp_path, path, lambda _: pytest.fail("no eligible wrappers"))
    assert report["status"] == "detection_failed"
    assert report["errors"][0]["file"] == "incidental.rs"
    assert (path / "lib.rs").read_text() == "fn main() {}\n"


@pytest.mark.parametrize("manifest", [False, True])
@pytest.mark.parametrize("provider_error", [False, True])
def test_stage_uses_default_llm_and_shared_usage_before_final_validation(
    tmp_path, manifest, provider_error
):
    value = stage_input(
        tmp_path,
        config={"validation_repair_model": "strong-agent-model"},
        llm={
            "provider": "replay",
            "model": "cheap-model",
            "context_overflow": "truncate_oldest",
        },
    )
    c_project = tmp_path / "c-reference-input"
    c_project.mkdir()
    (c_project / "main.c").write_text("secret C code should not enter prompt\n")
    value = replace(value, inputs=replace(value.inputs, c_project=c_project))
    if manifest:
        (value.inputs.rust_project / "testinfra.toml").write_text('strategy="cando2"\n')
    tools = SyntaxTools()
    requests = []
    settings_used = []

    class Client:
        def complete(self, request):
            requests.append(request)
            assert "secret C code" not in request.messages[0].content
            if len(requests) == 1 and provider_error:
                raise AuthError("test failure")
            return response('["n"]' if len(requests) == 1 else '["n", "n"]')

    def factory(settings, tracker):
        settings_used.append(settings)
        return Client()

    def runner(command, **kwargs):
        assert len(requests) == 2
        assert command[0] == "uv"
        assert "(n\n)" in (kwargs["cwd"] / "lib.rs").read_text()
        return CommandResult(
            0,
            json.dumps(
                {
                    "passed": True,
                    "tests_count": 1,
                    "successes_count": 1,
                    "failures_count": 0,
                    "errors_count": 0,
                }
            ),
        )

    output = run_stage(
        value,
        stage_dir=STAGE_DIR,
        tools=tools,
        llm_client_factory=factory,
        validation_runner=runner,
    )
    assert output.status == "success", output.error
    assert settings_used == [
        {"provider": "replay", "model": "cheap-model", "context_overflow": "error"}
    ]
    assert len(requests) == 2
    assert output.usage.calls == 2
    assert output.metrics["llm_generation_calls"] == 0
    assert [(prompt.id, prompt.version) for prompt in output.prompts] == [
        ("wrapper_bound_inference", 1)
    ]
    records = read_usage(value.framework.workdir / "usage.jsonl")
    assert len(records) == 2
    assert all(record["model"] == "cheap-model" for record in records)
    assert all(record["prompt_id"] == "wrapper_bound_inference" for record in records)
    report = json.loads(
        (value.outputs.artifacts_dir / "test-validation.json").read_text()
    )
    assert report["status"] == ("validation_succeeded" if manifest else "no_manifest")
    assert [item["status"] for item in report["wrapper_bounds"]["wrappers"]] == (
        ["unchanged", "applied"] if provider_error else ["applied", "applied"]
    )
    assert (
        json.loads((value.outputs.artifacts_dir / "observations.json").read_text())[
            "observations"
        ]
        == []
    )
    assert (value.inputs.rust_project / "lib.rs").read_text() == "old\n"
    assert not (value.framework.workdir / "test-validation/c-reference").exists()


def test_stage_does_not_initialize_llm_client_without_eligible_wrapper(tmp_path):
    value = stage_input(tmp_path)
    output = run_stage(
        value,
        stage_dir=STAGE_DIR,
        tools=FakeTools(),
        llm_client_factory=lambda *args: pytest.fail("no model initialization"),
    )
    assert output.status == "success"
    report = json.loads(
        (value.outputs.artifacts_dir / "test-validation.json").read_text()
    )
    assert report["wrapper_bounds"]["status"] == "no_wrappers"
    assert output.usage is None


def test_tooling_wrapper_bounds_uses_read_only_json_protocol(tmp_path):
    request = tmp_path / "request.json"
    request.write_text('{"source":"fn f() {}"}')
    output = tmp_path / "response.json"
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        output.write_text('{"wrappers":[],"candidate":null}')
        return CommandResult(0)

    tools = CratTools(tmp_path / "tools.log", run_command=runner)
    tools.crat_tool = Path("/tools/crat-tool")
    assert tools.wrapper_bounds(request, output) == {"wrappers": [], "candidate": None}
    assert calls == [
        [
            "/tools/crat-tool",
            "wrapper-bounds",
            "--input",
            str(request),
            "--output",
            str(output),
        ]
    ]
    with pytest.raises(StageFailure):
        tools.wrapper_bounds(request, request)
    assert request.read_text() == '{"source":"fn f() {}"}'


@pytest.mark.parametrize("kind", ["usage", "destination", "symlink"])
def test_bound_scratch_boundaries_are_validated_before_work(tmp_path, kind):
    value = stage_input(tmp_path)
    root = value.framework.workdir / "wrapper-bounds"
    if kind == "usage":
        root.mkdir()
        usage = root / "usage.jsonl"
        usage.write_text("preserve\n")
        value = replace(value, framework=replace(value.framework, usage_log=usage))
    elif kind == "destination":
        value = replace(
            value, outputs=replace(value.outputs, rust_project=root / "output")
        )
    else:
        target = tmp_path / "external"
        target.mkdir()
        root.symlink_to(target, target_is_directory=True)
    tools = FakeTools()
    output = run_stage(value, stage_dir=STAGE_DIR, tools=tools)
    assert output.status == "failure"
    assert not tools.events
