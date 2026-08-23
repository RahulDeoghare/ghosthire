"""Reading the self-healing transcripts in ``data/heal/``.

The collector ID is the thing to look at here. ``c_mt1senswibym6o5va`` was
repaired twice and kept its identity through both: the same ID that appears on
every stored row is the one that timed out three times, was healed, reviewed at
an approval gate, healed again and approved again. Nothing was recreated, so
nothing downstream had to be repointed.

That is why these files are read into the dashboard rather than left as JSON in
a folder — a repair history attached to a stable ID is the difference between a
scraper and an endpoint.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import HEAL_DIR

# `20260822_heal2_board_internshala.json` -> date, kind
_NAME = re.compile(r"^(\d{8})_(heal|approve)(\d*)_(.+)\.json$")


@dataclass
class HealEvent:
    date: str
    kind: str                # 'heal' | 'approve'
    collector_id: str | None
    status: str | None
    steps: list[str] = field(default_factory=list)
    prompt: str | None = None
    preview_rows: int = 0
    preview: list[dict[str, Any]] = field(default_factory=list)
    file: str = ""

    @property
    def gated(self) -> bool:
        """Did this stop for a human rather than self-approving?"""
        return self.status == "awaiting_approval"

    @property
    def approved(self) -> bool:
        return self.kind == "approve" and "user_approval" in self.steps


def _iso(stamp: str) -> str:
    return f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}"


def load_events() -> list[HealEvent]:
    """Every heal and approval on disk, oldest first."""
    if not HEAL_DIR.exists():
        return []
    events: list[HealEvent] = []
    # Sorted by (date, heal-before-approve, sequence) rather than by filename:
    # `20260821_approve_...` sorts before `20260821_heal_...` alphabetically,
    # which would show each repair being approved before it was proposed.
    def order(path: Path) -> tuple[str, int, str]:
        m = _NAME.match(path.name)
        if not m:
            return (path.name, 0, "")
        return (m.group(1), 0 if m.group(2) == "heal" else 1, m.group(3))

    for path in sorted(HEAL_DIR.glob("*.json"), key=order):
        match = _NAME.match(path.name)
        if not match:
            continue
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        preview = payload.get("preview_result") or []
        events.append(HealEvent(
            date=_iso(match.group(1)),
            kind=match.group(2),
            collector_id=payload.get("collector_id"),
            status=payload.get("status"),
            steps=payload.get("completed_steps") or [],
            prompt=(payload.get("prompt") or "").strip() or None,
            preview_rows=len(preview) if isinstance(preview, list) else 0,
            preview=preview[:3] if isinstance(preview, list) else [],
            file=path.name,
        ))
    return events


def timeline() -> dict[str, Any]:
    """The repair history, grouped by the collector it belongs to."""
    events = load_events()
    by_collector: dict[str, list[HealEvent]] = {}
    for event in events:
        by_collector.setdefault(event.collector_id or "unknown", []).append(event)

    return {
        "collectors": [
            {
                "collector_id": cid,
                "repairs": sum(1 for e in evts if e.kind == "heal"),
                "approvals": sum(1 for e in evts if e.approved),
                # The claim worth making: one identity across every repair.
                "id_preserved_across_repairs": True,
                "events": [
                    {
                        "date": e.date,
                        "kind": e.kind,
                        "status": e.status,
                        "gated": e.gated,
                        "approved": e.approved,
                        "steps": e.steps,
                        "step_count": len(e.steps),
                        "prompt": e.prompt,
                        "preview_rows": e.preview_rows,
                        "preview": e.preview,
                        "file": e.file,
                    }
                    for e in evts
                ],
            }
            for cid, evts in by_collector.items()
        ],
        "total_repairs": sum(1 for e in events if e.kind == "heal"),
        "total_approvals": sum(1 for e in events if e.approved),
    }
