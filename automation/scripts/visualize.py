#!/usr/bin/env python3

import argparse
import json
from itertools import zip_longest

from bs4 import BeautifulSoup
from tabulate import tabulate, tabulate_formats


class Visualizer:
    extension = "any"

    def __init__(
        self,
        agg_dir: str,
        bundles: list[str],
        confs: list[str],
        out_file: str,
        tablefmt: str,
    ):
        self.out_file = out_file
        self.tablefmt = tablefmt
        self.dcts = {}

        for bundle in bundles:
            self.dcts[bundle] = []

            for conf in confs:
                file = f"{agg_dir}/{bundle}_{conf}.{self.extension}"
                with open(file, "rt") as fp:
                    dct = {"conf": conf}
                    dct.update(self.load_as_dict(fp))

                    self.dcts[bundle].append(dct)

    def load_as_dict(self, fp) -> dict:
        """Load item from file to json"""
        raise NotImplementedError()

    def output_tabulate(self, tab_dct, output=True):
        if not output:
            return

        output = tabulate(tab_dct, headers="keys", tablefmt=self.tablefmt) + "\n\n\n"

        if self.out_file == "stdout":
            print(output)
        else:
            with open(self.out_file, "at") as fp:
                fp.write(output)
                print(f"Appended to {self.out_file}")

    def tabulate_vertical(self, output=True) -> list[dict]:
        raise NotImplementedError()

    def _from_header_to_value(self, header) -> str:
        return "_".join(header.split())

    def _from_value_to_header(self, value) -> str:
        return value.replace("_", "\n")

    def tabulate_horizontal(self, output=True) -> list[dict]:
        vert_tab_dcts = self.tabulate_vertical(output=False)
        tab_dcts = []

        for vert_tab_dct in vert_tab_dcts:
            headers = list(vert_tab_dct.keys())
            rows = list(zip_longest(*vert_tab_dct.values(), fillvalue=0))
            tab_dct = {}

            first_header = headers[0]
            tab_dct[first_header] = []
            for header in headers[1:]:
                new_value = self._from_header_to_value(header)
                tab_dct[first_header].append(new_value)

            for row in rows:
                new_header = self._from_value_to_header(row[0])
                tab_dct[new_header] = list(row[1:])

            tab_dcts.append(tab_dct)
            self.output_tabulate(tab_dct, output)

        return tab_dcts

    def tabulate(self, conf_orient) -> list[dict]:
        if conf_orient == "vertical":
            return self.tabulate_vertical()
        elif conf_orient == "horizontal":
            return self.tabulate_horizontal()
        else:
            print(f"Unknown config orientation: {conf_orient}")


class JsonVisualizer(Visualizer):
    extension = "json"

    def load_as_dict(self, fp) -> dict:
        return json.load(fp)


class XmlVisualizer(Visualizer):
    extension = "xml"


class UnsafetyVisualizer(JsonVisualizer):
    tag = "[Unsafety]"

    def tabulate_vertical(self, output=True) -> list[dict]:
        tab_dcts = []

        for bundle, bundle_dcts in self.dcts.items():
            if not bundle_dcts:
                print(f"Skipping over empty bundle: {bundle}")
                continue

            tab_dct = {}
            first_header = f"{self.tag}\n{bundle}"

            for bundle_dct in bundle_dcts:
                for key, value in bundle_dct.items():
                    if key == "conf":
                        new_key = first_header
                    else:
                        key_parts = key.split("_")
                        break_idx = len(key_parts) // 2
                        new_key = ""

                        # intertwine parts and separators
                        for idx, part in enumerate(key_parts, 1):
                            sep = "\n" if idx == break_idx else " "
                            new_key += part + sep

                        # remove last sep
                        new_key = new_key[:-1]

                    tab_dct.setdefault(new_key, [])
                    tab_dct[new_key].append(value)

            tab_dcts.append(tab_dct)
            self.output_tabulate(tab_dct, output)

        return tab_dcts


