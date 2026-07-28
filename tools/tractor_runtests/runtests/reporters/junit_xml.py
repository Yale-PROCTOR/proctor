# © 2026 Massachusetts Institute of Technology
# MIT License

import xml.etree.ElementTree as ET
from pathlib import Path
from ..local_types import JUnitCase


def write_junit_xml(report_path: Path, suites: dict[str, list[JUnitCase]]) -> None:
    """
    suites: { suite_name -> [ JUnitCase, ... ] }
    - ok=True, skipped=False, error=False    -> Pass
    - ok=False, Skipped = False, error=False -> Failure
    - error=True                             -> Error
    - skipped=True                           -> Skipped
    """
    report_path.parent.mkdir(parents=True, exist_ok=True)

    root = ET.Element("testsuites", name="Tests")
    root_total = 0
    root_errors = 0
    root_skipped = 0
    root_failures = 0
    for suite_name, vectors in suites.items():
        total = len(vectors)
        errors = sum(1 for v in vectors if v.error)
        skipped = sum(1 for v in vectors if v.skipped)
        failures = sum(
            1 for v in vectors if (not v.skipped and not v.error and not v.ok)
        )

        root_total += total
        root_errors += errors
        root_skipped += skipped
        root_failures += failures

        ts = ET.SubElement(
            root,
            "testsuite",
            name=str(suite_name),
            tests=str(total),
            failures=str(failures),
            errors=str(errors),
            skipped=str(skipped),
        )

        for v in vectors:
            tc = ET.SubElement(
                ts, "testcase", name=str(v.name), classname=str(suite_name)
            )
            if v.skipped:
                ET.SubElement(tc, "skipped", message=v.message or "skipped")
            elif v.error:
                ET.SubElement(tc, "error", message=v.message or "error")
            elif not v.ok:
                ET.SubElement(tc, "failure", message=v.message or "failed")

    root.set("tests", str(root_total))
    root.set("errors", str(root_errors))
    root.set("skipped", str(root_skipped))
    root.set("failures", str(root_failures))
    # TODO: Have more fine-grained data in the junit xml
    tree = ET.ElementTree(root)
    ET.indent(tree, space="    ", level=0)
    tree.write(report_path, encoding="utf-8", xml_declaration=True)
