"""Enough of Fusion's `adsk` package to import the add-in outside Fusion.

The real one exists only inside Fusion's embedded interpreter, so without this
the bridge — the largest and most security-sensitive file in the project —
cannot be tested at all. This stub is deliberately minimal: it exists to let the
module import and its HTTP, auth, concurrency and document-identity logic run,
NOT to simulate Fusion. Anything that genuinely needs Fusion is exercised
against the real application instead.
"""

from . import core, fusion

__all__ = ["core", "fusion"]


def doEvents():          # noqa: N802 — matches Autodesk's spelling
    """Present so an accidental call is a no-op rather than an AttributeError."""
