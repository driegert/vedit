"""A pixel rectangle, shared by `still` (what is drawn) and `ocr` (what was read)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    @property
    def centre(self) -> tuple[float, float]:
        return self.x + self.w / 2, self.y + self.h / 2

    def contains(self, other: "Rect") -> bool:
        return (self.x <= other.x and self.y <= other.y
                and other.right <= self.right and other.bottom <= self.bottom)

    def inset(self, by: int) -> "Rect":
        """The rectangle shrunk by `by` pixels on every side (never below 1x1)."""
        return Rect(self.x + by, self.y + by, max(1, self.w - 2 * by), max(1, self.h - 2 * by))

    def overlap(self, other: "Rect") -> float:
        """Fraction of *this* rectangle's area that lies inside `other`."""
        ix = max(0, min(self.right, other.right) - max(self.x, other.x))
        iy = max(0, min(self.bottom, other.bottom) - max(self.y, other.y))
        area = self.w * self.h
        return (ix * iy) / area if area else 0.0
