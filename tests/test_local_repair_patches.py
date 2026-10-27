from __future__ import annotations

import json

import pytest

from patching import PatchError, apply_patch
from protocol import PromptRenderInput, render_prompt
from tests.test_local_transformation import (
    FakeClient,
    FakeTools,
    INVALID,
    VALID,
    extract_code_block,
    fn_record,
    patch_response,
    response,
    run_fake,
)
from tests.test_local_unstructured_repair import REPAIRED, SEED, UnstructuredTools
from tooling import CommandResult


def hunk(text):
    return (
        "*** Begin Patch\n*** Update File: transformation.rs\n"
        + text
        + "\n*** End Patch"
    )


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("trailing", ["", "\n"])
def test_patch_changes_only_requested_lines_and_preserves_line_endings(
    newline, trailing
):
    base = "fn first() {\n    a();\n    b();\n    c();\n}\n\nfn second() {}" + trailing
    patch = hunk(
        "@@ fn first() {\n-    a();\n+    replacement();\n"
        "@@ fn first() {\n     b();\n-    c();\n+    extra();\n+    c();"
    )
    expected = base.replace("    a();", "    replacement();").replace(
        "    c();", "    extra();\n    c();"
    )
    assert apply_patch(base.replace("\n", newline), patch) == expected.replace(
        "\n", newline
    )


def test_patch_preserves_mixed_line_endings_outside_additions():
    base = "a\r\nb\nc\r\nd"
    assert apply_patch(base, hunk("@@\n a\n-b\n+new\n c")) == "a\r\nnew\r\nc\r\nd"
    assert apply_patch(base, hunk("@@ d\n+end")) == base + "\r\nend"


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("trailing", ["", "\n"])
@pytest.mark.parametrize("terminators", [1, 2, 4])
def test_patch_accepts_repeated_trailing_terminators(newline, trailing, terminators):
    patch = (
        hunk("@@\n-before\n+after\n unchanged")
        + "\n*** End Patch" * (terminators - 1)
        + trailing
    ).replace("\n", newline)
    assert apply_patch(f"before{newline}unchanged{newline}", patch) == (
        f"after{newline}unchanged{newline}"
    )


def test_patch_repeated_terminators_preserve_marker_text_in_file_contents():
    patch = hunk("@@\n *** End Patch\n-old\n+*** End Patch") + "\n*** End Patch"
    assert apply_patch("*** End Patch\nold", patch) == "*** End Patch\n*** End Patch"


@pytest.mark.parametrize(
    "base, changes, expected",
    [
        ("fn f() {\n}", "@@ fn f() {\n+    value();", "fn f() {\n    value();\n}"),
        ("", "@@\n+fn f() {}", "fn f() {}"),
        ("a\nb\na\nb", "@@\n-a\n-b\n+end\n*** End of File", "a\nb\nend"),
        ("a\nb", "@@\n+a\n*** End of File", "a\nb\na"),
        ("a\nb", "@@\n-b", "a\n"),
        ("a\r\nb", "@@\n-b", "a\r\n"),
        ("a\nb", "@@\n-a\n-b", ""),
        ("a\n\nb", "@@\n a\n-\n b", "a\nb"),
        ("outer\ninner\nold", "@@ outer\n@@ inner\n-old\n+new", "outer\ninner\nnew"),
        ('let x = "a\\nb";', '@@\n-let x = "a\\nb";\n+let x = "b";', 'let x = "b";'),
    ],
)
def test_patch_context_insertions_deletions_and_end_anchor(base, changes, expected):
    assert apply_patch(base, hunk(changes)) == expected


