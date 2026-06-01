from __future__ import annotations

from html import escape
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ASSET_DIR = ROOT / "docs" / "assets"

WIDTH = 900
HEIGHT = 420
FONT = "Arial, Helvetica, sans-serif"
MONO_FONT = "Consolas, Menlo, monospace"


def wrap_text(text: str, max_chars: int) -> list[str]:
    """Wrap text by character count for predictable SVG layout."""
    lines: list[str] = []
    for raw_line in text.splitlines():
        if not raw_line:
            lines.append("")
            continue
        start = 0
        while start < len(raw_line):
            lines.append(raw_line[start : start + max_chars])
            start += max_chars
    return lines or [""]


def text_line(
    x: int,
    y: int,
    text: str,
    *,
    size: int = 15,
    color: str = "#172033",
    weight: int | None = None,
    family: str = FONT,
) -> str:
    weight_attr = f' font-weight="{weight}"' if weight else ""
    return (
        f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}"'
        f'{weight_attr} fill="{color}">{escape(text)}</text>'
    )


def rect(
    x: int,
    y: int,
    width: int,
    height: int,
    *,
    rx: int = 16,
    fill: str = "#ffffff",
    stroke: str = "#d8e1ec",
) -> str:
    return (
        f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="{rx}" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="1"/>'
    )


def badge(x: int, y: int, label: str, *, fill: str, color: str = "#172033") -> str:
    width = max(86, len(label) * 8 + 24)
    return "\n".join(
        [
            rect(x, y, width, 28, rx=14, fill=fill, stroke="none"),
            text_line(x + 12, y + 19, label, size=13, color=color, weight=700),
        ]
    )


def labeled_box(
    x: int,
    y: int,
    width: int,
    title: str,
    body: list[str],
    *,
    fill: str = "#f8fafc",
    mono: bool = False,
    max_chars: int = 42,
) -> tuple[str, int]:
    body_lines: list[str] = []
    for item in body:
        body_lines.extend(wrap_text(item, max_chars))

    line_height = 22
    height = 58 + len(body_lines) * line_height
    parts = [
        rect(x, y, width, height, rx=18, fill=fill),
        text_line(x + 22, y + 27, title, size=13, color="#64748b", weight=700),
    ]
    family = MONO_FONT if mono else FONT
    text_y = y + 58
    for line in body_lines:
        parts.append(text_line(x + 22, text_y, line, size=15, family=family))
        text_y += line_height
    return "\n".join(parts), height


def outcome_box(
    x: int,
    y: int,
    width: int,
    title: str,
    lines: list[str],
    badges: list[tuple[str, str]],
) -> str:
    parts = [
        rect(x, y, width, 116, rx=18, fill="#ffffff"),
        text_line(x + 22, y + 30, title, size=13, color="#64748b", weight=700),
    ]
    text_y = y + 59
    for line in lines:
        parts.append(text_line(x + 22, text_y, line, size=15))
        text_y += 22

    badge_x = x + width - 22
    rendered_badges: list[str] = []
    for label, fill in reversed(badges):
        badge_width = max(86, len(label) * 8 + 24)
        badge_x -= badge_width
        rendered_badges.append(badge(badge_x, y + 16, label, fill=fill))
        badge_x -= 10
    parts.extend(reversed(rendered_badges))
    return "\n".join(parts)


def card_shell(title: str, subtitle: str, body: str) -> str:
    return "\n".join(
        [
            (
                f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" '
                f'height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}" '
                f'role="img" aria-label="{escape(title)}">'
            ),
            '<rect x="0" y="0" width="900" height="420" fill="#eef3f8"/>',
            rect(24, 24, 852, 372, rx=28, fill="#ffffff", stroke="#cfd9e6"),
            text_line(52, 64, title, size=24, weight=700),
            text_line(52, 90, subtitle, size=14, color="#64748b"),
            text_line(
                52,
                114,
                "Generated mock preview. No real chat data.",
                size=13,
                color="#64748b",
            ),
            body,
            "</svg>",
        ]
    )


def demo_run_status() -> str:
    left, _ = labeled_box(
        52,
        142,
        380,
        "User command",
        ["/run main summarize project"],
        fill="#ecfdf5",
        mono=True,
        max_chars=36,
    )
    right, _ = labeled_box(
        468,
        142,
        380,
        "Bot response",
        [
            "🚀 Job started",
            "Job: job_20260601_120000",
            "Project: main",
        ],
        fill="#f8fafc",
        max_chars=38,
    )
    outcome = outcome_box(
        52,
        288,
        796,
        "Outcome",
        ["The controller tracks the job and delivers the final report."],
        [
            ("running", "#dbeafe"),
            ("finished", "#dcfce7"),
            ("report attached", "#fef3c7"),
        ],
    )
    return card_shell(
        "Telegram demo preview",
        "Run a read-only task, check status, and receive completion feedback.",
        "\n".join([left, right, outcome]),
    )


def demo_approval_states() -> str:
    left, _ = labeled_box(
        52,
        142,
        380,
        "User command",
        ["/run main update README"],
        fill="#ecfdf5",
        mono=True,
        max_chars=36,
    )
    right, _ = labeled_box(
        468,
        142,
        380,
        "Bot response",
        [
            "⚠️ Approval required",
            "Risk: write_workspace",
            "Options: /approve or /deny",
        ],
        fill="#fff7ed",
        max_chars=38,
    )
    outcome = outcome_box(
        52,
        288,
        796,
        "Outcome",
        [
            "blocked_repo_dirty",
            "If the repo is dirty, approval is blocked until the user chooses.",
            "Options: /approve_anyway or /deny",
        ],
        [
            ("pending approval", "#fef3c7"),
            ("blocked_repo_dirty", "#fee2e2"),
            ("explicit override", "#e0e7ff"),
        ],
    )
    return card_shell(
        "Mock workflow preview",
        "Write tasks require one approval and dirty repos require explicit override.",
        "\n".join([left, right, outcome]),
    )


def demo_report_delivery() -> str:
    left, _ = labeled_box(
        52,
        142,
        380,
        "User command",
        ["Job finished"],
        fill="#ecfdf5",
        max_chars=36,
    )
    right, _ = labeled_box(
        468,
        142,
        380,
        "Bot response",
        [
            "Telegram summary message",
            "Attached files are sent back",
            "for mobile review.",
        ],
        fill="#f8fafc",
        max_chars=38,
    )
    outcome = outcome_box(
        52,
        288,
        796,
        "Outcome",
        [
            "Attached files: report_telegram.md, stdout_tail.log,",
            "artifact_manifest.json, figure_preview.png",
        ],
        [
            ("artifacts", "#dbeafe"),
            ("success", "#dcfce7"),
            ("mobile report", "#fef3c7"),
        ],
    )
    return card_shell(
        "Telegram demo preview",
        "Reports, logs, and artifacts are delivered as Telegram files.",
        "\n".join([left, right, outcome]),
    )


def write_svg(name: str, content: str) -> None:
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    (ASSET_DIR / name).write_bytes((content + "\n").encode("utf-8"))


def main() -> int:
    write_svg("demo_run_status.svg", demo_run_status())
    write_svg("demo_approval_states.svg", demo_approval_states())
    write_svg("demo_report_delivery.svg", demo_report_delivery())
    print(f"Generated demo assets in {ASSET_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
