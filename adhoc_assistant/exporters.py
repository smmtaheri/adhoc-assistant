import calendar
import csv
import html
from datetime import date
from pathlib import Path

from .constants import (
    HTML_CALENDAR_WEEKDAYS,
    HTML_WEEKDAY_COLUMNS,
    WEEKDAY_NAMES,
)


DAY_PALETTE = [
    "#fff8df",
    "#edf7ff",
    "#effaf0",
    "#fff0f1",
    "#f4f1ff",
    "#eef8f6",
]


def print_terminal_calendar(schedule: list[dict], year: int, month: int) -> None:
    rows = build_calendar_rows(schedule, year, month)
    cell_width = 19
    separator = "-" * ((cell_width + 3) * len(HTML_CALENDAR_WEEKDAYS) - 3)

    print(f"Bug Day Schedule - {year}/{month:02d}")
    print(
        " | ".join(
            WEEKDAY_NAMES[weekday].center(cell_width)
            for weekday in HTML_CALENDAR_WEEKDAYS
        )
    )
    print(separator)

    for row in rows:
        day_line = []
        helper_line = []
        for item in row:
            if item is None:
                day_line.append(" " * cell_width)
                helper_line.append(" " * cell_width)
                continue

            day_number = date.fromisoformat(item["date"]).day
            owner = f"{day_number:02d} {item['main']}"
            helper = f"helper: {item['backup']}"
            day_line.append(owner[:cell_width].ljust(cell_width))
            helper_line.append(helper[:cell_width].ljust(cell_width))

        print(" | ".join(day_line))
        print(" | ".join(helper_line))
        print(separator)


def print_summary(stats: dict) -> None:
    print("\n\n## Summary")
    print("| Person | Main | Backup | Total | Thursday |")
    print("|---|---:|---:|---:|---:|")

    for name, stat in sorted(stats.items()):
        print(
            f"| {name} | "
            f"{stat['main_count']} | "
            f"{stat['backup_count']} | "
            f"{stat['total_count']} | "
            f"{stat['thursday_count']} |"
        )


def write_schedule_csv(schedule: list[dict], output_path: Path) -> None:
    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["date", "weekday", "holiday", "main", "backup"],
        )
        writer.writeheader()
        writer.writerows(schedule)


def default_html_output_path(year: int, month: int) -> Path:
    return Path(f"adhoc_schedule_{year}_{month:02d}.html")


def default_image_output_path(year: int, month: int) -> Path:
    return Path(f"adhoc_schedule_{year}_{month:02d}.svg")


def build_calendar_rows(
    schedule: list[dict],
    year: int,
    month: int,
) -> list[list[dict | None]]:
    schedule_by_date = {item["date"]: item for item in schedule}
    _, last_day = calendar.monthrange(year, month)
    rows = []
    current_row = [None] * len(HTML_CALENDAR_WEEKDAYS)

    for day_number in range(1, last_day + 1):
        current_day = date(year, month, day_number)

        if current_day.weekday() == 4:
            continue

        if current_day.weekday() == 5 and any(current_row):
            rows.append(current_row)
            current_row = [None] * len(HTML_CALENDAR_WEEKDAYS)

        column = HTML_WEEKDAY_COLUMNS[current_day.weekday()]
        current_row[column] = schedule_by_date.get(current_day.isoformat())

    if any(current_row):
        rows.append(current_row)

    return rows


def render_calendar_card(item: dict | None) -> str:
    if item is None:
        return '<div class="day-card day-card--empty"></div>'

    current_day = date.fromisoformat(item["date"])
    color_index = current_day.day % len(DAY_PALETTE)
    day_number = html.escape(str(current_day.day))
    main = html.escape(item["main"])
    backup = html.escape(item["backup"])
    holiday = html.escape(item.get("holiday", ""))
    holiday_badge = f'<div class="holiday-badge">{holiday}</div>' if holiday else ""

    return f"""
        <article class="day-card" style="--card-bg: {DAY_PALETTE[color_index]}">
            <div class="day-card__top">
                <span class="day-number">{day_number}</span>
                {holiday_badge}
            </div>
            <div class="bug-day">
                <span class="bug-day__label">Bug Day</span>
                <strong>{main}</strong>
            </div>
            <div class="helper-pill">
                <span>Helper</span>
                <strong>{backup}</strong>
            </div>
        </article>
    """


