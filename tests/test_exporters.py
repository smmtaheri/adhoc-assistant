import tempfile
import unittest
import re
import shutil
from pathlib import Path
from PIL import Image

from adhoc_assistant.exporters import export_image_calendar


def sample_schedule() -> list[dict]:
    return [
        {
            "gregorian_date": "2026-06-23",
            "date": "1405-04-02",
            "day": 2,
            "weekday": "سه‌شنبه",
            "holiday": "",
            "main": "Ali",
            "backup": "Sara",
        },
        {
            "gregorian_date": "2026-06-24",
            "date": "1405-04-03",
            "day": 3,
            "weekday": "چهارشنبه",
            "holiday": "",
            "main": "Sara",
            "backup": "Ali",
        },
        {
            "gregorian_date": "2026-06-25",
            "date": "1405-04-04",
            "day": 4,
            "weekday": "پنجشنبه",
            "holiday": "",
            "main": "Ali",
            "backup": "Sara",
        },
    ]


class ImageExporterTests(unittest.TestCase):
    def test_jalali_svg_has_solid_background_rtl_title_and_summary(self) -> None:
        stats = {
            "Ali": {"main_count": 2, "backup_count": 1, "total_count": 3},
            "Sara": {"main_count": 1, "backup_count": 2, "total_count": 3},
        }

        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "schedule.svg"
            export_image_calendar(
                sample_schedule(),
                1405,
                4,
                "jalali",
                output_path,
                stats=stats,
            )

            svg = output_path.read_text(encoding="utf-8")

        self.assertIn('style="background:#ffffff"', svg)
        self.assertIn('fill="#ffffff"', svg)
        self.assertIn("برنامه ادهاک تیرماه ۱۴۰۵", svg)
        self.assertIn('text-anchor="start" direction="rtl"', svg)
        width = int(re.search(r'width="(\d+)"', svg).group(1))
        title_x = int(re.search(r'<text x="(\d+)" y="66"', svg).group(1))
        self.assertLessEqual(title_x, width - 120)
        self.assertGreaterEqual(width - title_x, 300)
        self.assertIn("آمار ماهانه", svg)
        self.assertIn("روز باگ", svg)
        self.assertIn("پشتیبان", svg)
        self.assertIn('unicode-bidi="plaintext"', svg)
        self.assertNotIn('stroke="#d0d5d3" stroke-width="1"', svg)

    def test_gregorian_svg_keeps_english_left_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "schedule.svg"
            export_image_calendar(
                sample_schedule(),
                2026,
                6,
                "gregorian",
                output_path,
                stats={"Ali": {"main_count": 1, "backup_count": 0, "total_count": 1}},
            )

            svg = output_path.read_text(encoding="utf-8")

        self.assertIn("Adhoc Schedule - June 2026", svg)
        self.assertIn('text-anchor="start"', svg)
        self.assertIn("Monthly summary", svg)

    def test_svg_summary_wraps_long_names_away_from_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "schedule.svg"
            export_image_calendar(
                sample_schedule(),
                1405,
                4,
                "jalali",
                output_path,
                stats={
                    "Mohammad With A Very Very Long Display Name": {
                        "main_count": 12,
                        "backup_count": 10,
                        "total_count": 22,
                    }
                },
            )

            svg = output_path.read_text(encoding="utf-8")

        self.assertIn("<tspan", svg)
        self.assertIn("Mohammad With A Very", svg)
        self.assertIn(">12</text>", svg)

    def test_svg_day_cards_fit_long_main_and_helper_names(self) -> None:
        schedule = [
            {
                "gregorian_date": "2026-06-23",
                "date": "1405-04-02",
                "day": 2,
                "weekday": "سه‌شنبه",
                "holiday": "",
                "main": "Mohammad Hossein With Long Name",
                "backup": "Very Long Helper Display Name",
            }
        ]

        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "schedule.svg"
            export_image_calendar(schedule, 1405, 4, "jalali", output_path)
            svg = output_path.read_text(encoding="utf-8")

        self.assertIn('class="owner" font-size="', svg)
        self.assertIn('class="helper" font-size="', svg)
        self.assertGreaterEqual(svg.count("<tspan"), 3)
        self.assertIn("Mohammad Hossein", svg)
        self.assertIn("Very Long Helper", svg)

    def test_jpg_export_rasterizes_calendar_with_white_background(self) -> None:
        if shutil.which("rsvg-convert") is None:
            self.skipTest("rsvg-convert is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "schedule.jpg"
            export_image_calendar(
                sample_schedule(),
                1405,
                4,
                "jalali",
                output_path,
                stats={"Ali": {"main_count": 2, "backup_count": 1, "total_count": 3}},
            )

            with Image.open(output_path) as image:
                self.assertEqual(image.format, "JPEG")
                self.assertEqual(image.mode, "RGB")
                self.assertGreater(image.width, 1000)
                self.assertGreater(image.height, 700)


if __name__ == "__main__":
    unittest.main()
