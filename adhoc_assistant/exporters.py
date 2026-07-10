import csv
import html
import shutil
import subprocess
import tempfile
from datetime import date
from pathlib import Path

from PIL import Image

from .calendars import format_month_title
from .constants import (
    GREGORIAN_CALENDAR_WEEKDAYS,
    JALALI_CALENDAR_WEEKDAYS,
    PERSIAN_WEEKDAY_NAMES,
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


def calendar_weekday_names(calendar_type: str) -> dict[int, str]:
    if calendar_type == "jalali":
        return PERSIAN_WEEKDAY_NAMES
    return WEEKDAY_NAMES


def visual_weekdays(calendar_type: str) -> list[int]:
    if calendar_type == "jalali":
        return JALALI_CALENDAR_WEEKDAYS
    return GREGORIAN_CALENDAR_WEEKDAYS


def internal_date(item: dict) -> date:
    return date.fromisoformat(item.get("gregorian_date", item["date"]))


def print_terminal_calendar(
    schedule: list[dict],
    year: int,
    month: int,
    calendar_type: str,
) -> None:
    weekdays = visual_weekdays(calendar_type)
    rows = build_calendar_rows(schedule, calendar_type)
    cell_width = 19
    separator = "-" * ((cell_width + 3) * len(weekdays) - 3)
    weekday_names = calendar_weekday_names(calendar_type)

    print(f"Bug Day Schedule - {format_month_title(year, month, calendar_type)}")
    print(
        " | ".join(
            weekday_names[weekday].center(cell_width)
            for weekday in weekdays
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

            day_number = str(item.get("day", item["date"].split("-")[-1]))
            owner = f"{day_number.zfill(2)} {item['main']}"
            helper = f"   {item['backup']}"
            day_line.append(owner[:cell_width].ljust(cell_width))
            helper_line.append(helper[:cell_width].ljust(cell_width))

        print(" | ".join(day_line))
        print(" | ".join(helper_line))
        print(separator)


def print_summary(stats: dict, calendar_type: str) -> None:
    thursday_label = "پنجشنبه" if calendar_type == "jalali" else "Thursday"

    print("\n\n## Summary")
    print(f"| Person | Main | Backup | Total | {thursday_label} |")
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
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["date", "weekday", "holiday", "main", "backup"],
        )
        writer.writeheader()
        writer.writerows(
            {
                "date": item["date"],
                "weekday": item["weekday"],
                "holiday": item["holiday"],
                "main": item["main"],
                "backup": item["backup"],
            }
            for item in schedule
        )


def default_html_output_path(year: int, month: int) -> Path:
    return Path(f"adhoc_schedule_{year}_{month:02d}.html")


def default_image_output_path(year: int, month: int) -> Path:
    return Path(f"adhoc_schedule_{year}_{month:02d}.jpg")


def build_calendar_rows(
    schedule: list[dict],
    calendar_type: str,
) -> list[list[dict | None]]:
    weekdays = visual_weekdays(calendar_type)
    weekday_columns = {weekday: index for index, weekday in enumerate(weekdays)}
    rows = []
    current_row = [None] * len(weekdays)

    for item in schedule:
        current_day = internal_date(item)
        if current_day.weekday() == 5 and any(current_row):
            rows.append(current_row)
            current_row = [None] * len(weekdays)

        column = weekday_columns[current_day.weekday()]
        current_row[column] = item

    if any(current_row):
        rows.append(current_row)

    return rows


def render_calendar_card(item: dict | None) -> str:
    if item is None:
        return '<div class="day-card day-card--empty"></div>'

    current_day = internal_date(item)
    color_index = current_day.day % len(DAY_PALETTE)
    day_number = html.escape(str(item.get("day", current_day.day)))
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
            <strong class="bug-day-name">{main}</strong>
            <div class="helper-pill">
                <strong>{backup}</strong>
            </div>
        </article>
    """


def export_html_calendar(
    schedule: list[dict],
    year: int,
    month: int,
    calendar_type: str,
    output_path: Path,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    weekdays = visual_weekdays(calendar_type)
    rows = build_calendar_rows(schedule, calendar_type)
    weekday_names = calendar_weekday_names(calendar_type)
    weekday_headers = "\n".join(
        f"<div class=\"weekday-heading\">{html.escape(weekday_names[index])}</div>"
        for index in weekdays
    )
    week_rows = "\n".join(
        f"""
        <section class="calendar-row">
            {"".join(render_calendar_card(item) for item in row)}
        </section>
        """
        for row in rows
    )
    title = html.escape(f"Bug Day Schedule - {format_month_title(year, month, calendar_type)}")
    html_lang = "fa" if calendar_type == "jalali" else "en"
    html_dir = "rtl" if calendar_type == "jalali" else "ltr"
    subtitle = (
        "تقویم تیم از شنبه تا پنجشنبه."
        if calendar_type == "jalali"
        else "Team calendar from Saturday through Thursday."
    )

    document = f"""<!doctype html>
<html lang="{html_lang}" dir="{html_dir}">
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
            direction: ltr;
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

        .bug-day-name {{
            display: grid;
            gap: 2px;
            padding-top: 8px;
            border-top: 1px solid var(--line);
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
                <p class="subtitle">{html.escape(subtitle)}</p>
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


def svg_text_lines(
    value: str,
    *,
    max_chars: int,
    max_lines: int,
) -> list[str]:
    lines = wrap_svg_text(value, max_chars)
    if len(lines) <= max_lines:
        return lines

    clipped = lines[:max_lines]
    clipped[-1] = clipped[-1][: max(max_chars - 1, 1)].rstrip() + "…"
    return clipped


def render_svg_day(item: dict | None, x: int, y: int, width: int, height: int) -> str:
    if item is None:
        return ""

    current_day = internal_date(item)
    bg = DAY_PALETTE[current_day.day % len(DAY_PALETTE)]
    day_number = html.escape(str(item.get("day", current_day.day)))
    main_lines = svg_text_lines(item["main"], max_chars=16, max_lines=2)
    backup_lines = svg_text_lines(item["backup"], max_chars=20, max_lines=1)
    main_markup = "".join(
        f'<tspan x="{x + 18}" dy="{0 if index == 0 else 27}">{html.escape(line)}</tspan>'
        for index, line in enumerate(main_lines)
    )
    backup = html.escape(backup_lines[0])
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
        <text x="{x + 18}" y="{y + 92}" class="owner">{main_markup}</text>
        <rect x="{x + 14}" y="{y + height - 39}" width="{width - 28}" height="27"
              rx="13.5" fill="#ffffff" fill-opacity="0.78" stroke="#dddddd"/>
        <text x="{x + width - 26}" y="{y + height - 20}" text-anchor="end"
              class="helper">{backup}</text>
    </g>
    """


def svg_title(year: int, month: int, calendar_type: str) -> str:
    if calendar_type == "jalali":
        return f"برنامه ادهاک {format_month_title(year, month, calendar_type)}"
    return f"Adhoc Schedule - {format_month_title(year, month, calendar_type)}"


def summary_values(stats: dict | None) -> list[tuple[str, dict]]:
    if not stats:
        return []

    return sorted(
        (
            name,
            {
                "main_count": int(values.get("main_count", 0)),
                "backup_count": int(values.get("backup_count", 0)),
                "total_count": int(values.get("total_count", 0)),
            },
        )
        for name, values in stats.items()
    )


def wrap_svg_text(value: str, max_chars: int) -> list[str]:
    words = value.split()
    if not words:
        return [""]

    lines: list[str] = []
    current = ""
    for word in words:
        chunks = [
            word[index : index + max_chars]
            for index in range(0, len(word), max_chars)
        ]
        for chunk in chunks:
            candidate = chunk if not current else f"{current} {chunk}"
            if len(candidate) <= max_chars:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = chunk
    if current:
        lines.append(current)
    return lines


def render_svg_summary(
    stats: dict | None,
    calendar_type: str,
    left: int,
    start_y: int,
    width: int,
) -> tuple[str, int]:
    rows = summary_values(stats)
    if not rows:
        return "", 0

    if calendar_type == "jalali":
        title = "آمار ماهانه"
        labels = ("روز باگ", "پشتیبان", "کل")
        direction = "rtl"
        anchor = "start"
        safe_inset = 96
        title_x = width - left - safe_inset
        name_x = width - left - 18 - safe_inset
        value_columns = (left + 150, left + 290, left + 430)
    else:
        title = "Monthly summary"
        labels = ("Bug Day", "Helper", "Total")
        direction = "ltr"
        anchor = "start"
        title_x = left
        name_x = left + 18
        value_columns = (width - left - 430, width - left - 290, width - left - 150)

    line_height = 16
    row_gap = 12
    wrapped_rows = [(name, wrap_svg_text(name, 24), values) for name, values in rows]
    row_heights = [max(26, len(name_lines) * line_height) + row_gap for _, name_lines, _ in wrapped_rows]
    summary_height = 62 + sum(row_heights)
    main_x, backup_x, total_x = value_columns
    header_y = start_y + 40

    parts = [
        f'<text x="{title_x}" y="{start_y + 24}" text-anchor="{anchor}" '
        f'direction="{direction}" unicode-bidi="plaintext" class="summary-title">{html.escape(title)}</text>',
        f'<text x="{main_x}" y="{header_y}" text-anchor="middle" class="summary-head">{html.escape(labels[0])}</text>',
        f'<text x="{backup_x}" y="{header_y}" text-anchor="middle" class="summary-head">{html.escape(labels[1])}</text>',
        f'<text x="{total_x}" y="{header_y}" text-anchor="middle" class="summary-head">{html.escape(labels[2])}</text>',
    ]

    current_y = header_y + 26
    for (name, name_lines, values), row_height in zip(wrapped_rows, row_heights):
        value_y = current_y + max(0, (row_height - row_gap - 26) // 2)
        line_parts = [
            f'<tspan x="{name_x}" dy="{0 if index == 0 else line_height}">'
            f"{html.escape(line)}</tspan>"
            for index, line in enumerate(name_lines)
        ]
        parts.append(
            f'<text x="{name_x}" y="{current_y}" text-anchor="{anchor}" direction="{direction}" '
            f'unicode-bidi="plaintext" class="summary-name">{"".join(line_parts)}</text>'
        )
        parts.append(
            f'<text x="{main_x}" y="{value_y}" text-anchor="middle" class="summary-value">{values["main_count"]}</text>'
        )
        parts.append(
            f'<text x="{backup_x}" y="{value_y}" text-anchor="middle" class="summary-value">{values["backup_count"]}</text>'
        )
        parts.append(
            f'<text x="{total_x}" y="{value_y}" text-anchor="middle" class="summary-value">{values["total_count"]}</text>'
        )
        current_y += row_height

    return "\n".join(parts), summary_height


def export_image_calendar(
    schedule: list[dict],
    year: int,
    month: int,
    calendar_type: str,
    output_path: Path,
    stats: dict | None = None,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    svg = build_calendar_svg(schedule, year, month, calendar_type, stats)
    if output_path.suffix.lower() in {".jpg", ".jpeg"}:
        write_svg_as_jpg(svg, output_path)
        return
    output_path.write_text(svg, encoding="utf-8")


def build_calendar_svg(
    schedule: list[dict],
    year: int,
    month: int,
    calendar_type: str,
    stats: dict | None = None,
) -> str:
    weekdays = visual_weekdays(calendar_type)
    rows = build_calendar_rows(schedule, calendar_type)
    weekday_names = calendar_weekday_names(calendar_type)
    cell_width = 198
    cell_height = 174
    gap = 12
    rtl_canvas_gutter = 480 if calendar_type == "jalali" else 0
    rtl_content_shift = rtl_canvas_gutter // 2
    left = 40 + rtl_content_shift
    top = 150
    base_width = 40 * 2 + len(weekdays) * cell_width + (len(weekdays) - 1) * gap
    width = base_width + rtl_canvas_gutter
    calendar_height = len(rows) * cell_height + max(len(rows) - 1, 0) * gap
    summary_markup, summary_height = render_svg_summary(
        stats=stats,
        calendar_type=calendar_type,
        left=left,
        start_y=top + calendar_height + 28,
        width=width,
    )
    height = top + calendar_height + 48 + summary_height
    title = html.escape(svg_title(year, month, calendar_type))
    rtl_safe_inset = 96 if calendar_type == "jalali" else 0
    title_x = width - left - rtl_safe_inset if calendar_type == "jalali" else left
    title_anchor = "start"
    title_direction = "rtl" if calendar_type == "jalali" else "ltr"

    weekday_labels = []
    for index, weekday in enumerate(weekdays):
        x = left + index * (cell_width + gap) + cell_width / 2
        weekday_labels.append(
            f'<text x="{x}" y="104" text-anchor="middle" class="weekday">'
            f"{html.escape(weekday_names[weekday])}</text>"
        )

    cards = []
    for row_index, row in enumerate(rows):
        y = top + row_index * (cell_height + gap)
        for column_index, item in enumerate(row):
            x = left + column_index * (cell_width + gap)
            cards.append(render_svg_day(item, x, y, cell_width, cell_height))

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"
     viewBox="0 0 {width} {height}" style="background:#ffffff">
    <style>
        .title {{ font: 900 38px "Noto Sans Arabic", "Iranian Sans", "Noto Sans", sans-serif; fill: #202124; }}
        .weekday {{ font: 850 17px "Noto Sans Arabic", "Iranian Sans", "Noto Sans", sans-serif; fill: #55585f; }}
        .day-number {{ font: 900 17px "Noto Sans", sans-serif; fill: #19766d; }}
        .holiday {{ font: 800 13px "Noto Sans Arabic", "Iranian Sans", "Noto Sans", sans-serif; fill: #7a4e00; }}
        .owner {{ font: 900 25px "Noto Sans", sans-serif; fill: #202124; }}
        .helper {{ font: 800 13px "Noto Sans", sans-serif; fill: #4f5358; }}
        .summary-title {{ font: 900 22px "Noto Sans Arabic", "Iranian Sans", "Noto Sans", sans-serif; fill: #202124; }}
        .summary-head {{ font: 850 15px "Noto Sans Arabic", "Iranian Sans", "Noto Sans", sans-serif; fill: #55585f; }}
        .summary-name {{ font: 800 16px "Noto Sans", sans-serif; fill: #202124; }}
        .summary-value {{ font: 900 16px "Noto Sans", sans-serif; fill: #202124; }}
    </style>
    <rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff"/>
    <text x="{title_x}" y="66" text-anchor="{title_anchor}" direction="{title_direction}"
          unicode-bidi="plaintext" class="title">{title}</text>
    {"".join(weekday_labels)}
    <line x1="{left}" y1="118" x2="{width - left}" y2="118" stroke="#d0d5d3" stroke-width="2"/>
    {"".join(cards)}
    {summary_markup}
</svg>
"""


def write_svg_as_jpg(svg: str, output_path: Path) -> None:
    converter = shutil.which("rsvg-convert")
    if converter is None:
        raise RuntimeError("rsvg-convert is required to export JPG calendar images.")

    with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as svg_file:
        svg_path = Path(svg_file.name)
        svg_path.write_text(svg, encoding="utf-8")

    png_path = svg_path.with_suffix(".png")
    try:
        subprocess.run(
            [converter, "-z", "2", "-o", str(png_path), str(svg_path)],
            check=True,
            capture_output=True,
        )
        with Image.open(png_path) as image:
            background = Image.new("RGB", image.size, "#ffffff")
            if image.mode == "RGBA":
                background.paste(image, mask=image.split()[3])
            else:
                background.paste(image)
            background.save(output_path, format="JPEG", quality=94, optimize=True)
    finally:
        svg_path.unlink(missing_ok=True)
        png_path.unlink(missing_ok=True)