def export_html_calendar(
    schedule: list[dict],
    year: int,
    month: int,
    output_path: Path,
) -> None:
    rows = build_calendar_rows(schedule, year, month)
    weekday_headers = "\n".join(
        f"<div class=\"weekday-heading\">{html.escape(WEEKDAY_NAMES[index])}</div>"
        for index in HTML_CALENDAR_WEEKDAYS
    )
    week_rows = "\n".join(
        f"""
        <section class="calendar-row">
            {"".join(render_calendar_card(item) for item in row)}
        </section>
        """
        for row in rows
    )
    title = html.escape(f"Bug Day Schedule - {year}/{month:02d}")

    document = f"""<!doctype html>
<html lang="en" dir="ltr">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{title}</title>
    <style>
        :root {{
            --bg: #f7f7f4;
            --text: #202124;
            --muted: #696b70;
            --line: #deded8;
            --card: #fff8df;
            --accent: #19766d;
            --accent-soft: #dff1ed;
            --week-line: #d0d5d3;
        }}

        * {{
            box-sizing: border-box;
        }}

        body {{
            margin: 0;
            background: var(--bg);
            color: var(--text);
            font-family: Inter, "Segoe UI", Tahoma, sans-serif;
            line-height: 1.6;
        }}

        .page {{
            width: min(1180px, calc(100% - 32px));
            margin: 0 auto;
            padding: 32px 0 40px;
        }}

        .page-header {{
            display: flex;
            align-items: end;
            justify-content: space-between;
            gap: 16px;
            margin-bottom: 20px;
            border-bottom: 1px solid var(--line);
            padding-bottom: 16px;
        }}

        h1 {{
            margin: 0;
            font-size: clamp(1.6rem, 3vw, 2.4rem);
            font-weight: 800;
        }}

        .subtitle {{
            margin: 6px 0 0;
            color: var(--muted);
            font-size: 0.95rem;
        }}

        .calendar {{
            display: grid;
            gap: 14px;
        }}

        .weekday-row,
        .calendar-row {{
            display: grid;
            grid-template-columns: repeat(6, minmax(0, 1fr));
            gap: 10px;
        }}

        .weekday-row {{
            padding: 0 0 8px;
            border-bottom: 2px solid var(--week-line);
        }}

        .calendar-row {{
            padding-bottom: 14px;
            border-bottom: 1px solid var(--week-line);
        }}

        .calendar-row:last-child {{
            border-bottom: 0;
        }}

        .weekday-heading {{
            color: var(--muted);
            font-size: 0.9rem;
            font-weight: 700;
            padding: 0 4px;
        }}

        .day-card {{
            min-height: 150px;
            position: relative;
            background: var(--card-bg, var(--card));
            border: 1px solid #e6b64d;
            border-radius: 8px;
            padding: 12px;
            display: flex;
            flex-direction: column;
            gap: 12px;
            box-shadow: 0 1px 2px rgba(20, 20, 20, 0.04);
        }}

        .day-card--empty {{
            background: transparent;
            border-style: dashed;
            box-shadow: none;
        }}

        .day-card__top {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 8px;
            min-height: 28px;
        }}

        .day-number {{
            width: 32px;
            height: 32px;
            border-radius: 50%;
            background: var(--accent-soft);
            color: var(--accent);
            display: inline-flex;
            align-items: center;
            justify-content: center;
            font-weight: 800;
        }}

        .holiday-badge {{
            color: #7a4e00;
            font-size: 0.78rem;
            font-weight: 700;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }}

        .bug-day {{
            display: grid;
            gap: 2px;
            padding-top: 8px;
            border-top: 1px solid var(--line);
        }}

        .bug-day__label {{
            color: var(--muted);
            font-size: 0.74rem;
            letter-spacing: 0;
        }}

        .bug-day strong {{
            font-size: 1.18rem;
            font-weight: 850;
            overflow-wrap: anywhere;
        }}

        .helper-pill {{
            position: absolute;
            inset-inline-end: 10px;
            bottom: 10px;
            max-width: calc(100% - 20px);
            display: inline-flex;
            align-items: center;
            gap: 5px;
            color: var(--muted);
            background: rgba(255, 255, 255, 0.66);
            border: 1px solid rgba(120, 120, 120, 0.16);
            border-radius: 999px;
            padding: 3px 8px;
            font-size: 0.68rem;
            white-space: nowrap;
        }}

        .helper-pill strong {{
            color: var(--text);
            font-size: 0.72rem;
            font-weight: 650;
            overflow: hidden;
            text-overflow: ellipsis;
        }}

        @media (max-width: 820px) {{
            .page {{
                width: min(100% - 20px, 680px);
                padding-top: 20px;
            }}

            .page-header {{
                display: block;
            }}

            .weekday-row {{
                display: none;
            }}

            .calendar-row {{
                grid-template-columns: repeat(2, minmax(0, 1fr));
            }}

            .day-card--empty {{
                display: none;
            }}
        }}

        @media (max-width: 520px) {{
            .calendar-row {{
                grid-template-columns: 1fr;
            }}

            .day-card {{
                min-height: 0;
            }}
        }}
    </style>
</head>
<body>
    <main class="page">
        <header class="page-header">
            <div>
                <h1>{title}</h1>
                <p class="subtitle">Team calendar from Saturday through Thursday.</p>
            </div>
        </header>
        <div class="calendar">
            <section class="weekday-row">
                {weekday_headers}
            </section>
            {week_rows}
        </div>
    </main>
</body>
</html>
"""
    output_path.write_text(document, encoding="utf-8")


