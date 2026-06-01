from __future__ import annotations

from dataclasses import dataclass
from html import escape
from pathlib import Path
from textwrap import wrap


ROOT = Path(__file__).resolve().parents[1]
ASSET_DIR = ROOT / "docs" / "assets"

WIDTH = 420
HEIGHT = 760
PADDING = 22
PHONE_X = 48
PHONE_Y = 28
PHONE_W = 324
PHONE_H = 704
CHAT_X = PHONE_X + 18
CHAT_W = PHONE_W - 36
FONT = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"


@dataclass(frozen=True)
class Message:
    text: str
    side: str = "left"
    color: str = "#ffffff"
    mono: bool = False


def text_lines(text: str, max_chars: int) -> list[str]:
    lines: list[str] = []
    for raw_line in text.splitlines():
        if not raw_line:
            lines.append("")
            continue
        lines.extend(wrap(raw_line, width=max_chars) or [""])
    return lines


def svg_text(lines: list[str], x: int, y: int, size: int, color: str) -> str:
    spans = []
    for index, line in enumerate(lines):
        dy = 0 if index == 0 else size + 4
        spans.append(
            f'<tspan x="{x}" dy="{dy}">{escape(line)}</tspan>'
        )
    return (
        f'<text font-family="{FONT}" font-size="{size}" '
        f'fill="{color}">' + "".join(spans) + "</text>"
    )


def bubble(message: Message, y: int) -> tuple[str, int]:
    max_chars = 32
    lines = text_lines(message.text, max_chars)
    text_w = min(CHAT_W - 34, max(72, max(len(line) for line in lines) * 7 + 22))
    line_h = 17
    height = max(38, 18 + len(lines) * line_h)
    if message.side == "right":
        x = CHAT_X + CHAT_W - text_w
        fill = "#dff6d7"
    else:
        x = CHAT_X
        fill = message.color
    text_x = x + 12
    text_y = y + 18
    rect = (
        f'<rect x="{x}" y="{y}" width="{text_w}" height="{height}" '
        f'rx="14" fill="{fill}" stroke="#d7dee8" stroke-width="1"/>'
    )
    text = svg_text(lines, text_x, text_y, 12, "#1f2937")
    return rect + text, y + height + 12


def phone_frame(title: str, subtitle: str, messages: list[Message]) -> str:
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}">',
        '<rect width="100%" height="100%" fill="#eef3f8"/>',
        f'<rect x="{PHONE_X}" y="{PHONE_Y}" width="{PHONE_W}" height="{PHONE_H}" '
        'rx="36" fill="#f8fafc" stroke="#cbd5e1" stroke-width="2"/>',
        f'<rect x="{PHONE_X + 88}" y="{PHONE_Y + 13}" width="148" height="20" '
        'rx="10" fill="#111827" opacity="0.88"/>',
        f'<rect x="{CHAT_X}" y="{PHONE_Y + 54}" width="{CHAT_W}" height="622" '
        'rx="22" fill="#e7eff7"/>',
        svg_text([title], CHAT_X + 14, PHONE_Y + 84, 14, "#0f172a"),
        svg_text([subtitle], CHAT_X + 14, PHONE_Y + 106, 10, "#64748b"),
    ]
    y = PHONE_Y + 128
    for message in messages:
        element, y = bubble(message, y)
        parts.append(element)
    parts.append(
        f'<rect x="{CHAT_X + 16}" y="{PHONE_Y + 648}" width="{CHAT_W - 32}" '
        'height="36" rx="18" fill="#ffffff" stroke="#d7dee8"/>'
    )
    parts.append(svg_text(["Message"], CHAT_X + 34, PHONE_Y + 671, 11, "#94a3b8"))
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def write_svg(name: str, title: str, subtitle: str, messages: list[Message]) -> None:
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    (ASSET_DIR / name).write_text(
        phone_frame(title, subtitle, messages),
        encoding="utf-8",
    )


def main() -> int:
    write_svg(
        "demo_run_status.svg",
        "Telegram demo preview",
        "Mock workflow preview - not a real chat screenshot",
        [
            Message("/run main inspect project and report", "right", mono=True),
            Message(
                "running\nCodex job started.\nJob: job_20260602_101500\n"
                "Project: main"
            ),
            Message("/status", "right", mono=True),
            Message(
                "running\nJob: job_20260602_101500\nProject: main\n"
                "Risk: read_only\nRuntime: 183s"
            ),
            Message(
                "success\nCodex job finished\nReport attached.",
                color="#f8fbff",
            ),
        ],
    )
    write_svg(
        "demo_approval_states.svg",
        "Mock workflow preview",
        "Approval state machine with a dirty repo block",
        [
            Message("/run main update benchmark config", "right", mono=True),
            Message(
                "pending_approval\nJob has NOT started.\nOptions:\n"
                "/approve\n/deny"
            ),
            Message("/approve", "right", mono=True),
            Message(
                "blocked_repo_dirty\nJob has NOT started.\nReason:\n"
                "repository has uncommitted changes\nOptions:\n"
                "/approve_anyway\n/deny",
                color="#fff7ed",
            ),
            Message("/approve_anyway", "right", mono=True),
            Message("running\nJob: job_20260602_102300\nRisk: write_workspace"),
        ],
    )
    write_svg(
        "demo_report_delivery.svg",
        "Telegram demo preview",
        "Generated mock preview - no real files or account data",
        [
            Message(
                "success\nJob: job_20260602_103000\nArtifacts ready.\n"
                "Report attached."
            ),
            Message("report_telegram.md", color="#ffffff"),
            Message("stdout_tail.log", color="#ffffff"),
            Message("artifact_manifest.json", color="#ffffff"),
            Message(
                "Summary\n- 3 files delivered\n- mobile report generated\n"
                "- no public port opened",
                color="#f8fbff",
            ),
        ],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
