import argparse
from pathlib import Path

from .config import load_config
from .constants import DEFAULT_CONFIG_PATH, DEFAULT_DB_PATH
from .exporters import (
    default_html_output_path,
    default_image_output_path,
    export_html_calendar,
    export_image_calendar,
    print_summary,
    print_terminal_calendar,
    write_schedule_csv,
)
from .scheduler import build_schedule
from .storage import load_history_from_db, save_month_to_db


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a monthly bug day / adhoc schedule."
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to a TOML or JSON config file. Default: {DEFAULT_CONFIG_PATH}",
    )
    parser.add_argument(
        "--csv",
        help="Optional path for writing the generated schedule as CSV.",
    )
    parser.add_argument(
        "--html",
        help="Optional path for writing the generated calendar HTML.",
    )
    parser.add_argument(
        "--image",
        help="Optional path for writing the generated calendar image as SVG.",
    )
    parser.add_argument(
        "--db",
        default=DEFAULT_DB_PATH,
        help=f"SQLite history database path. Default: {DEFAULT_DB_PATH}",
    )
    parser.add_argument(
        "--no-db-history",
        action="store_true",
        help="Ignore previously saved SQLite history when scoring this month.",
    )
    parser.add_argument(
        "--save-history",
        action="store_true",
        help="Save this generated month into the SQLite history database.",
    )
    return parser.parse_args()


def resolve_html_path(config: dict, cli_path: str | None) -> Path:
    html_path = default_html_output_path(config["year"], config["month"])
    if config.get("output", {}).get("html"):
        html_path = Path(config["output"]["html"])
    if cli_path:
        html_path = Path(cli_path)
    return html_path


def resolve_image_path(config: dict, cli_path: str | None) -> Path:
    image_path = default_image_output_path(config["year"], config["month"])
    if config.get("output", {}).get("image"):
        image_path = Path(config["output"]["image"])
    if cli_path:
        image_path = Path(cli_path)
    return image_path


def main() -> None:
    args = parse_args()
    config = load_config(Path(args.config))

    db_path = Path(args.db)
    people_names = [person["name"] for person in config["people"]]
    db_history = {}
    if not args.no_db_history:
        db_history = load_history_from_db(
            db_path=db_path,
            target_year=config["year"],
            target_month=config["month"],
            people_names=people_names,
        )

    schedule, stats = build_schedule(config, db_history=db_history)

    print_terminal_calendar(
        schedule,
        config["year"],
        config["month"],
        config["calendar"],
    )
    print_summary(stats, config["calendar"])

    csv_path = args.csv or config.get("output", {}).get("csv")
    if csv_path:
        output_path = Path(csv_path)
        write_schedule_csv(schedule, output_path)
        print(f"\nCSV written to: {output_path}")

    html_path = resolve_html_path(config, args.html)
    export_html_calendar(
        schedule=schedule,
        year=config["year"],
        month=config["month"],
        calendar_type=config["calendar"],
        output_path=html_path,
    )
    print(f"HTML written to: {html_path}")

    image_path = resolve_image_path(config, args.image)
    export_image_calendar(
        schedule=schedule,
        year=config["year"],
        month=config["month"],
        calendar_type=config["calendar"],
        output_path=image_path,
        stats=stats,
    )
    print(f"Image written to: {image_path}")

    if args.save_history:
        save_month_to_db(
            db_path=db_path,
            year=config["year"],
            month=config["month"],
            schedule=schedule,
            stats=stats,
        )
        print(f"History saved to: {db_path}")
