"""Adapted from agent/src/agent/clippy_lint_map.py"""

import json
import logging
from os import getenv
from pathlib import Path

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


class ClippyLintMap:
    DEFAULT_URL = "https://rust-lang.github.io/rust-clippy/master/index.html"
    CACHE_DIR = Path(__file__).resolve().parent / ".cache"
    JSON_DIR = Path(getenv("CLIPPY_CONF_DIR", CACHE_DIR))

    GROUP_TO_LINT_FILE = JSON_DIR / "clippy_group_to_lint.json"
    LINT_TO_GROUP_FILE = JSON_DIR / "clippy_lint_to_group.json"

    def __init__(
        self,
        url: str = DEFAULT_URL,
        group_to_lint_file=GROUP_TO_LINT_FILE,
        lint_to_group_file=LINT_TO_GROUP_FILE,
    ):
        self.url = url
        self.group_to_lint_file = group_to_lint_file
        self.lint_to_group_file = lint_to_group_file

    def load_lint_to_group(self) -> dict:
        logger.debug(f"Loading {self.LINT_TO_GROUP_FILE}")
        return self._load_lint_map(self.LINT_TO_GROUP_FILE)

    def load_group_to_lint(self) -> dict:
        logger.debug(f"Loading {self.GROUP_TO_LINT_FILE}")
        return self._load_lint_map(self.GROUP_TO_LINT_FILE)

    def _load_lint_map(self, file: Path) -> dict:
        if not file.exists():
            ok = self.create_clippy_maps()
            if not ok:
                logger.critical("Could not create clippy maps")
                return {}

        with file.open("r") as fp:
            try:
                return json.load(fp)
            except Exception as e:
                logger.error("Invalid stored clippy map", exc_info=e)
                return {}

    def _fetch_content(self) -> BeautifulSoup | None:
        resp = requests.get(self.url)
        resp.raise_for_status()
        content = resp.text
        soup = None

        try:
            soup = BeautifulSoup(content, "html.parser")
            logger.info(f"Fetched {self.url}")
        except Exception as e:
            logger.exception(f"During fetching of {self.url}", exc_info=e)

        return soup

    def create_clippy_maps(self):
        soup = self._fetch_content()
        if not soup:
            return False

        group_to_lint = {}

        for article in soup.find_all("article"):
            lint = str(article["id"]).strip()

            lint_group = article.find("span", class_="lint-group")
            if not lint_group:
                logger.warning(f"No lint group found for {lint}")
                continue

            group = lint_group.text.strip()

            past_names = []
            past_names_heading = article.find(name="h3", string="Past names")  # type: ignore
            if past_names_heading:
                ul_tag = past_names_heading.find_next_sibling("ul")
                if ul_tag:
                    li_items = ul_tag.find_all("li")
                    for li in li_items:
                        past_names.append(li.get_text(strip=True))

            group_to_lint.setdefault(group, [])
            group_to_lint[group].extend([lint] + past_names)

        lint_to_group = {}
        for group, lints in group_to_lint.items():
            for lint in lints:
                lint_to_group[lint] = group

        for dct, file in [
            (group_to_lint, self.group_to_lint_file),
            (lint_to_group, self.lint_to_group_file),
        ]:
            file.parent.mkdir(parents=True, exist_ok=True)
            with file.open("wt") as fd:
                json.dump(dct, fd, indent=2)
                logger.info(f"Created {file}")

        return True
