"""``VisualJudgeProbe`` is a v1-only stub — instantiating must raise."""
from __future__ import annotations

import pytest

from postcheck.probes.ui_probe.visual_judge import VisualJudgeProbe


def test_visual_judge_is_stub():
    with pytest.raises(NotImplementedError, match="v1 only"):
        VisualJudgeProbe()
