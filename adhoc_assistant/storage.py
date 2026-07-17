import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from .scheduler import stats_to_plain_dict


def month_start(year: int, month: int) -> date:
    return date(year, month, 1)


@contextmanager
def connect(db_path: Path):
    conn = sqlite3.connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS monthly_stats (
                year INTEGER NOT NULL,
                month INTEGER NOT NULL,
                person TEXT NOT NULL,
                main_count INTEGER NOT NULL,
                backup_count INTEGER NOT NULL,
                total_count INTEGER NOT NULL,
                thursday_count INTEGER NOT NULL,
                PRIMARY KEY (year, month, person)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schedule_entries (
                year INTEGER NOT NULL,
                month INTEGER NOT NULL,
                work_date TEXT NOT NULL,
                weekday TEXT NOT NULL,
                holiday TEXT NOT NULL,
                main TEXT NOT NULL,
                backup TEXT NOT NULL,
                PRIMARY KEY (year, month, work_date)
            )
            """
        )


def load_history_from_db(
    db_path: Path,
    target_year: int,
    target_month: int,
    people_names: list[str],
) -> dict[str, dict[str, int]]:
    if not db_path.exists():
        return {}

    init_db(db_path)
    target = month_start(target_year, target_month)
    history = {}

    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
                person,
                SUM(main_count),
                SUM(backup_count),
                SUM(total_count),
                SUM(thursday_count)
            FROM monthly_stats
            WHERE date(printf('%04d-%02d-01', year, month)) < date(?)
            GROUP BY person
            """,
            (target.isoformat(),),
        ).fetchall()

    people = set(people_names)
    for person, main_count, backup_count, total_count, thursday_count in rows:
        if person not in people:
            continue
        history[person] = {
            "main_count": main_count or 0,
            "backup_count": backup_count or 0,
            "total_count": total_count or 0,
            "thursday_count": thursday_count or 0,
        }

    return history


def save_month_to_db(
    db_path: Path,
    year: int,
    month: int,
    schedule: list[dict],
    stats: dict,
) -> None:
    init_db(db_path)
    plain_stats = stats_to_plain_dict(stats)

    with connect(db_path) as conn:
        conn.execute("DELETE FROM monthly_stats WHERE year = ? AND month = ?", (year, month))
        conn.execute(
            "DELETE FROM schedule_entries WHERE year = ? AND month = ?",
            (year, month),
        )

        conn.executemany(
            """
            INSERT INTO monthly_stats (
                year,
                month,
                person,
                main_count,
                backup_count,
                total_count,
                thursday_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    year,
                    month,
                    person,
                    counts["main_count"],
                    counts["backup_count"],
                    counts["total_count"],
                    counts["thursday_count"],
                )
                for person, counts in sorted(plain_stats.items())
            ],
        )
        conn.executemany(
            """
            INSERT INTO schedule_entries (
                year,
                month,
                work_date,
                weekday,
                holiday,
                main,
                backup
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    year,
                    month,
                    item.get("gregorian_date", item["date"]),
                    item["weekday"],
                    item["holiday"],
                    item["main"],
                    item["backup"],
                )
                for item in schedule
            ],
        )