class IdiomaticityVisualizer(JsonVisualizer):
    ccc_tag = "[CCC]"
    total_lints_tag = "[Total Lints]"
    lints_tag = "[Lints]"

    def _from_header_to_value(self, header):
        return header.replace("\n", " ")

    def _tabulate_ccc_vertical(self, output=True) -> list[dict]:
        tab_dcts = []

        for bundle, bundle_dcts in self.dcts.items():
            if not bundle_dcts:
                print(f"Skipping over empty bundle: {bundle}")
                continue

            tab_dct = {}
            first_header = f"{self.ccc_tag}\n{bundle}"

            for bundle_dct in bundle_dcts:
                tab_dct.setdefault(first_header, [])
                tab_dct[first_header].append(bundle_dct["conf"])

                ccc_dct = bundle_dct["cyclomatic_complexity_counts"]
                for key in ccc_dct.keys():
                    tab_dct.setdefault(key, [])
                    tab_dct[key].append(ccc_dct[key])

            # fill empty cells
            max_col_len = len(tab_dct[first_header])
            for col in tab_dct.values():
                col_len = len(col)
                assert col_len <= max_col_len
                if col_len < max_col_len:
                    col.extend([0] * (max_col_len - col_len))

            # sort based on int headers
            first_col = tab_dct.pop(first_header)
            sorted_tab_dct = {first_header: first_col}
            for key in sorted(map(int, tab_dct.keys())):
                sorted_tab_dct[str(key)] = tab_dct[str(key)]

            tab_dcts.append(sorted_tab_dct)
            self.output_tabulate(sorted_tab_dct, output)

        return tab_dcts

    def _tabulate_total_lints_vertical(self, output=True) -> list[dict]:
        tab_dcts = []

        for bundle, bundle_dcts in self.dcts.items():
            if not bundle_dcts:
                print(f"Skipping over empty bundle: {bundle}")
                continue

            tab_dct = {}
            first_header = f"{self.total_lints_tag}\n{bundle}"

            for bundle_dct in bundle_dcts:
                tab_dct.setdefault(first_header, [])
                tab_dct[first_header].append(bundle_dct["conf"])

                for tool, inner_dct in bundle_dct["lints"].items():
                    for level, val_list in inner_dct.items():
                        header = f"{tool}\n{level}"
                        total_lints = val_list[0]
                        tab_dct.setdefault(header, [])
                        tab_dct[header].append(total_lints)

            # fill empty cells
            max_col_len = len(tab_dct[first_header])
            for col in tab_dct.values():
                col_len = len(col)
                assert col_len <= max_col_len
                if col_len < max_col_len:
                    col.extend([0] * (max_col_len - col_len))

            # sort reverse lexicographically
            first_col = tab_dct.pop(first_header)
            sorted_tab_dct = {first_header: first_col}
            for key in sorted(tab_dct.keys(), reverse=True):
                sorted_tab_dct[key] = tab_dct[key]

            tab_dcts.append(sorted_tab_dct)
            self.output_tabulate(sorted_tab_dct, output)

        return tab_dcts

    def _tabulate_lints_vertical(self, output=True):
        tab_dcts = []

        for bundle, bundle_dcts in self.dcts.items():
            if not bundle_dcts:
                print(f"Skipping over empty bundle: {bundle}")
                continue

            tab_dct = {}
            first_header = f"{self.lints_tag}\n{bundle}"

            for bundle_dct in bundle_dcts:
                tab_dct.setdefault(first_header, [])
                conf_idx = len(tab_dct[first_header])
                tab_dct[first_header].append(bundle_dct["conf"])

                for tool, inner_dct in bundle_dct["lints"].items():
                    for level, val_list in inner_dct.items():
                        for lint, num in val_list[1].items():
                            header = f"[{tool} {level}]\n{lint}"
                            tab_dct.setdefault(header, [])
                            col = tab_dct[header]
                            col.extend([0] * (conf_idx - len(col)))
                            col.append(num)

            # fill empty cells
            max_col_len = len(tab_dct[first_header])
            for col in tab_dct.values():
                col_len = len(col)
                assert col_len <= max_col_len
                if col_len < max_col_len:
                    col.extend([0] * (max_col_len - col_len))

            # sort reverse lexicographically
            first_col = tab_dct.pop(first_header)
            sorted_tab_dct = {first_header: first_col}
            for key in sorted(tab_dct.keys(), reverse=True):
                sorted_tab_dct[key] = tab_dct[key]

            tab_dcts.append(sorted_tab_dct)
            self.output_tabulate(sorted_tab_dct, output)

        return tab_dcts

    def tabulate_vertical(self, output=True) -> list[dict]:
        ccc_tab_dcts = self._tabulate_ccc_vertical(output)
        tlnt_tab_dcts = self._tabulate_total_lints_vertical(output)
        lnts_tab_dcts = self._tabulate_lints_vertical(output)
        return ccc_tab_dcts + tlnt_tab_dcts + lnts_tab_dcts


