import json

import requests
from bs4 import BeautifulSoup

url = "https://rust-lang.github.io/rust-clippy/master/index.html"
resp = requests.get(url)
resp.raise_for_status()
content = resp.text

soup = BeautifulSoup(content, "html.parser")

group_to_lint_file = "clippy_group_to_lint.json"
group_to_lint = {}
for article in soup.find_all("article"):
    lint = article["id"].strip()

    lint_group = article.find("span", class_="lint-group")
    if not lint_group:
        print(f"No lint-group found for {lint}")
        continue
    group = lint_group.text.strip()

    past_names = []
    past_names_heading = article.find("h3", string="Past names")
    if past_names_heading:
        ul_tag = past_names_heading.find_next_sibling("ul")
        if ul_tag:
            li_items = ul_tag.find_all("li")
            for li in li_items:
                past_names.append(li.get_text(strip=True))

    group_to_lint.setdefault(group, [])
    group_to_lint[group].extend([lint] + past_names)

lint_to_group_file = "clippy_lint_to_group.json"
lint_to_group = {}
for group, lints in group_to_lint.items():
    for lint in lints:
        lint_to_group[lint] = group

for dct, file in [
    (group_to_lint, group_to_lint_file),
    (lint_to_group, lint_to_group_file),
]:
    with open(file, "w") as fd:
        json.dump(dct, fd, indent=4)
        print(f"Created {file}")
