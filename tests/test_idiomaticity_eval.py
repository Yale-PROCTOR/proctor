"""idiomaticity_eval: report grouping/summary + parsing measure_idiomaticity's
JSON (the clippy subprocess is stubbed)."""

from pathlib import Path

import pytest

from proctor.testing import idiomaticity_eval as ie
from proctor.testing.idiomaticity_eval import IdiomEvalError, IdiomReport


def _report() -> IdiomReport:
    return IdiomReport(
        by_group={
            "style": {"needless_return": 5, "redundant_clone": 2},
            "complexity": {"needless_bool": 3},
        },
        complexity={4: 10, 19: 1},
        loc=1000,
    )


def test_totals_groups_and_top_lints() -> None:
    r = _report()
    assert r.total == 10
    assert r.group_totals() == {"style": 7, "complexity": 3}
    assert r.top_lints(2) == [("needless_return", 5), ("needless_bool", 3)]
    assert r.per_kloc == 10.0
    assert r.max_complexity == 19


def test_summary_mentions_groups_and_complexity() -> None:
    s = _report().summary()
    assert "10 clippy lints" in s and "style 7" in s and "max cog-complexity 19" in s


def test_measure_idiomaticity_parses_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    crate = tmp_path / "crate"
    crate.mkdir()

    def fake_run(cmd, **kw):
        out = Path(cmd[cmd.index("--output") + 1])
        out.write_text(
            '{"clippy": {"style": {"needless_return": 4}},'
            ' "cyclomatic_complexity_counts": {"7": 2}}',
            "utf-8",
        )

        class P:
            returncode = 0
            stdout = ""
            stderr = ""

        return P()

    monkeypatch.setattr(ie.subprocess, "run", fake_run)
    r = ie.measure_idiomaticity(crate, workdir=tmp_path / "wd", loc=500)
    assert r.total == 4 and r.group_totals() == {"style": 4}
    assert r.complexity == {7: 2} and r.loc == 500


def test_measure_idiomaticity_no_output_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(cmd, **kw):
        class P:
            returncode = 1
            stdout = ""
            stderr = "clippy blew up"

        return P()

    monkeypatch.setattr(ie.subprocess, "run", fake_run)
    with pytest.raises(IdiomEvalError, match="no output"):
        ie.measure_idiomaticity(tmp_path / "c", workdir=tmp_path / "wd")


def test_measure_idiomaticity_build_failure_is_not_zero_lints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a crate that doesn't compile yields an empty lint set + a build_error;
    # it must raise (skip), NOT return total=0 ("maximally idiomatic").
    def fake_run(cmd, **kw):
        out = Path(cmd[cmd.index("--output") + 1])
        out.write_text(
            '{"clippy": {}, "build_error": "error[E0412]: not found"}', "utf-8"
        )

        class P:
            returncode = 0
            stdout = ""
            stderr = ""

        return P()

    monkeypatch.setattr(ie.subprocess, "run", fake_run)
    with pytest.raises(IdiomEvalError, match="did not build"):
        ie.measure_idiomaticity(tmp_path / "c", workdir=tmp_path / "wd")


def test_measure_idiomaticity_timeout_is_wrapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(cmd, **kw):
        raise ie.subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(ie.subprocess, "run", fake_run)
    with pytest.raises(IdiomEvalError, match="timed out"):
        ie.measure_idiomaticity(tmp_path / "c", workdir=tmp_path / "wd")
