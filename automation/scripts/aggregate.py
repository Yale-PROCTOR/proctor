#!/usr/bin/env python3

import argparse
import json
from pathlib import Path

from bs4 import BeautifulSoup


class Aggregator:
    output_extension = "any"

    def __init__(self, bundle_path, file_pattern, output_prefix):
        self.bundle_path = bundle_path
        self.file_pattern = file_pattern
        self.output_txt = f"{output_prefix}.txt"
        self.output_file = f"{output_prefix}.{self.output_extension}"

    def item_merge_in_place(self, target, source):
        """Modify target in place"""
        raise NotImplementedError()

    def item_load(self, fp):
        """Load item from file"""
        raise NotImplementedError()

    def item_dumps(self, item: any) -> str:
        raise NotImplementedError()

    def aggregate(self):
        merged_item = None
        count = 0

        with open(self.output_txt, "wt") as ofp:
            for target_file in self.bundle_path.glob(self.file_pattern):
                with open(target_file, "rt") as ifp:
                    item = self.item_load(ifp)
                    count += 1

                    ofp.write(f"File: {target_file}\n")
                    ofp.write(self.item_dumps(item) + "\n\n")

                    if not merged_item:
                        merged_item = item
                    else:
                        self.item_merge_in_place(merged_item, item)

        print(f"Processed {count} files in {self.bundle_path}")
        if not merged_item:
            return

        with open(self.output_txt, "at") as fp:
            fp.write("Merged:\n")
            fp.write(self.item_dumps(merged_item) + "\n")
            print(f"Generated {self.output_txt}")

        with open(self.output_file, "wt") as fp:
            fp.write(self.item_dumps(merged_item) + "\n")
            print(f"Generated {self.output_file}\n")


class JsonAggregator(Aggregator):
    output_extension = "json"

    def item_load(self, fp):
        return json.load(fp)

    def item_dumps(self, dct):
        return json.dumps(dct, indent=2)


class XmlAggregator(Aggregator):
    output_extension = "xml"

    def item_load(self, fp):
        return BeautifulSoup(fp, "xml")

    def item_dumps(self, xmlsoup):
        return xmlsoup.prettify()


class UnsafetyAggregator(JsonAggregator):
    def item_merge_in_place(self, target_dct, source_dct):
        for k, v in source_dct.items():
            target_dct.setdefault(k, 0)
            target_dct[k] += v


class IdiomaticityAggregator(JsonAggregator):
    def item_merge_in_place(self, target_dct, source_dct):
        for k, v in source_dct["cyclomatic_complexity_counts"].items():
            target_ccc = target_dct["cyclomatic_complexity_counts"]
            target_ccc.setdefault(k, 0)
            target_ccc[k] += v

        target_lints = target_dct["lints"]
        source_lints = source_dct["lints"]

        for tool, inner_dct in source_lints.items():
            for level in inner_dct.keys():
                target_lints[tool].setdefault(level, [0, {}])
                target_lints[tool][level][0] += source_lints[tool][level][0]

                for k, v in source_lints[tool][level][1].items():
                    target_lints[tool][level][1].setdefault(k, 0)
                    target_lints[tool][level][1][k] += v


class TestsAggregator(XmlAggregator):
    def item_merge_in_place(self, target_xmlsoup, source_xmlsoup):
        attributes = ["errors", "failures", "skipped", "tests"]
        target_testsuites = target_xmlsoup.testsuites
        source_testsuite = source_xmlsoup.testsuite

        target_testsuites.append(source_testsuite)
        target_testsuites.append("\n")

        for attr in attributes:
            target_val = int(target_testsuites[attr])
            source_val = int(source_testsuite[attr])

            target_testsuites[attr] = target_val + source_val


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
        "--bundle_path",
        required=True,
        type=Path,
        help="the directory of the bundles, e.g. Public-Tests/B01_organic",
    )

    parser.add_argument(
        "--file_pattern",
        required=True,
        type=str,
        help="the file pattern to search for measurement files",
    )

    parser.add_argument(
        "--out_prefix",
        type=str,
        required=True,
        help="the prefix of the output files without a file extension",
    )

    return parser


if __name__ == "__main__":
    args = get_parser().parse_args()

    inputs = (args.bundle_path, args.file_pattern, args.out_prefix)

    if args.type == "unsafety":
        aggregator = UnsafetyAggregator(*inputs)
        aggregator.aggregate()

    elif args.type == "idiomaticity":
        aggregator = IdiomaticityAggregator(*inputs)
        aggregator.aggregate()

    elif args.type == "tests":
        aggregator = TestsAggregator(*inputs)
        aggregator.aggregate()

    else:
        print(f"Unknown type: {args.type}")
