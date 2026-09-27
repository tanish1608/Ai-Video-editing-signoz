"""Project-scoped creative preferences, independent of transient pipeline sessions."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from kinetograph.config import settings


class EditingOptions(BaseModel):
    editing_mode: Literal["narration", "highlights"] = "narration"
    audio_provider: Literal["elevenlabs", "soundstripe", "none"] = "elevenlabs"
    sound_effects_enabled: bool = True
    caption_style_id: str = "bold-yellow"


def load_options() -> EditingOptions:
    path = settings.state_dir / "editing_options.json"
    if not path.exists():
        return EditingOptions()
    return EditingOptions.model_validate_json(path.read_text())


def save_options(options: EditingOptions) -> None:
    path = settings.state_dir / "editing_options.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(options.model_dump_json(indent=2))
    temporary.replace(path)


def caption_style(style_id: str) -> dict:
    from kinetograph.core.captions import CAPTION_STYLE_PRESETS

    if style_id == "none":
        return {"id": "none", "name": "No captions"}
    if style_id not in CAPTION_STYLE_PRESETS:
        raise ValueError(f"Unknown caption style: {style_id}")
    return CAPTION_STYLE_PRESETS[style_id]


def pipeline_options() -> dict:
    options = load_options()
    return {
        **options.model_dump(exclude={"caption_style_id"}),
        "caption_style": caption_style(options.caption_style_id),
    }
