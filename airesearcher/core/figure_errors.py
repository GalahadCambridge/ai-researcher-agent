"""Stable error types shared by installed and copied plotting scripts."""


class PresentationError(ValueError):
    """A figure presentation can be repaired without changing evidence."""


class UnitLabelError(PresentationError):
    """A figure label needs explicit unit clarification."""
