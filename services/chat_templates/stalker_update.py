from __future__ import annotations


def render(*, message: str) -> dict[str, object]:
    return {
        "title": "Voice Tracker",
        "description": message,
        "color": 0x5865F2,
        "footer": "Voice Tracker",
    }
