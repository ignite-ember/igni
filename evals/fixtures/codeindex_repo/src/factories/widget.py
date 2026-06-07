"""Widget factory — straightforward factory-pattern example.

Tagged ``patterns=[factory]``, ``quality=good``, ``testability=easy``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Widget:
    name: str
    color: str


@dataclass
class GearWidget(Widget):
    teeth: int


@dataclass
class ScrewWidget(Widget):
    thread: str


def make_widget(kind: str, **kwargs) -> Widget:
    """Factory dispatching to the right widget subclass."""
    if kind == "gear":
        return GearWidget(name=kwargs["name"], color=kwargs["color"], teeth=kwargs["teeth"])
    if kind == "screw":
        return ScrewWidget(name=kwargs["name"], color=kwargs["color"], thread=kwargs["thread"])
    return Widget(name=kwargs["name"], color=kwargs["color"])
