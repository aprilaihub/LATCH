"""Encode literal arguments for fixed backend Tcl scripts."""

from __future__ import annotations


def tcl_word(value: object) -> str:
    """Return one quoted Tcl word without substitutions.

    Double quotes preserve braces and whitespace; escaping Tcl's substitution
    characters also preserves command-looking filenames and backslashes.
    """
    escaped = str(value).replace("\\", "\\\\")
    for character in ('"', "$", "[", "]"):
        escaped = escaped.replace(character, "\\" + character)
    escaped = escaped.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return '"' + escaped + '"'
