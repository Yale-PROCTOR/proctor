#!/usr/bin/env python3

"""Adapted from agent/src/agent/clippy.py"""

import argparse
import json
import logging
import re
import subprocess
from pathlib import Path

from clippy_lint_map import ClippyLintMap

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(message)s")


class Clippy:
    def __init__(self, lint_to_group_dct: dict, timeout: float):
        self.lint_to_group_dct = lint_to_group_dct
        self.timeout = timeout

    def rustup_add_clippy(self, timeout: float | None = None):
        """
        Runs the command to add clippy according to the current project if necessary.
        """
        timeout = timeout or self.timeout

        try:
            subprocess.run(
                ["rustup", "component", "add", "clippy"],
                timeout=timeout,
                check=True,
                capture_output=True,
                text=True,
            )

        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            logger.error("Exception occured during rustup add clippy", exc_info=e)
            logger.error(f"stdout: {e.stdout}")
            logger.error(f"stderr: {e.stderr}")

    def _process_rendered_lints(self, msg: str) -> str:
        pats = [
            r"^\s*= help:.*?(\n|$)",
            r"^\s*= note:.*?(\n|$)",
        ]
        new_msg = msg

        for pat in pats:
            new_msg = re.sub(pat, "", new_msg, flags=re.MULTILINE)

        return new_msg.strip()

    def _collect_lints(
        self,
        out_channel: str,
        lint_to_group_dct: dict,
    ) -> dict:
        rendered_filename_pattern = re.compile(r"--> (.*):\d+:\d+")
        cog_comp_pattern = re.compile(
            r"^the function has a cognitive complexity of \((\d+)/0\)$"
        )
        cog_counts = {}
        rustc_msgs = {}
        clippy_msgs = {}

        for line in out_channel.split("\n"):
            if not line.startswith("{"):
                continue

            dct = json.loads(line)

            if dct["reason"] != "compiler-message":
                logger.debug(f"Other:\n{dct}\n")
                continue

            msg = dct.get("message") or {}
            rendered = (msg.get("rendered") or "nothing").strip()
            level = msg.get("level") or "unknown"
            code_dct = msg.get("code") or {}
            kind = code_dct.get("code") or "unknown"

            filename = "unknown"
            match = rendered_filename_pattern.search(rendered)
            if match:
                filename = match.group(1)

            logger.debug(
                f"dictionary:\n{json.dumps(dct, indent=2)}\n"
                f"> rendered:\n{rendered}\n"
                f"> filename: {filename}\n"
                f"> level: {level}\n"
                f"> code_dct: {code_dct}\n"
                f"> kind: {kind}\n"
            )

            info_dct = {
                "rendered": self._process_rendered_lints(rendered),
                "filename": filename,
            }

            if kind == "clippy::cognitive_complexity":
                printed_msg = msg.get("message") or ""
                match = cog_comp_pattern.match(printed_msg)
                if match:
                    cog_value = int(match.group(1))
                    cog_counts.setdefault(cog_value, 0)
                    cog_counts[cog_value] += 1

            elif kind.startswith("clippy::"):
                lint = kind.removeprefix("clippy::")
                group = lint_to_group_dct.get(lint, "unknown")

                clippy_msgs.setdefault(group, {})
                clippy_msgs[group].setdefault(lint, [])

                msg_list = clippy_msgs[group][lint]
                if info_dct not in msg_list:
                    msg_list.append(info_dct)

            # skip one-line errors/warnings
            elif rendered.count("\n") == 0:
                logger.debug(f"Skipping: {rendered}")
                continue

            else:
                rustc_msgs.setdefault(level, {})
                rustc_msgs[level].setdefault(kind, [])

                msg_list = rustc_msgs[level][kind]
                if info_dct not in msg_list:
                    msg_list.append(info_dct)

        ret = {}
        if rustc_msgs:
            ret["rustc"] = rustc_msgs

        if clippy_msgs:
            ret["clippy"] = clippy_msgs

        if cog_counts:
            ret["cyclomatic_complexity_counts"] = cog_counts

        return ret

    def run_clippy(
        self,
        lint_to_group_dct: dict | None = None,
        timeout: float | None = None,
        fix: bool = False,
        ccc_counts: bool = False,
        dump_cargo_logs: Path | None = None,
    ) -> tuple[dict, str]:
        """
        Runs clippy in the current directory, which needs to be a Rust project.

        Returns a tuple of a ClippyResultBase and an error message.
        The internal dictionary is of the format:
        {
            "rustc": {
                "level": {
                    "code": [{}, ...],
                }
            },
            "clippy": {
                "group": {
                    "lint": [{}, ...],
                }
            },
            [optional] "cyclomatic_complexity_counts": {
                complexity: count
            }
        }
        """

        lint_to_group_dct = lint_to_group_dct or self.lint_to_group_dct
        timeout = timeout or self.timeout

        clippy_lint_args = [
            ("-D", "correctness"),
            ("-W", "suspicious"),
            ("-W", "complexity"),
            ("-W", "perf"),
            ("-W", "style"),
        ]

        if ccc_counts:
            clippy_lint_args.append(("-W", "cognitive_complexity"))

        args = [
            "cargo",
            "clippy",
            "--message-format",
            "json",
            "--workspace",
            "--all-targets",
            *(["--fix", "--allow-no-vcs", "--allow-dirty"] if fix else []),
            "--",
            "-A",
            "clippy::all",
        ]
        for flag, name in clippy_lint_args:
            args.extend([flag, f"clippy::{name}"])

        try:
            res = subprocess.run(
                args,
                timeout=timeout,
                check=True,
                capture_output=True,
                text=True,
            )
            if dump_cargo_logs:
                logger.info(f"Dumping cargo logs to {dump_cargo_logs}")
                dump_cargo_logs.write_text(res.stdout)

            res_base = self._collect_lints(res.stdout, lint_to_group_dct)
            error = ""

        except subprocess.CalledProcessError as e:
            if dump_cargo_logs:
                logger.info(f"Dumping cargo logs to {dump_cargo_logs}")
                dump_cargo_logs.write_text(e.stdout)

            logger.warning(f"Clippy terminated with return code: {e.returncode}")
            res_base = self._collect_lints(e.stdout, lint_to_group_dct)
            error = str(e.stderr.strip())

        except Exception as e:
            logger.error("Exception occured during clippy", exc_info=e)
            res_base = {}
            error = str(e)

        return res_base, error

    def convert_to_count(self, base_dct: dict) -> dict:
        """
        Converts the dictionary from base format to count format.
        The new dictionary contains number of lints encountered without details:
        {
            "rustc": {
                "level": {
                    "code": occurence_count,
                }
            },
            "clippy": {
                "group": {
                    "lint": occurence_count,
                }
            },
        }
        """
        res_dct = {}

        for key_0, dict_0 in base_dct.items():
            if key_0 == "cyclomatic_complexity_counts":
                res_dct[key_0] = dict(dict_0)
                continue

            res_dct.setdefault(key_0, {})

            for key_1, dict_1 in dict_0.items():
                res_dct[key_0].setdefault(key_1, {})

                for key_2, list_2 in dict_1.items():
                    cnt_2 = len(list_2)
                    res_dct[key_0][key_1].setdefault(key_2, cnt_2)

        return res_dct