@pytest.mark.parametrize(
    "patch",
    [
        "",
        "```diff\n" + hunk("@@\n-a\n+b") + "\n```",
        "Explanation\n" + hunk("@@\n-a\n+b"),
        hunk("@@\n-a\n+b") + "\nExplanation",
        hunk("@@\n-a\n+b").replace("transformation.rs", "../lib.rs"),
        hunk("@@\n-a\n+b").replace("Update File", "Add File"),
        hunk("@@\n-a\n+b").replace("Update File", "Delete File"),
        hunk("*** Move to: other.rs\n@@\n-a\n+b"),
        hunk("@@\n-a\n+b\n*** Update File: other.rs\n@@\n-a\n+b"),
        hunk("@@ -1 +1 @@\n-a\n+b"),
        hunk("@@\na\n+b"),
        hunk("@@\n a"),
        hunk("@@\n-a\n+b\n*** End of File\n@@\n-b\n+c"),
        hunk("@@\n-a\n+b").removesuffix("\n*** End Patch"),
    ],
)
def test_patch_rejects_unsupported_or_malformed_responses(patch):
    with pytest.raises(PatchError):
        apply_patch("a", patch)


@pytest.mark.parametrize(
    "patch",
    [
        hunk("@@\n-a\n+b") + "\n*** End Patch\nExplanation",
        hunk("@@\n-a\n+b") + "\n*** End Patch\n\n",
        hunk("@@\n-a\n+b") + "\n\n*** End Patch",
        hunk("@@\n-a\n+b") + "\n " + "\n*** End Patch",
        hunk("@@\n-a\n+b") + "\n" + hunk("@@\n-b\n+c") + "\n*** End Patch",
        hunk("@@\n-a\n+b\n*** End Patch\n@@\n-b\n+c") + "\n*** End Patch",
        hunk("@@\n-a\n+b\n*** Update File: other.rs\n@@\n-a\n+b") + "\n*** End Patch",
        hunk("@@\n-a\n+b\n*** Add File: other.rs\n+c") + "\n*** End Patch",
        hunk("@@\n-a\n+b\n*** Delete File: other.rs") + "\n*** End Patch",
        hunk("@@\n-a\n+b\n*** Move to: other.rs") + "\n*** End Patch",
        hunk("@@\na\n+b") + "\n*** End Patch",
    ],
)
def test_patch_repeated_terminators_do_not_relax_other_framing(patch):
    with pytest.raises(PatchError):
        apply_patch("a", patch)


@pytest.mark.parametrize(
    "base, changes, diagnostic",
    [
        ("a\na", "@@\n-a\n+b", "ambiguous"),
        ("a\na", "@@ a\n+x", "ambiguous"),
        (" a", "@@\n-a\n+b", "exactly"),
        ("a\nb", "@@\n-b\n+c\n@@\n-a\n+d", "exactly"),
        ("a\nb", "@@\n-a\n+b\n@@\n-missing\n+c", "exactly"),
        ("a", "@@\n+b", "locate"),
    ],
)
@pytest.mark.parametrize("extra_terminator", ["", "\n*** End Patch"])
def test_patch_rejects_ambiguous_mismatched_or_overlapping_context(
    base, changes, diagnostic, extra_terminator
):
    with pytest.raises(PatchError, match=diagnostic):
        apply_patch(base, hunk(changes) + extra_terminator)


@pytest.mark.parametrize("unstructured", [False, True])
def test_patch_prompts_retain_context_and_only_latest_rejected_patch(unstructured):
    rendered = render_prompt(
        PromptRenderInput(
            "DEPENDENCIES",
            "TARGETS",
            "BASE",
            "CODE ERROR",
            unstructured=unstructured,
            patch_failure=("REJECTED PATCH", "PATCH ERROR"),
        )
    )
    for content in [
        "DEPENDENCIES",
        "TARGETS",
        "BASE",
        "CODE ERROR",
        "REJECTED PATCH",
        "PATCH ERROR",
    ]:
        assert content in rendered.text
    assert rendered.text.count("## Repair Base: `transformation.rs`") == 1
    assert "Return only one Codex-style apply_patch patch" in rendered.text
    assert "Regenerate every" not in rendered.text
    assert "Return exactly one" not in rendered.text


