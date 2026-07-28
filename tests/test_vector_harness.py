"""V1 unit tests: JUnit parsing and corpus assembly (no toolchain).

The actual harness invocation (cargo/subprocess) is exercised in the
e2e tests."""

from pathlib import Path

import pytest

from proctor.testing.vector_harness import (
    VectorHarnessError,
    _assemble_corpus,
    parse_junit,
)

# mirrors the vendored reporter's format (tools/tractor_runtests reporters/junit_xml.py)
SAMPLE_JUNIT = """<?xml version='1.0' encoding='utf-8'?>
<testsuites name="Tests" tests="4" errors="1" skipped="1" failures="1">
  <testsuite name="Public-Tests/B01_synthetic/033_bitfield" tests="4" failures="1" errors="1" skipped="1">
    <testcase name="build" classname="Public-Tests/B01_synthetic/033_bitfield" />
    <testcase name="test1" classname="Public-Tests/B01_synthetic/033_bitfield" />
    <testcase name="test2" classname="Public-Tests/B01_synthetic/033_bitfield">
      <failure message="stdout mismatch" />
    </testcase>
    <testcase name="test3" classname="Public-Tests/B01_synthetic/033_bitfield">
      <skipped message="has_ub" />
    </testcase>
    <testcase name="test4" classname="Public-Tests/B01_synthetic/033_bitfield">
      <error message="cando internal error" />
    </testcase>
  </testsuite>
</testsuites>
"""


def test_parse_junit_statuses() -> None:
    report = parse_junit(SAMPLE_JUNIT)
    by_name = {r.name: r for r in report.results}
    assert by_name["build"].status == "pass"
    assert by_name["test1"].status == "pass"
    assert by_name["test2"].status == "fail"
    assert by_name["test2"].message == "stdout mismatch"
    assert by_name["test3"].status == "skip"
    assert by_name["test4"].status == "error"


def test_report_counts_exclude_build() -> None:
    report = parse_junit(SAMPLE_JUNIT)
    assert report.case == "Public-Tests/B01_synthetic/033_bitfield"
    assert report.build_ok is True
    assert report.total == 4  # test1..test4, build excluded
    assert report.passed == 1  # test1
    assert report.failed == 2  # test2 (fail) + test4 (error)
    assert report.skipped == 1  # test3
    assert report.ok is False  # a vector failed


def test_all_pass_report() -> None:
    xml = """<testsuites><testsuite name="c">
      <testcase name="build"/><testcase name="test1"/><testcase name="test2"/>
    </testsuite></testsuites>"""
    report = parse_junit(xml, case="c")
    assert report.ok is True
    assert report.passed == 2 and report.failed == 0
    assert report.summary()["vectors"] == {"test1": "pass", "test2": "pass"}


def test_build_failure_makes_report_not_ok() -> None:
    xml = """<testsuites><testsuite name="c">
      <testcase name="build"><failure message="cargo build failed"/></testcase>
      <testcase name="test1"/>
    </testsuite></testsuites>"""
    report = parse_junit(xml, case="c")
    assert report.build_ok is False
    assert report.ok is False


def test_invalid_junit_raises() -> None:
    with pytest.raises(VectorHarnessError, match="invalid JUnit"):
        parse_junit("<not valid xml")


def _corpus_case(tmp_path: Path, *, library: bool = False) -> tuple[Path, Path]:
    """Returns (translated_rust, case_dir)."""
    translated = tmp_path / "out" / "rust"
    translated.mkdir(parents=True)
    (translated / "Cargo.toml").write_text(
        "[package]\nname='driver'\n", encoding="utf-8"
    )

    case = tmp_path / "corpus_src" / "001_helloworld"
    (case / "test_vectors").mkdir(parents=True)
    (case / "test_vectors" / "test1.json").write_text("{}", encoding="utf-8")
    (case / "test_case" / "src").mkdir(parents=True)
    (case / "test_case" / "CMakeLists.txt").write_text("", encoding="utf-8")
    if library:
        (case / "runner" / "src").mkdir(parents=True)
        (case / "runner" / "Cargo.toml").write_text("", encoding="utf-8")
    return translated, case


def test_assemble_corpus_binary(tmp_path: Path) -> None:
    translated, case = _corpus_case(tmp_path)
    dest_root = tmp_path / "assembled"
    name = _assemble_corpus(translated, case, dest_root)
    assert name == "001_helloworld"
    d = dest_root / "001_helloworld"
    assert (d / "translated_rust" / "Cargo.toml").is_file()
    assert (d / "test_vectors" / "test1.json").is_file()
    assert (d / "test_case" / "CMakeLists.txt").is_file()
    assert not (d / "runner").exists()  # binary case: no runner


def test_assemble_corpus_library_includes_runner(tmp_path: Path) -> None:
    translated, case = _corpus_case(tmp_path, library=True)
    dest_root = tmp_path / "assembled"
    _assemble_corpus(translated, case, dest_root)
    assert (dest_root / "001_helloworld" / "runner" / "Cargo.toml").is_file()


def test_assemble_refuses_bad_inputs(tmp_path: Path) -> None:
    # missing Cargo.toml in translated
    (tmp_path / "notcargo").mkdir()
    (tmp_path / "case" / "test_vectors").mkdir(parents=True)
    with pytest.raises(VectorHarnessError, match="not a Cargo project"):
        _assemble_corpus(tmp_path / "notcargo", tmp_path / "case", tmp_path / "a")
    # missing test_vectors
    good = tmp_path / "rust"
    good.mkdir()
    (good / "Cargo.toml").write_text("", encoding="utf-8")
    with pytest.raises(VectorHarnessError, match="test_vectors"):
        _assemble_corpus(good, tmp_path / "novectors", tmp_path / "b")