class TestsVisualizer(XmlVisualizer):
    total_tests_tag = "[Total Tests]"
    tests_tag = "[Tests]"

    name_attribute = "name"
    attributes = ["tests", "skipped", "failures", "errors"]

    def load_as_dict(self, fp) -> dict:
        xmlsoup = BeautifulSoup(fp, "xml")
        testsuites = xmlsoup.testsuites

        dct = {}

        # total name is Tests
        name = testsuites[self.name_attribute]
        dct.setdefault(name, {})
        for attr in self.attributes:
            dct[name][attr] = testsuites[attr]

        for testsuite in testsuites.find_all("testsuite"):
            # keep basename of the path name
            name = testsuite[self.name_attribute].split("/")[-1]
            dct.setdefault(name, {})
            for attr in self.attributes:
                dct[name][attr] = testsuite[attr]

        return dct

    def _from_header_to_value(self, header):
        return header

    def _tabulate_total_tests_vertical(self, output=True) -> list[dict]:
        tab_dcts = []

        for bundle, bundle_dcts in self.dcts.items():
            if not bundle_dcts:
                print(f"Skipping over empty bundle: {bundle}")
                continue

            tab_dct = {}
            first_header = f"{self.total_tests_tag}\n{bundle}"

            for bundle_dct in bundle_dcts:
                tab_dct.setdefault(first_header, [])
                tab_dct[first_header].append(bundle_dct["conf"])

                for attribute, value in bundle_dct["Tests"].items():
                    tab_dct.setdefault(attribute, [])
                    tab_dct[attribute].append(value)

            # fill empty cells
            max_col_len = len(tab_dct[first_header])
            for col in tab_dct.values():
                col_len = len(col)
                assert col_len <= max_col_len
                if col_len < max_col_len:
                    col.extend([0] * (max_col_len - col_len))

            tab_dcts.append(tab_dct)
            self.output_tabulate(tab_dct, output)

        return tab_dcts

    def _tabulate_tests_vertical(self, output=True) -> list[dict]:
        tab_dcts = []

        for bundle, bundle_dcts in self.dcts.items():
            if not bundle_dcts:
                print(f"Skipping over empty bundle: {bundle}")
                continue

            tab_dct = {}
            first_header = f"{self.tests_tag}\n{bundle}"

            for bundle_dct in bundle_dcts:
                tab_dct.setdefault(first_header, [])
                conf_idx = len(tab_dct[first_header])
                tab_dct[first_header].append(bundle_dct["conf"])

                for name, inner_dct in bundle_dct.items():
                    if name in ["conf", "Tests"]:
                        continue

                    for attribute, value in inner_dct.items():
                        header = f"[{name}] {attribute}"
                        tab_dct.setdefault(header, [])
                        col = tab_dct[header]
                        col.extend([0] * (conf_idx - len(col)))
                        col.append(value)

            # fill empty cells
            max_col_len = len(tab_dct[first_header])
            for col in tab_dct.values():
                col_len = len(col)
                assert col_len <= max_col_len
                if col_len < max_col_len:
                    col.extend([0] * (max_col_len - col_len))

            tab_dcts.append(tab_dct)
            self.output_tabulate(tab_dct, output)

        return tab_dcts

    def tabulate_vertical(self, output=True) -> list[dict]:
        total_tab_dcts = self._tabulate_total_tests_vertical(output)
        tests_tab_dcts = self._tabulate_tests_vertical(output)
        return total_tab_dcts + tests_tab_dcts


def get_parser():
    parser = argparse.ArgumentParser(
        usage="%(prog)s [options]",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        "--type",
        type=str,
        required=True,
        choices=("unsafety", "idiomaticity", "tests"),
        help="the type of json files to aggregate",
    )

    parser.add_argument(
        "--agg_dir",
        required=True,
        type=str,
        help="the directory of the aggregate files",
    )

    parser.add_argument(
        "--bundles",
        nargs="*",
        help="the bundle prefixes to consider, e.g. B01_organic",
    )

    parser.add_argument(
        "--confs",
        required=True,
        nargs="*",
        help="the configuration postfixes to consider",
    )

    parser.add_argument(
        "--out_file",
        type=str,
        required=True,
        help="the output file, use `stdout` for just printing",
    )

    parser.add_argument(
        "--tablefmt",
        type=str,
        default="rounded_grid",
        help=f"the tabulate tablefmt: {tabulate_formats}",
    )

    parser.add_argument(
        "--conf_orient",
        type=str,
        default="horizontal",
        choices=("horizontal", "vertical"),
        help="the orientation of configs",
    )

    return parser


if __name__ == "__main__":
    args = get_parser().parse_args()

    inputs = (
        args.agg_dir,
        args.bundles,
        args.confs,
        args.out_file,
        args.tablefmt,
    )

    if args.type == "unsafety":
        visualizer = UnsafetyVisualizer(*inputs)
        visualizer.tabulate(args.conf_orient)

    elif args.type == "idiomaticity":
        visualizer = IdiomaticityVisualizer(*inputs)
        visualizer.tabulate(args.conf_orient)

    elif args.type == "tests":
        visualizer = TestsVisualizer(*inputs)
        visualizer.tabulate(args.conf_orient)

    else:
        print(f"Unknown type: {args.type}")