@pytest.mark.parametrize("failure_kind", ["structural", "compilation"])
def test_invalid_patches_preserve_base_and_code_failure_then_clear_after_application(
    tmp_path, failure_kind
):
    first = response(statement="missing();")
    second = response(statement="different();")
    last = response(statement="fixed();")
    initial_code = extract_code_block(first).candidate
    second_code = extract_code_block(second).candidate
    invalid_one = hunk(
        "@@\n-    missing();\n+    first_attempt();\n@@\n-    absent();\n+    other();"
    )
    invalid_two = hunk("@@\n-    also_absent();\n+    second_attempt();")
    errors = [json.dumps(INVALID), json.dumps({**INVALID, "detail": "fresh error"})]
    compilation = failure_kind == "compilation"
    if compilation:
        errors = ["first compiler error", "second compiler error"]
    tools = FakeTools(
        skeletons=[fn_record(0, "target", "target", [])],
        validators=[VALID] * 3
        if compilation
        else [
            INVALID,
            {
                **INVALID,
                "failures": [
                    {**INVALID["failures"][0], "failed_snippet": "fresh error"}
                ],
            },
            VALID,
        ],
        candidates=["first failed source\n", "second failed source\n", "accepted\n"]
        if compilation
        else ["accepted\n"],
        builds=[
            CommandResult(0),
            CommandResult(101, "", errors[0]),
            CommandResult(101, "", errors[1]),
            CommandResult(0),
        ]
        if compilation
        else None,
    )
    client = FakeClient(
        [
            first,
            invalid_one,
            invalid_two,
            patch_response(first, second),
            patch_response(second, last),
        ]
    )
    value, output = run_fake(
        tmp_path, tools, client, config={"dump_llm_exchanges": True}
    )
    assert output.status == "success", output.error
    prompts = [request.messages[0].content for request in client.requests]
    for prompt in prompts[1:4]:
        assert initial_code in prompt
        assert errors[0] in prompt
    assert invalid_one in prompts[2]
    assert invalid_two in prompts[3] and invalid_one not in prompts[3]
    assert (
        prompts[2].split("Previous rejected patch:")[0].rstrip() == prompts[1].rstrip()
    )
    assert second_code in prompts[4]
    assert (errors[1] if compilation else "fresh error") in prompts[4]
    assert "Previous rejected patch:" not in prompts[4]
    if compilation:
        assert errors[0] not in prompts[4]
    validations = [
        event[1]["transformation"] for event in tools.events if event[0] == "validate"
    ]
    assert validations == [
        initial_code,
        second_code,
        extract_code_block(last).candidate,
    ]
    assert output.metrics["repair_calls"] == 4
    assert output.usage.calls == output.metrics["llm_generation_calls"] == 5
    exchange = value.outputs.artifacts_dir / "llm-exchanges/scc-0"
    assert (exchange / "generation-02/repair-base.rs").read_text() == initial_code
    assert not (exchange / "generation-02/transformation.rs").exists()
    assert (exchange / "generation-03/transformation.rs").read_text() == second_code


def test_mode_transition_uses_code_history_and_drops_rejected_patch_feedback(tmp_path):
    first = response()
    rejected = hunk("@@\n-    absent();\n+    attempt();")
    tools = UnstructuredTools(
        seed=SEED,
        skeletons=[fn_record(0, "target", "target", [])],
        validators=[INVALID],
        unstructured_results=["failed seed\n", "accepted\n"],
        builds=[
            CommandResult(0),
            CommandResult(101, "", "fresh seed error"),
            CommandResult(0),
        ],
    )
    client = FakeClient([first, rejected, patch_response(SEED, REPAIRED)])
    _, output = run_fake(tmp_path, tools, client, config={"max_repairs": 1})
    assert output.status == "success", output.error
    preparation = next(
        event[1] for event in tools.events if event[0] == "prepare_unstructured"
    )
    assert preparation["transformations"] == [extract_code_block(first).candidate]
    prompt = client.requests[-1].messages[0].content
    assert SEED in prompt and "fresh seed error" in prompt
    assert rejected not in prompt
    assert "Patch application diagnostics:" not in prompt
    assert "missing_label" not in prompt and "#[proctor(" not in prompt
    assert output.metrics["repair_calls"] == 2
    assert output.metrics["unstructured_repair_calls"] == 1


