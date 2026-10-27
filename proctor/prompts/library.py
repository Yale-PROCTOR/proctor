"""Reusable prompt library (plan §5.3).

Templates are Markdown files with TOML frontmatter between ``+++``
fences. Rendering is Jinja2 with ``StrictUndefined`` — a missing
variable is an error, never a silent empty string. Reproducibility
comes from content hashes: the template hash is stable per version and
recorded (with the rendered-prompt hash) on every usage record.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomllib
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from jinja2 import exceptions as jinja_exceptions


class PromptError(ValueError):
    """A template is malformed, missing, or rendered with bad params."""


@dataclass(frozen=True)
class RenderedPrompt:
    id: str
    version: int
    text: str
    content_hash: str  # sha256 of the rendered text


@dataclass(frozen=True)
class PromptTemplate:
    id: str
    version: int
    description: str
    variables: tuple[str, ...]
    body: str
    source: Path

    @property
    def template_hash(self) -> str:
        return hashlib.sha256(self.body.encode()).hexdigest()

    def render(self, **params: Any) -> RenderedPrompt:
        unknown = set(params) - set(self.variables)
        if self.variables and unknown:
            raise PromptError(
                f"prompt '{self.id}' got unknown variables: {sorted(unknown)}"
            )
        env = Environment(
            loader=FileSystemLoader(self.source.parent),
            undefined=StrictUndefined,
            keep_trailing_newline=True,
        )
        try:
            text = env.from_string(self.body).render(**params)
        except jinja_exceptions.TemplateError as exc:
            raise PromptError(f"prompt '{self.id}' v{self.version}: {exc}") from exc
        return RenderedPrompt(
            id=self.id,
            version=self.version,
            text=text,
            content_hash=hashlib.sha256(text.encode()).hexdigest(),
        )


def _parse_template(file: Path) -> PromptTemplate:
    raw = file.read_text(encoding="utf-8")
    if not raw.startswith("+++"):
        raise PromptError(f"{file}: missing +++ frontmatter fence")
    try:
        _, frontmatter, body = raw.split("+++", 2)
    except ValueError:
        raise PromptError(f"{file}: unterminated +++ frontmatter fence") from None
    try:
        meta = tomllib.loads(frontmatter)
    except tomllib.TOMLDecodeError as exc:
        raise PromptError(f"{file}: bad frontmatter TOML: {exc}") from exc

    prompt_id = meta.get("id")
    if not isinstance(prompt_id, str) or not prompt_id:
        raise PromptError(f"{file}: frontmatter must set a non-empty 'id'")
    version = meta.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise PromptError(f"{file}: 'version' must be a positive integer")
    description = meta.get("description", "")
    if not isinstance(description, str):
        raise PromptError(f"{file}: 'description' must be a string")
    variables_raw = meta.get("variables", [])
    if not isinstance(variables_raw, list) or not all(
        isinstance(v, str) for v in variables_raw
    ):
        raise PromptError(f"{file}: 'variables' must be an array of strings")

    return PromptTemplate(
        id=prompt_id,
        version=version,
        description=description,
        variables=tuple(variables_raw),
        body=body.lstrip("\n"),
        source=file,
    )


class PromptLibrary:
    """Indexes every ``*.md`` under a directory by (id, version).

    Multiple versions of one id may coexist as separate files;
    ``get(id)`` returns the latest, ``get(id, version=n)`` pins one.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._templates: dict[tuple[str, int], PromptTemplate] = {}
        if not directory.is_dir():
            raise PromptError(f"prompt library directory {directory} does not exist")
        for file in sorted(directory.glob("*.md")):
            template = _parse_template(file)
            key = (template.id, template.version)
            if key in self._templates:
                raise PromptError(
                    f"duplicate prompt {template.id!r} v{template.version} "
                    f"({file} and {self._templates[key].source})"
                )
            self._templates[key] = template

    def ids(self) -> list[str]:
        return sorted({prompt_id for prompt_id, _ in self._templates})

    def versions(self, prompt_id: str) -> list[int]:
        return sorted(v for pid, v in self._templates if pid == prompt_id)

    def get(self, prompt_id: str, version: int | None = None) -> PromptTemplate:
        versions = self.versions(prompt_id)
        if not versions:
            raise PromptError(f"unknown prompt {prompt_id!r}; available: {self.ids()}")
        chosen = version if version is not None else versions[-1]
        template = self._templates.get((prompt_id, chosen))
        if template is None:
            raise PromptError(
                f"prompt {prompt_id!r} has no version {chosen}; available: {versions}"
            )
        return template
