"""View models for the guide card and step pill. Phase 1 feeds these from mock data."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Segment(_Frozen):
    """A run of instruction text, or a keyboard shortcut rendered as keycaps."""

    text: str = ""
    keys: tuple[str, ...] = ()  # e.g. ("Shift", "A"); empty for plain text


class StepView(_Frozen):
    title: str
    segments: tuple[Segment, ...]
    why: str = ""
    done: bool = False


class GuideView(_Frozen):
    goal: str
    steps: tuple[StepView, ...] = Field(min_length=1)
    current: int = Field(0, ge=0)

    @property
    def total(self) -> int:
        return len(self.steps)

    @property
    def progress_label(self) -> str:
        return f"Step {min(self.current, self.total - 1) + 1} of {self.total}"

    def to_qml(self) -> dict[str, object]:
        """Plain data for QML: no pydantic objects cross the boundary."""
        return {
            "goal": self.goal,
            "current": self.current,
            "total": self.total,
            "progressLabel": self.progress_label,
            "steps": [
                {
                    "title": s.title,
                    "why": s.why,
                    "done": s.done,
                    "segments": [{"text": g.text, "keys": list(g.keys)} for g in s.segments],
                }
                for s in self.steps
            ],
        }


def mock_guide() -> GuideView:
    """Sample guide used until the brain exists (Phase 1)."""

    def t(text: str) -> Segment:
        return Segment(text=text)

    def k(*keys: str) -> Segment:
        return Segment(keys=keys)

    return GuideView(
        goal="Sample guide: a glossy cube in Blender",
        current=1,
        steps=(
            StepView(
                title="Add a cube",
                segments=(t("Press "), k("Shift", "A"), t(" and choose Mesh → Cube.")),
                why="Shift+A opens the Add menu wherever your cursor is in the viewport.",
                done=True,
            ),
            StepView(
                title="Open the Material tab",
                segments=(t("In the Properties panel, click the red sphere icon."),),
                why="Materials decide how a surface reacts to light.",
            ),
            StepView(
                title="Make it glossy",
                segments=(t("Set Roughness to 0.1 and Metallic to 1.0."),),
                why="Low roughness means sharp reflections.",
            ),
            StepView(
                title="Switch to Cycles",
                segments=(t("In Render Properties, set Render Engine to Cycles."),),
                why="Cycles traces light paths, so reflections are physically accurate.",
            ),
            StepView(
                title="Render",
                segments=(t("Press "), k("F12"), t(" to render the image.")),
                why="F12 renders the current frame from the active camera.",
            ),
        ),
    )
