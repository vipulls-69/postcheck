"""v1 only — see CLAUDE.md.

Will screenshot each interaction step and ask Claude to judge whether the
rendered UI matches the predicted state, after running the screenshot
through the PII redaction pipeline. Stub in v0 so imports don't break for
forward-looking tooling.
"""
from __future__ import annotations


class VisualJudgeProbe:
    """v1 only — see CLAUDE.md."""

    name = "ui"

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise NotImplementedError("v1 only — see CLAUDE.md")


__all__ = ["VisualJudgeProbe"]
