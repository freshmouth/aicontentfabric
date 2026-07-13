from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class CharacterError(RuntimeError):
    pass


@dataclass(frozen=True)
class CharacterProfile:
    character_id: str
    name: str
    master_reference: str
    metadata_path: str
    age: str = ""
    role: str = ""
    ethnicity: str = ""
    hair: str = ""
    eyes: str = ""
    skin_tone: str = ""
    outfit: str = ""
    environment: str = ""
    voice: str = ""
    identity_bible: str = ""

    def prompt_identity(self) -> str:
        parts = [
            f"{self.name} is the on-camera person.",
            self.role,
            f"Age: {self.age}" if self.age else "",
            f"Ethnicity/appearance: {self.ethnicity}" if self.ethnicity else "",
            f"Hair: {self.hair}" if self.hair else "",
            f"Eyes: {self.eyes}" if self.eyes else "",
            f"Skin: {self.skin_tone}" if self.skin_tone else "",
            f"Outfit: {self.outfit}" if self.outfit else "",
            f"Environment: {self.environment}" if self.environment else "",
            f"Voice: {self.voice}" if self.voice else "",
            self.identity_bible,
        ]
        return " ".join(part.strip() for part in parts if part and part.strip())


def load_character(root: Path, config: dict[str, Any], concept: dict[str, Any]) -> CharacterProfile:
    character_id = str(
        concept.get("character_id")
        or config.get("default_character_id")
        or config.get("character_id")
        or "claire_natural"
    ).strip()
    if not character_id:
        raise CharacterError("character_id is required.")

    config_entry = dict((config.get("characters") or {}).get(character_id) or {})
    metadata_path = resolve_path(
        root,
        str(config_entry.get("metadata_path") or f"characters/{character_id}/character.json"),
    )
    file_entry: dict[str, Any] = {}
    if metadata_path.exists():
        file_entry = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
    elif not config_entry:
        raise CharacterError(f"Missing character metadata: {metadata_path}")

    merged = {**file_entry, **config_entry}
    master_reference = str(
        merged.get("master_reference")
        or merged.get("reference_image")
        or ("characters/claire_natural/master_reference.png" if character_id == "claire_natural" else f"characters/{character_id}/master_reference.png")
    )
    master_path = resolve_path(root, master_reference)
    if not master_path.exists() or master_path.stat().st_size <= 0:
        raise CharacterError(f"Missing master reference for {character_id}: {master_path}")

    return CharacterProfile(
        character_id=character_id,
        name=str(merged.get("name") or character_id.replace("_", " ").title()).strip(),
        master_reference=relative_or_absolute(root, master_path),
        metadata_path=relative_or_absolute(root, metadata_path),
        age=str(merged.get("age") or "").strip(),
        role=str(merged.get("role") or merged.get("style") or "").strip(),
        ethnicity=str(merged.get("ethnicity") or "").strip(),
        hair=str(merged.get("hair") or "").strip(),
        eyes=str(merged.get("eyes") or "").strip(),
        skin_tone=str(merged.get("skin_tone") or "").strip(),
        outfit=str(merged.get("outfit") or "").strip(),
        environment=str(merged.get("environment") or "").strip(),
        voice=str(merged.get("voice") or "").strip(),
        identity_bible=str(merged.get("identity_bible") or "").strip(),
    )


def character_text(text: str, character: CharacterProfile) -> str:
    if character.character_id == "claire_natural":
        return text
    return text.replace("Claire Natural", character.name).replace("Claire", character.name)


def resolve_path(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (root / path).resolve()


def relative_or_absolute(root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path)