def test_unstructured_invalid_patch_keeps_seed_and_unparseable_code_becomes_next_base(
    tmp_path,
):
    rejected = hunk("@@\n-absent\n+attempt")
    broken = "unsafe fn target("
    tools = UnstructuredTools(
        seed=SEED,
        skeletons=[fn_record(0, "target", "target", [])],
        validators=[INVALID],
        unstructured_results=[
            "failed seed\n",
            {
                "status": "invalid",
                "diagnostics": "syntax error",
                "failed_transformation": None,
            },
            "accepted\n",
        ],
        builds=[
            CommandResult(0),
            CommandResult(101, "", "seed build error"),
            CommandResult(0),
        ],
    )
    client = FakeClient(
        [
            response(),
            rejected,
            patch_response(SEED, broken),
            patch_response(broken, REPAIRED),
        ]
    )
    _, output = run_fake(tmp_path, tools, client, config={"max_repairs": 0})
    assert output.status == "success", output.error
    second_prompt = client.requests[2].messages[0].content
    assert SEED in second_prompt and "seed build error" in second_prompt
    assert rejected in second_prompt
    final_prompt = client.requests[-1].messages[0].content
    assert broken in final_prompt and "syntax error" in final_prompt
    assert "seed build error" not in final_prompt
    assert "Previous rejected patch:" not in final_prompt
    assert output.metrics["unstructured_repair_calls"] == 3
    replacements = [
        event[1]["transformation"]
        for event in tools.events
        if event[0] == "replace_unstructured"
    ]
    assert replacements == [SEED, broken, REPAIRED]


@pytest.mark.parametrize("seed", [None, ""])
def test_unstructured_missing_or_empty_seed_uses_explicit_stubs(tmp_path, seed):
    stub = "unsafe fn target() { todo!() }"
    tools = UnstructuredTools(
        seed=seed,
        skeletons=[fn_record(0, "target", "target", [])],
        unstructured_results=["accepted\n"],
    )
    client = FakeClient(["no initial code", patch_response(stub, REPAIRED)])
    _, output = run_fake(tmp_path, tools, client, config={"max_repairs": 0})
    assert output.status == "success", output.error
    assert f"```rust\n{stub}\n```" in client.requests[1].messages[0].content


def test_scc_patch_retains_unchanged_member(tmp_path):
    code = "unsafe fn first() { #[proctor(0)] () }\nunsafe fn second() { #[proctor(0)] () }"
    patch = hunk(
        "@@\n-unsafe fn second() { #[proctor(0)] () }\n+unsafe fn second() { #[proctor(0)] 1; }"
    )
    tools = FakeTools(
        skeletons=[
            fn_record(0, "first", "first", [1]),
            fn_record(1, "second", "second", [0]),
        ],
        validators=[INVALID, VALID],
        candidates=["accepted\n"],
    )
    _, output = run_fake(tmp_path, tools, FakeClient([f"```rust\n{code}\n```", patch]))
    assert output.status == "success", output.error
    transformations = [
        event[1]["transformation"] for event in tools.events if event[0] == "validate"
    ]
    assert transformations[-1] == code.replace(
        "unsafe fn second() { #[proctor(0)] () }",
        "unsafe fn second() { #[proctor(0)] 1; }",
    )


def test_empty_initial_code_uses_selected_skeleton_as_repair_base(tmp_path):
    record = fn_record(0, "target", "target", [])
    tools = FakeTools(
        skeletons=[record], validators=[INVALID, VALID], candidates=["accepted\n"]
    )
    client = FakeClient(
        ["```rust\n\n```", patch_response(record["applied"]["skeleton"], response())]
    )
    _, output = run_fake(tmp_path, tools, client)
    assert output.status == "success", output.error
    assert record["applied"]["skeleton"] in client.requests[1].messages[0].content
