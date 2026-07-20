from __future__ import annotations

from dataclasses import dataclass
from typing import Any


DEFAULT_SUBJECT_LABEL = "product"
DEFAULT_SUBJECT_PLACEMENT_HINT = "naturally held in hand or placed on surface"


@dataclass(frozen=True)
class SubjectDescriptor:
    label: str = DEFAULT_SUBJECT_LABEL
    placement_hint: str = DEFAULT_SUBJECT_PLACEMENT_HINT

    @classmethod
    def from_mapping(
        cls,
        project: dict[str, Any] | None = None,
        scene: dict[str, Any] | None = None,
    ) -> "SubjectDescriptor":
        project = project or {}
        scene = scene or {}
        label = str(
            scene.get("subject_label")
            or project.get("subject_label")
            or DEFAULT_SUBJECT_LABEL
        ).strip()
        placement_hint = str(
            scene.get("subject_placement_hint")
            or project.get("subject_placement_hint")
            or DEFAULT_SUBJECT_PLACEMENT_HINT
        ).strip()
        return cls(
            label=label or DEFAULT_SUBJECT_LABEL,
            placement_hint=placement_hint or DEFAULT_SUBJECT_PLACEMENT_HINT,
        )

    @property
    def is_default(self) -> bool:
        return (
            self.label == DEFAULT_SUBJECT_LABEL
            and self.placement_hint == DEFAULT_SUBJECT_PLACEMENT_HINT
        )


def replace_product_placeholder(template: str, subject: SubjectDescriptor) -> str:
    return template.replace("[PRODUCT]", subject.label)
