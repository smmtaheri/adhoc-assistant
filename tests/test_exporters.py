import tempfile
import unittest
import re
import shutil
from pathlib import Path
from unittest import mock

from PIL import Image, UnidentifiedImageError

from adhoc_assistant.exporters import (
    PERSON_BACKGROUND_PALETTE,
    build_calendar_rows,
    export_html_calendar,
    export_image_calendar,
    write_svg_as_jpg,
)


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

    def test_gregorian_svg_can_use_persian_language_rtl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "schedule.svg"
            export_image_calendar(
                sample_schedule(),
                2026,
                6,
                "gregorian",
                output_path,
                stats={"Ali": {"main_count": 1, "backup_count": 0, "total_count": 1}},
                language="fa",
            )

            svg = output_path.read_text(encoding="utf-8")

        self.assertIn("برنامه ادهاک ژوئن ۲۰۲۶", svg)
        self.assertIn("آمار ماهانه", svg)
        self.assertIn("روز باگ", svg)
        self.assertIn('direction="rtl"', svg)
        self.assertIn(">شنبه</text>", svg)
        self.assertNotIn("Monthly summary", svg)

    def test_jalali_svg_can_use_english_language_ltr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "schedule.svg"
            export_image_calendar(
                sample_schedule(),
                1405,
                4,
                "jalali",
                output_path,
                stats={"Ali": {"main_count": 1, "backup_count": 0, "total_count": 1}},
                language="en",
            )

            svg = output_path.read_text(encoding="utf-8")

        self.assertIn("Adhoc Schedule - Tir 1405", svg)
        self.assertIn("Monthly summary", svg)
        self.assertIn("Bug Day", svg)
        self.assertIn('direction="ltr"', svg)
        self.assertIn(">Thursday</text>", svg)
        self.assertNotIn("برنامه ادهاک", svg)

    def test_arabic_svg_uses_arabic_text_and_rtl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "schedule.svg"
            export_image_calendar(
                sample_schedule(),
                2026,
                6,
                "gregorian",
                output_path,
                stats={"Ali": {"main_count": 1, "backup_count": 0, "total_count": 1}},
                language="ar",
            )

            svg = output_path.read_text(encoding="utf-8")

        self.assertIn("جدول Adhoc - يونيو ٢٠٢٦", svg)
        self.assertIn("الملخص الشهري", svg)
        self.assertIn("المسؤول الأساسي", svg)
        self.assertIn('direction="rtl"', svg)
        self.assertIn(">السبت</text>", svg)

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
        self.assertIn(">Mohammad</tspan>", svg)
        self.assertIn(">Hossein…</tspan>", svg)
        self.assertIn("Very Long Helper", svg)

    def test_main_name_wraps_at_fixed_size_and_friday_is_presentation_only(self) -> None:
        schedule = [
            {
                "gregorian_date": "2026-06-23",
                "date": "1405-04-02",
                "day": 2,
                "weekday": "سه‌شنبه",
                "holiday": "",
                "main": "Mahdi Farhang",
                "backup": "Ali",
                "main_color_index": 7,
            }
        ]

        with tempfile.TemporaryDirectory() as tmp:
            svg_path = Path(tmp) / "schedule.svg"
            html_path = Path(tmp) / "schedule.html"
            export_image_calendar(schedule, 1405, 4, "jalali", svg_path)
            export_html_calendar(schedule, 1405, 4, "jalali", html_path)
            svg = svg_path.read_text(encoding="utf-8")
            document = html_path.read_text(encoding="utf-8")

        self.assertEqual(len(PERSON_BACKGROUND_PALETTE), 20)
        self.assertIn('fill="#eef2ff"', svg)
        self.assertIn('class="owner" font-size="25px"', svg)
        self.assertIn(">Mahdi</tspan>", svg)
        self.assertIn(">Farhang</tspan>", svg)
        name_lines = re.search(
            r'<text x="(\d+)" y="\d+" class="owner" font-size="25px">'
            r'<tspan x="(\d+)"[^>]*>Mahdi</tspan>'
            r'<tspan x="(\d+)"[^>]*>Farhang</tspan>',
            svg,
        )
        self.assertIsNotNone(name_lines)
        self.assertGreater(int(name_lines.group(3)), int(name_lines.group(2)))
        self.assertIn(">جمعه</text>", svg)
        self.assertIn('fill="#eef1f3"', svg)
        self.assertNotIn("بدون شیفت", svg)
        self.assertIn("repeat(7, minmax(0, 1fr))", document)
        self.assertIn("margin-inline-start", document)

    def test_calendar_rows_have_a_gray_placeholder_for_each_friday(self) -> None:
        rows = build_calendar_rows(
            sample_schedule(),
            "jalali",
            year=1405,
            month=4,
            include_friday=True,
        )

        self.assertTrue(rows)
        self.assertTrue(all(len(row) == 7 for row in rows))
        off_days = [item for row in rows for item in row if item and item.get("is_off_day")]
        self.assertTrue(off_days)
        self.assertTrue(all(item["main"] == "" and item["backup"] == "" for item in off_days))

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

    def test_jpg_export_reports_unreadable_intermediate_png_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "schedule.jpg"
            bad_png = output_path.parent / ".render-tmp" / "case" / "schedule.png"

            def fake_tempdir(*, dir: Path | str):
                temp_dir = Path(dir) / "case"
                temp_dir.mkdir(parents=True, exist_ok=True)

                class _TempDir:
                    def __enter__(self_inner):
                        return str(temp_dir)

                    def __exit__(self_inner, exc_type, exc, tb):
                        shutil.rmtree(temp_dir, ignore_errors=True)

                return _TempDir()

            def fake_run(cmd, check, capture_output, text):
                bad_png.parent.mkdir(parents=True, exist_ok=True)
                bad_png.write_text("not-a-real-png", encoding="utf-8")
                return mock.Mock(stdout="", stderr="")

            with (
                mock.patch("adhoc_assistant.exporters.ensure_jpg_export_support"),
                mock.patch("adhoc_assistant.exporters.shutil.which", return_value="/usr/bin/rsvg-convert"),
                mock.patch("adhoc_assistant.exporters.tempfile.TemporaryDirectory", side_effect=fake_tempdir),
                mock.patch("adhoc_assistant.exporters.subprocess.run", side_effect=fake_run),
                mock.patch("adhoc_assistant.exporters.Image.open", side_effect=UnidentifiedImageError("bad png")),
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    write_svg_as_jpg("<svg/>", output_path)

            self.assertIn("unreadable PNG preview", str(ctx.exception))
            self.assertIn(".render-tmp", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
