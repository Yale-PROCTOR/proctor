#!/usr/bin/env python3

import argparse
import logging
import subprocess
import time
import traceback
from abc import abstractmethod
from pathlib import Path
from signal import SIGHUP, SIGINT, SIGTERM, signal, strsignal

SCRIPT_DIR = Path(__file__).resolve().parent
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] (%(process)d): %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__file__)


def run_command(cmd_args: list[str], timeout: float, failed_file: Path):
    def log_error(print_msg, log_msg):
        logger.error(print_msg)
        with failed_file.open("at") as fp:
            fp.write(log_msg)

    def fmt(cmd) -> str:
        return " ".join(cmd)

    try:
        p = subprocess.run(
            args=cmd_args,
            timeout=timeout,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=True,
            text=True,
        )
        logger.info(f"Command exited with code {p.returncode}: {fmt(p.args)}")
        logger.info(p.stdout)

    except subprocess.CalledProcessError as e:
        pmsg = f"Command exited with code {e.returncode}: {fmt(e.cmd)}"
        lmsg = f"{pmsg}\n{e.stdout}\n\n"
        log_error(pmsg, lmsg)

    except subprocess.TimeoutExpired as e:
        pmsg = f"Command timed out after {e.timeout} seconds: {fmt(e.cmd)}"
        lmsg = f"{pmsg}\n{e.stdout}\n\n"
        log_error(pmsg, lmsg)

    except Exception:
        pmsg = f"Command raised an exception: {fmt(cmd_args)}\n{traceback.format_exc()}"
        lmsg = f"{pmsg}\n\n"
        log_error(pmsg, lmsg)


class Action:
    name = "action"

    def __init__(self, args):
        self.args = args
        self.prefix = f"[{self.args.action}]"

    def _sequential_loop(self):
        raise NotImplementedError()

    def sequential_process(self):
        logger.info(f"{self.prefix} Processing in sequence: start")
        self._sequential_loop()
        logger.info(f"{self.prefix} Processing in sequence: end")

    @abstractmethod
    def _parallel_loop(self, pool):
        raise NotImplementedError()

    def parallel_process(self):

        from mpire import WorkerPool

        logger.info(f"{self.prefix} Processing in parallel: start")

        with WorkerPool(
            n_jobs=self.args.max_workers,
        ) as pool:

            def handle_signal(sig, _):
                pool.terminate()
                logger.critical(
                    f"{self.prefix} Exiting due to signal: {strsignal(sig)}"
                )
                logger.info(f"{self.prefix} Processing in parallel: end")
                exit(0)

            for sig in [SIGHUP, SIGINT, SIGTERM]:
                signal(sig, handle_signal)

            self._parallel_loop(pool)
            pool.join()

        logger.info(f"{self.prefix} Processing in parallel: end")

    def process(self):
        if self.args.mode == "sequential":
            self.sequential_process()
        elif self.args.mode == "parallel":
            self.parallel_process()
        else:
            logger.error(f"{self.prefix} Unknown mode: {self.args.mode}")


class Translate(Action):
    def _translate_dir_with_confs(
        self,
        dir: Path,
        confs: list[str],
        translate_script: Path,
        timeout: float,
    ):
        logger.info(f"Processing directory: {dir}")

        for conf in confs:
            run_command(
                cmd_args=[translate_script.as_posix(), f"--{conf}", dir.as_posix()],
                timeout=timeout,
                failed_file=self.args.failed_file,
            )

    def _sequential_loop(self):
        for bundle in self.args.bundles:
            bundle_dir = self.args.bundles_dir / bundle
            for dir in bundle_dir.glob("*"):
                if dir.is_dir():
                    self._translate_dir_with_confs(
                        dir=dir,
                        confs=self.args.confs,
                        translate_script=self.args.translate_script,
                        timeout=self.args.timeout,
                    )

    def _parallel_loop(self, pool):
        for bundle in self.args.bundles:
            bundle_dir = self.args.bundles_dir / bundle
            for dir in bundle_dir.glob("*"):
                if dir.is_dir():
                    pool.apply_async(
                        self._translate_dir_with_confs,
                        (
                            dir,
                            self.args.confs,
                            self.args.translate_script,
                            self.args.timeout,
                        ),
                        task_timeout=self.args.timeout + 30,
                        error_callback=lambda e: logger.error(f"T|Exception: {e}"),
                    )


class Aggregate(Action):
    def _aggregate_bundle_with_conf(
        self,
        atype: str,
        bundle_path: Path,
        conf: str,
        aggregate_script: Path,
        timeout: float,
    ):
        out_dir = Path(f"{atype}_results")
        out_dir.mkdir(exist_ok=True)

        types2ext = {
            "unsafety": "json",
            "idiomaticity": "json",
            "tests": "xml",
        }
        ext = types2ext[atype]

        file_pattern = f"**/translated_{conf}/results/{atype}.{ext}"
        out_prefix = out_dir / f"{bundle_path.name}_{conf}"

        run_command(
            cmd_args=[
                aggregate_script.as_posix(),
                "--type",
                atype,
                "--bundle_path",
                bundle_path.as_posix(),
                "--file_pattern",
                file_pattern,
                "--out_prefix",
                out_prefix.as_posix(),
            ],
            timeout=timeout,
            failed_file=self.args.failed_file,
        )

    def _sequential_loop(self):
        for atype in self.args.aggregate_types:
            for bundle in self.args.bundles:
                for conf in self.args.confs:
                    self._aggregate_bundle_with_conf(
                        atype=atype,
                        bundle_path=self.args.bundles_dir / bundle,
                        conf=conf,
                        aggregate_script=self.args.aggregate_script,
                        timeout=self.args.timeout,
                    )

    def _parallel_loop(self, pool):
        for atype in self.args.aggregate_types:
            for bundle in self.args.bundles:
                for conf in self.args.confs:
                    pool.apply_async(
                        self._aggregate_bundle_with_conf,
                        (
                            atype,
                            self.args.bundles_dir / bundle,
                            conf,
                            self.args.aggregate_script,
                            self.args.timeout,
                        ),
                        task_timeout=self.args.timeout + 30,
                        error_callback=lambda e: logger.error(f"A|Exception: {e}"),
                    )


