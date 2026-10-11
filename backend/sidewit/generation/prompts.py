"""Versioned templates shipped as sidewit package resources (string.Template syntax)."""

from functools import cache
from importlib.resources import files
from string import Template


@cache
def template(version: str) -> Template:
    return Template(
        files("sidewit").joinpath("prompts", f"{version}.md").read_text(encoding="utf-8")
    )