def get_parser():
    parser = argparse.ArgumentParser(
        usage="%(prog)s [options] project_dir",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        "project_dir",
        type=str,
        help="the directory of the Rust project",
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=Path("idiomaticity.json"),
        help="output path to store the results",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="use clippy fix",
    )

    parser.add_argument(
        "--clippy_lint_to_group_json",
        type=Path,
        default=ClippyLintMap.LINT_TO_GROUP_FILE,
        help="the json file with lint to group mappings",
    )

    parser.add_argument(
        "--timeout",
        default=300,
        help="timeout (sec) of the clippy invocation",
    )

    parser.add_argument(
        "--include_ccc",
        action="store_true",
        help="include cyclomatic_complexity_counts",
    )

    parser.add_argument(
        "--dump_cargo_logs",
        type=Path,
        default=None,
        help="path to dump the raw cargo logs",
    )

    parser.add_argument(
        "--debug",
        action="store_true",
        help="enable debug logging",
    )

    return parser


def main():
    from os import chdir

    parser = get_parser()
    args = parser.parse_args()

    if args.debug:
        logger.setLevel("DEBUG")

    dump_cargo_logs = args.dump_cargo_logs
    if dump_cargo_logs:
        dump_cargo_logs = dump_cargo_logs.resolve()

    output: Path = args.output.resolve()

    clippy_lint_map = ClippyLintMap(lint_to_group_file=args.clippy_lint_to_group_json)
    lint_to_group_dct = clippy_lint_map.load_lint_to_group()

    clippy = Clippy(lint_to_group_dct, args.timeout)

    chdir(args.project_dir)
    clippy.rustup_add_clippy()

    base_dct, err = clippy.run_clippy(
        fix=args.fix,
        ccc_counts=args.include_ccc,
        dump_cargo_logs=dump_cargo_logs,
    )

    if err:
        logger.error(f"Error occured: {err}")
        return

    count_dct = clippy.convert_to_count(base_dct)
    dct_str = json.dumps(count_dct, indent=2)
    logger.info(dct_str)
    output.write_text(dct_str)


if __name__ == "__main__":
    main()