class Visualize(Action):
    def _visualize_bundles_with_confs(
        self,
        vtype: str,
        bundles: list[str],
        confs: list[str],
        visualize_script: Path,
        timeout: float,
    ):
        agg_dir = Path(f"{vtype}_results")
        if not agg_dir.exists():
            logger.error(f"No directory found: {agg_dir}")
            return

        out_dir = Path(f"{agg_dir}/tables")
        out_dir.mkdir(exist_ok=True)

        out_file = out_dir / f"{vtype}.txt"
        out_file.unlink(missing_ok=True)

        run_command(
            cmd_args=[
                visualize_script.as_posix(),
                "--type",
                vtype,
                "--agg_dir",
                agg_dir.as_posix(),
                "--bundles",
                *bundles,
                "--confs",
                *confs,
                "--out_file",
                out_file.as_posix(),
            ],
            timeout=timeout,
            failed_file=self.args.failed_file,
        )

    def _sequential_loop(self):
        for vtype in self.args.visualize_types:
            self._visualize_bundles_with_confs(
                vtype=vtype,
                bundles=self.args.bundles,
                confs=self.args.confs,
                visualize_script=self.args.visualize_script,
                timeout=self.args.timeout,
            )

    def _parallel_loop(self, pool):
        for vtype in self.args.aggregate_types:
            pool.apply_async(
                self._visualize_bundles_with_confs,
                (
                    vtype,
                    self.args.bundles,
                    self.args.confs,
                    self.args.visualize_script,
                    self.args.timeout,
                ),
                task_timeout=self.args.timeout + 30,
                error_callback=lambda e: logger.error(f"V|Exception: {e}"),
            )


def get_parser():
    parser = argparse.ArgumentParser(
        usage="%(prog)s [options] bundles_dir",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        "bundles_dir",
        type=Path,
        help="the top-level bundles directory, e.g. Public-Tests",
    )

    parser.add_argument(
        "--bundles",
        type=str,
        nargs="+",
        default=[
            "B01_organic",
            "B01_synthetic",
            "P00_perlin_noise",
            "P01_sphincs_plus",
            "B02_organic",
            "B02_synthetic",
        ],
        help="the bundles to recurse into",
    )

    parser.add_argument(
        "--confs",
        type=str,
        nargs="+",
        default=[
            "c2rust",
            "c2rust_cfix",
            "c2rust_crat",
            "c2rust_crat_cfix",
        ],
        help="the configurations to use for the action",
    )

    parser.add_argument(
        "--action",
        required=True,
        choices=("translate", "aggregate", "visualize"),
        help="the action to perform",
    )

    parser.add_argument(
        "--mode",
        choices=("sequential", "parallel"),
        default="parallel",
        help="perform the action in sequence or in parallel",
    )

    parser.add_argument(
        "--translate_script",
        type=Path,
        default=SCRIPT_DIR / "translate.sh",
        help="the translate script to use",
    )

    parser.add_argument(
        "--aggregate_script",
        type=Path,
        default=SCRIPT_DIR / "aggregate.py",
        help="the aggregate script to use",
    )

    parser.add_argument(
        "--aggregate_types",
        type=str,
        nargs="+",
        default=["unsafety", "idiomaticity", "tests"],
        help="which types to aggregate",
    )

    parser.add_argument(
        "--visualize_script",
        type=Path,
        default=SCRIPT_DIR / "visualize.py",
        help="the visualize script to use "
        "(aggregate output is required, bundles_dir is not used)",
    )

    parser.add_argument(
        "--visualize_types",
        type=str,
        nargs="+",
        default=["unsafety", "idiomaticity", "tests"],
        help="which types to visualize",
    )

    parser.add_argument(
        "--max_workers",
        type=int,
        default=None,
        help="the maximum number of workers for parallel actions",
    )

    parser.add_argument(
        "--failed_file",
        type=Path,
        default=Path("process_all.failed"),
        help="the file to log failed actions",
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=3600,
        help="timeout (sec) of a worker",
    )

    return parser


def main():
    args = get_parser().parse_args()
    args.failed_file.unlink(missing_ok=True)

    action2class = {
        "translate": Translate,
        "aggregate": Aggregate,
        "visualize": Visualize,
    }

    start = time.time()

    aclass = action2class.get(args.action)
    if aclass is not None:
        aclass(args).process()
    else:
        logger.error(f"Unknown action: {args.action}")

    end = time.time()
    logger.info(f"Took {end - start:.2f} sec")


if __name__ == "__main__":
    main()
