"""Picks the adapter (Writer, Calc or Impress) for an open document."""

import importlib

# Impress is checked before the others only because order does not matter between
# the three: each one tests for its own document service.
_MODULES = ("writer", "calc", "impress")


def adapter_for(doc):
    """The adapter module for this document, or None for a kind we don't support."""
    if doc is None:
        return None
    for name in _MODULES:
        module = importlib.import_module("claude_lo." + name)
        if module.is_doc(doc):
            return module
    return None