def render_svg_day(item: dict | None, x: int, y: int, width: int, height: int) -> str:
    if item is None:
        return ""

    current_day = date.fromisoformat(item["date"])
    bg = DAY_PALETTE[current_day.day % len(DAY_PALETTE)]
    day_number = html.escape(str(current_day.day))
    main = html.escape(item["main"])
    backup = html.escape(item["backup"])
    holiday = html.escape(item.get("holiday", ""))
    holiday_text = (
        f'<text x="{x + width - 14}" y="{y + 26}" text-anchor="end" '
        f'class="holiday">{holiday}</text>'
        if holiday
        else ""
    )

    return f"""
    <g>
        <rect x="{x}" y="{y}" width="{width}" height="{height}" rx="8"
              fill="{bg}" stroke="#e6b64d" stroke-width="1"/>
        <circle cx="{x + 26}" cy="{y + 26}" r="16" fill="#dff1ed"/>
        <text x="{x + 26}" y="{y + 31}" text-anchor="middle" class="day-number">{day_number}</text>
        {holiday_text}
        <line x1="{x + 12}" y1="{y + 52}" x2="{x + width - 12}" y2="{y + 52}"
              stroke="#deded8"/>
        <text x="{x + 14}" y="{y + 78}" class="label">Bug Day</text>
        <text x="{x + 14}" y="{y + 104}" class="owner">{main}</text>
        <rect x="{x + width - 92}" y="{y + height - 34}" width="78" height="22"
              rx="11" fill="#ffffff" fill-opacity="0.72" stroke="#dddddd"/>
        <text x="{x + width - 53}" y="{y + height - 19}" text-anchor="middle"
              class="helper">Helper: {backup}</text>
    </g>
    """


def export_image_calendar(
    schedule: list[dict],
    year: int,
    month: int,
    output_path: Path,
) -> None:
    rows = build_calendar_rows(schedule, year, month)
    cell_width = 170
    cell_height = 142
    gap = 10
    left = 34
    top = 112
    title = html.escape(f"Bug Day Schedule - {year}/{month:02d}")
    width = left * 2 + len(HTML_CALENDAR_WEEKDAYS) * cell_width + 5 * gap
    height = top + len(rows) * cell_height + max(len(rows) - 1, 0) * gap + 42

    weekday_labels = []
    for index, weekday in enumerate(HTML_CALENDAR_WEEKDAYS):
        x = left + index * (cell_width + gap) + cell_width / 2
        weekday_labels.append(
            f'<text x="{x}" y="82" text-anchor="middle" class="weekday">'
            f"{html.escape(WEEKDAY_NAMES[weekday])}</text>"
        )

    week_lines = []
    cards = []
    for row_index, row in enumerate(rows):
        y = top + row_index * (cell_height + gap)
        if row_index > 0:
            week_lines.append(
                f'<line x1="{left}" y1="{y - gap / 2}" x2="{width - left}" '
                f'y2="{y - gap / 2}" stroke="#d0d5d3" stroke-width="1"/>'
            )
        for column_index, item in enumerate(row):
            x = left + column_index * (cell_width + gap)
            cards.append(render_svg_day(item, x, y, cell_width, cell_height))

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"
     viewBox="0 0 {width} {height}">
    <style>
        .title {{ font: 800 30px Inter, Segoe UI, sans-serif; fill: #202124; }}
        .weekday {{ font: 700 13px Inter, Segoe UI, sans-serif; fill: #696b70; }}
        .day-number {{ font: 800 14px Inter, Segoe UI, sans-serif; fill: #19766d; }}
        .holiday {{ font: 700 11px Inter, Segoe UI, sans-serif; fill: #7a4e00; }}
        .label {{ font: 600 11px Inter, Segoe UI, sans-serif; fill: #696b70; }}
        .owner {{ font: 850 20px Inter, Segoe UI, sans-serif; fill: #202124; }}
        .helper {{ font: 600 10px Inter, Segoe UI, sans-serif; fill: #696b70; }}
    </style>
    <rect width="100%" height="100%" fill="#f7f7f4"/>
    <text x="{left}" y="46" class="title">{title}</text>
    {"".join(weekday_labels)}
    <line x1="{left}" y1="94" x2="{width - left}" y2="94" stroke="#d0d5d3" stroke-width="2"/>
    {"".join(week_lines)}
    {"".join(cards)}
</svg>
"""
    output_path.write_text(svg, encoding="utf-8")
