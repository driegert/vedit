"""Declarative video editing on top of auto-editor + ffmpeg."""

__version__ = "0.1.0"


class VeditError(Exception):
    """A user-facing error: message is safe to show verbatim to an agent."""
