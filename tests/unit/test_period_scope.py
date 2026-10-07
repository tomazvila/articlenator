from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ZR = ROOT / "zettel_ralph"
FIX = ROOT / "tests" / "fixtures" / "time_window"
LIT = FIX / "lit"
NOTES = FIX / "notes"
CANDIDATES = FIX / "candidates"
if str(ZR) not in sys.path:
    sys.path.insert(0, str(ZR))

import verify_claims as vc  # noqa: E402


class SourcePeriodWindows(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.lines = (LIT / "vid-Hm0lo9StzFI.md").read_text(encoding="utf-8").splitlines()
        before = next(line for line in cls.lines if line.startswith("[01:55]")).split("] ", 1)[1]
        at = next(line for line in cls.lines if line.startswith("[02:00]")).split("] ", 1)[1]
        cls.historical = f"{before} {at}"
        cls.diet = next(line for line in cls.lines if line.startswith("[05:40]")).split("] ", 1)[1]

    @staticmethod
    def q(text: str) -> list[vc.Quantity]:
        return vc.extract_quantities(text)

    def test_historical_year_and_a_half_is_a_one_off_window(self) -> None:
        students = next(q for q in self.q(self.historical) if q.values == (500.0,))
        self.assertEqual(students.period, "one year and a half")
        self.assertNotIn("year", [q.unit for q in self.q(self.historical)])

        two_years = next(q for q in self.q("more than 500 students over two years") if q.values == (500.0,))
        two_and_half = next(q for q in self.q("more than 500 students for two years and a half")
                            if q.values == (500.0,))
        one_year = next(q for q in self.q("more than 500 students for one year") if q.values == (500.0,))
        one_and_half = next(q for q in self.q("more than 500 students over one year and a half")
                            if q.values == (500.0,))
        self.assertEqual(two_years.period, "two years")
        self.assertEqual(two_and_half.period, "two years and a half")
        self.assertEqual(one_year.period, "one year")
        self.assertEqual(one_and_half.period, "one year and a half")
        self.assertEqual(vc.match_quantity(students, [two_years])[0], "changed-period")
        self.assertEqual(vc.match_quantity(students, [one_year])[0], "changed-period")
        self.assertEqual(vc.match_quantity(students, [one_and_half])[0], "supported")
        missing_half = next(q for q in self.q("more than 500 students over one year") if q.values == (500.0,))
        wrong_duration = next(q for q in self.q("more than 500 students over two years and a half")
                              if q.values == (500.0,))
        self.assertEqual(vc.match_quantity(students, [missing_half])[0], "changed-period")
        self.assertEqual(vc.match_quantity(students, [wrong_duration])[0], "changed-period")
        self.assertEqual(vc.match_quantity(students, [
            next(q for q in self.q("more than 500 students every 2 weeks") if q.values == (500.0,))
        ])[0], "changed-period")

        unrelated = self.q("for two years I worked elsewhere, and later we had more than 500 students")
        students_unrelated = next(q for q in unrelated if q.values == (500.0,))
        self.assertIsNone(students_unrelated.period)

    def test_meal_and_day_limits_bind_only_in_the_explicit_diet_rate_frame(self) -> None:
        quantities = self.q(self.diet)
        meals = next(q for q in quantities if q.values == (4.0,))
        meal_cap = next(q for q in quantities if q.values == (500.0,))
        daily_cap = next(q for q in quantities if q.values == (2000.0,))
        self.assertIsNone(meals.period)
        self.assertEqual((meal_cap.unit, meal_cap.period, meal_cap.quals),
                         ("calorie", "per meal", frozenset({"upper"})))
        self.assertEqual((daily_cap.unit, daily_cap.period, daily_cap.quals),
                         ("calorie", "per day", frozenset({"upper"})))

        swapped = self.q("maximum 500 calories for the day and maximum 2 000 calories the meal")
        self.assertEqual(vc.match_quantity(meal_cap, swapped)[0], "changed-period")
        self.assertEqual(vc.match_quantity(daily_cap, swapped)[0], "changed-period")

        isolated_meal = self.q("I ate 500 calories at the meal today")[0]
        isolated_day = self.q("I ate 2 000 calories for the day")[0]
        self.assertEqual(isolated_meal.period, "the meal")
        self.assertEqual(isolated_day.period, "for the day")
        self.assertEqual(vc.match_quantity(meal_cap, [isolated_meal])[0], "changed-period")
        self.assertEqual(vc.match_quantity(daily_cap, [isolated_day])[0], "changed-period")
        self.assertNotEqual(vc.match_quantity(isolated_meal, [meal_cap])[0], "supported")
        self.assertNotEqual(vc.match_quantity(isolated_day, [daily_cap])[0], "supported")

    def test_held_metadata_needs_source_faithful_revision(self) -> None:
        lit = vc.LitIndex([LIT])
        path = NOTES / "Radoslav Radev Reports That When He Is On Diet He Tries To Eat Four Meals Of Maximum 500 Calories The Meal And No More Than 2 000 Calories For The Day.md"
        report = vc.verify_note(path, lit, vc.Options())
        self.assertIn("frontmatter", {failure["type"] for failure in report["failures"]}, path.name)

    def test_complete_revised_candidates_verify_against_registered_source(self) -> None:
        source_path = Path(os.environ.get("ZR_PERIOD_SCOPE_SOURCE", ""))
        source_dir = source_path.parent if source_path.is_file() else LIT
        lit = vc.LitIndex([source_dir])
        candidates = sorted(CANDIDATES.glob("Radoslav Radev *.md"))
        candidate_notes = [p for p in candidates if not p.name.startswith(
            ("Radoslav Radev Says That Without", "Radoslav Radev Reports That Right Now")
        )]
        self.assertEqual(len(candidate_notes), 2)
        for path in candidate_notes:
            report = vc.verify_note(path, lit, vc.Options())
            self.assertEqual(report["failures"], [], path.name)


    def test_unrelated_duration_does_not_supply_scope_period(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lit_dir = root / "lit"
            lit_dir.mkdir()
            source = (LIT / "vid-Hm0lo9StzFI.md").read_text()
            source_header = "---" + source.split("---", 2)[1] + "---\n"
            source_text = source_header + (
                "[00:01] for one year and a half I lived abroad.\n"
                "[00:10] now we have more than 500 students.\n"
            )
            (lit_dir / "vid-Hm0lo9StzFI.md").write_text(source_text)
            sample = next(p for p in CANDIDATES.glob("*.md") if "500 Students" in p.name).read_text()
            note_header = "---" + sample.split("---", 2)[1] + "---\n"
            title = "Radoslav Radev Reports His Student Count"
            note = note_header + (
                f"\n# {title}\n\n"
                "Radoslav Radev reports that they have more than 500 students. "
                "[src: vid-Hm0lo9StzFI @ 00:10]\n\n"
                "## Details\n\n"
                "- He says they have more than 500 students. [src: vid-Hm0lo9StzFI @ 00:10]\n\n"
                "## Evidence\n\n"
                '- vid-Hm0lo9StzFI @ 00:10 (Radoslav Radev): "now we have more than 500 students."\n\n'
                "## Connected Ideas\n\n- [[Home]] — index.\n"
            )
            (root / "Home.md").write_text("# Home\n")
            report = vc.verify_note(root / (title + ".md"), vc.LitIndex([lit_dir]),
                                    vc.Options(), text=note)
            self.assertTrue(any(f["type"] == "scope-mismatch" and
                                f["where"] == "scope.quantities[0]"
                                for f in report["failures"]), report)


class ProductiveWorkoutPeriodAlternatives(unittest.TestCase):
    @staticmethod
    def quantities(text: str) -> list[vc.Quantity]:
        return vc.extract_quantities(text)

    def test_exact_period_disjunction_stays_a_bounded_window(self) -> None:
        text = "one productive workout for the week or for the month"
        quantity, = self.quantities(text)
        period = "for the week or for the month"
        self.assertEqual(quantity.period, period)
        self.assertTrue(vc._allowed_quantity_period(period))
        self.assertEqual(vc.match_quantity(quantity, [quantity])[0], "supported")

        weekly = self.quantities("one productive workout per week")[0]
        monthly = self.quantities("one productive workout per month")[0]
        self.assertEqual((weekly.period, monthly.period), ("per week", "per month"))
        self.assertEqual(vc.match_quantity(quantity, [weekly])[0], "changed-period")
        self.assertEqual(vc.match_quantity(quantity, [monthly])[0], "changed-period")
        self.assertEqual(vc.match_quantity(weekly, [quantity])[0], "changed-period")

    def test_changed_or_dropped_window_alternative_fails(self) -> None:
        exact = self.quantities("one productive workout for the week or for the month")[0]
        week_only = self.quantities("one productive workout for the week")[0]
        changed = self.quantities("one productive workout for the week or for the day")[0]
        self.assertEqual(vc.match_quantity(week_only, [exact])[0], "changed-period")
        self.assertEqual(vc.match_quantity(exact, [changed])[0], "changed-period")
        reversed_exact = self.quantities("one productive workout for the month or for the week")[0]
        month_only = self.quantities("one productive workout for the month")[0]
        self.assertEqual(vc.match_quantity(exact, [reversed_exact])[0], "supported")
        self.assertEqual(vc.match_quantity(week_only, [month_only])[0], "changed-period")
        extra = "for the week or for the month or per week"
        self.assertEqual(vc._claim_period_matches(extra, exact.period), False)
        self.assertFalse(vc._allowed_quantity_period("for the week or per month"))
        self.assertFalse(vc._allowed_quantity_period(extra))

    def test_unknown_period_equality_is_unchanged(self) -> None:
        self.assertTrue(vc._same_period(None, None))
        self.assertTrue(vc._claim_period_matches(None, None))
        self.assertFalse(vc._same_period(None, ""))
        self.assertFalse(vc._claim_period_matches(None, ""))

    def test_unrelated_one_off_window_remains_attached_to_its_own_quantity(self) -> None:
        quantities = self.quantities(
            "one productive workout for the week or for the month, then rest four days this week"
        )
        self.assertEqual([q.period for q in quantities],
                         ["for the week or for the month", "this week"])

    def test_generic_week_window_does_not_override_or_create_rest_day_rates(self) -> None:
        recurring_context = self.quantities(
            "if you train four times per week for the week you have three rest days "
            "two of these three race days"
        )
        self.assertEqual([q.period for q in recurring_context], ["per week"] * 4)

        isolated = self.quantities("you have three rest days for the week two of these three race days")
        self.assertEqual([q.period for q in isolated], [None, None, None])

    def test_held_note_period_metadata_before_and_after(self) -> None:
        fixture = Path(__file__).resolve().parents[1] / "fixtures" / "workout_period"
        title = "Radoslav Radev Advises Five Minutes Rest Between Planche Attempts And A Six-Attempt Maximum.md"
        lit = vc.LitIndex([fixture / "lit"])
        before = vc.verify_note(fixture / "notes" / "before" / title, lit, vc.Options())
        self.assertEqual([(f["type"], f["where"]) for f in before["failures"]], [
            ("scope-mismatch", "scope.quantities[5]"),
            ("scope-mismatch", "scope.quantities[6]"),
        ])
        after = vc.verify_note(fixture / "notes" / title, lit, vc.Options())
        self.assertEqual(after["failures"], [])


if __name__ == "__main__":
    unittest.main()
