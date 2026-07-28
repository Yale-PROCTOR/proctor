# © 2026 Massachusetts Institute of Technology
# MIT License

from abc import ABC, abstractmethod
from typing import Optional, Mapping
from ..local_types import TestOutcome, TestCase


class TestCaseRunner(ABC):
    @abstractmethod
    def run_tests(test_case: TestCase, verbose: bool, env: Optional[Mapping[str, str]], timeout: Optional[float] = None) -> list[TestOutcome]:
        pass
