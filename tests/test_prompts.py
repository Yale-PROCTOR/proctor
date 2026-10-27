"""Prompt library: parsing, versioning, strict rendering, hashes."""

from pathlib import Path

import pytest

from proctor.prompts.library import PromptError, PromptLibrary

REPO = Path(__file__).parent.parent
TEMPLATES = REPO / "proctor" / "prompts" / "templates"


def _write(directory: Path, name: str, content: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(content, encoding="utf-8")


def test_builtin_templates_load() -> None:
    library = PromptLibrary(TEMPLATES)
    assert "wrapper_preserve_update" in library.ids()
    template = library.get("wrapper_preserve_update")
    assert template.version >= 1
    assert "api_signature" in template.variables


def test_render_and_hash_stability() -> None:
    library = PromptLibrary(TEMPLATES)
    template = library.get("compile_error_repair")
    first = template.render(
        errors="E0308", source_context="fn x() {}", constraints="- none"
    )
    second = template.render(
        errors="E0308", source_context="fn x() {}", constraints="- none"
    )
    assert "E0308" in first.text
    assert first.content_hash == second.content_hash
    third = template.render(
        errors="E0999", source_context="fn x() {}", constraints="- none"
    )
    assert third.content_hash != first.content_hash


def test_missing_variable_is_error() -> None:
    library = PromptLibrary(TEMPLATES)
    template = library.get("compile_error_repair")
    with pytest.raises(PromptError, match="undefined"):
        template.render(errors="E0308")


def test_included_guidance_uses_strict_variables_and_changes_rendered_hash(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "greet.md",
        '+++\nid = "greet"\nversion = 1\nvariables = ["name"]\n+++\n'
        '{% include "partials/guidance.jinja" %}',
    )
    _write(tmp_path / "partials", "guidance.jinja", "hello {{ name }}\n")
    template = PromptLibrary(tmp_path).get("greet")
    first = template.render(name="Ada")
    assert first.text == "hello Ada\n"
    with pytest.raises(PromptError, match="undefined"):
        template.render()
    _write(tmp_path / "partials", "guidance.jinja", "welcome {{ name }}\n")
    updated = template.render(name="Ada")
    assert updated.text == "welcome Ada\n"
    assert updated.content_hash != first.content_hash


def test_missing_included_guidance_identifies_the_prompt(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "greet.md",
        '+++\nid = "greet"\nversion = 1\n+++\n{% include "partials/missing.jinja" %}',
    )
    template = PromptLibrary(tmp_path).get("greet")
    with pytest.raises(PromptError, match="prompt 'greet' v1:.*partials/missing.jinja"):
        template.render()


def test_unknown_variable_is_error() -> None:
    library = PromptLibrary(TEMPLATES)
    template = library.get("compile_error_repair")
    with pytest.raises(PromptError, match="unknown variables"):
        template.render(errors="x", source_context="y", constraints="z", bogus="w")


def test_version_pinning(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "greet.md",
        '+++\nid = "greet"\nversion = 1\nvariables = ["name"]\n+++\nhi {{ name }}\n',
    )
    _write(
        tmp_path,
        "greet_v2.md",
        '+++\nid = "greet"\nversion = 2\nvariables = ["name"]\n+++\nhello {{ name }}\n',
    )
    library = PromptLibrary(tmp_path)
    assert library.versions("greet") == [1, 2]
    assert library.get("greet").version == 2  # latest by default
    pinned = library.get("greet", version=1)
    assert pinned.render(name="x").text.startswith("hi")


def test_duplicate_id_version_rejected(tmp_path: Path) -> None:
    body = '+++\nid = "dup"\nversion = 1\n+++\nsame\n'
    _write(tmp_path, "a.md", body)
    _write(tmp_path, "b.md", body)
    with pytest.raises(PromptError, match="duplicate"):
        PromptLibrary(tmp_path)


def test_malformed_frontmatter_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "bad.md", "no frontmatter here\n")
    with pytest.raises(PromptError, match="frontmatter"):
        PromptLibrary(tmp_path)


def test_unknown_prompt_lists_available(tmp_path: Path) -> None:
    _write(tmp_path, "only.md", '+++\nid = "only"\nversion = 1\n+++\nx\n')
    library = PromptLibrary(tmp_path)
    with pytest.raises(PromptError, match="available"):
        library.get("nope")
