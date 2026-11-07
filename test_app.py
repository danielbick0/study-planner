"""Minimal tests for the Study Planner (Steps 1–4).

Run:  python3 -m unittest test_app -v
Uses a temporary database, so it never touches planner.db.
"""

import http.client
import os
import re
import sqlite3
import tempfile
import threading
import unittest
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from urllib.parse import quote, urlencode

import app


def _topics(n, learned=None):
    """Fake topics: [{'id': 1, 'title': 'Topic 1', 'learned': 0}, ...]."""
    learned = learned or set()
    return [
        {"id": i + 1, "title": f"Topic {i + 1}", "learned": 1 if i + 1 in learned else 0}
        for i in range(n)
    ]


def _all_week_slots(start="09:00", end="12:00"):
    """One free slot every weekday, 3 hours each (3 sessions a day)."""
    return [
        {"weekday": d, "start_time": start, "end_time": end} for d in range(7)
    ]


class PlannerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        app.DB_PATH = os.path.join(self.tmp.name, "test.db")
        app.init_db()

    def tearDown(self):
        self.tmp.cleanup()

    def test_seed_courses_are_daniels_real_courses(self):
        with app.connect() as conn:
            names = [r["name"] for r in app.get_courses(conn)]
        self.assertEqual(
            names, ["Algorithms", "Calculus 2", "Systems Programming"]
        )

    def test_add_and_list_assignment(self):
        with app.connect() as conn:
            course_id = conn.execute(
                "SELECT id FROM courses WHERE name = 'Algorithms'"
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO assignments (course_id, title, due_date)"
                " VALUES (?, ?, ?)",
                (course_id, "Problem set 3", "2026-11-12"),
            )
            rows = app.get_assignments(conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Problem set 3")
        self.assertEqual(rows[0]["course_name"], "Algorithms")
        self.assertEqual(rows[0]["done"], 0)

    def test_mark_done(self):
        with app.connect() as conn:
            conn.execute(
                "INSERT INTO assignments (course_id, title)"
                " SELECT id, 'Quiz' FROM courses WHERE name = 'Calculus 2'"
            )
            conn.execute("UPDATE assignments SET done = 1 WHERE title = 'Quiz'")
            done = conn.execute(
                "SELECT done FROM assignments WHERE title = 'Quiz'"
            ).fetchone()["done"]
        self.assertEqual(done, 1)

    # --- Step 2: syllabus parsing -------------------------------------

    def test_parse_syllabus_one_line_one_topic(self):
        parsed = app.parse_syllabus(
            "1. Limits\n2) Derivatives\n- Integrals\n\nSeries\n"
        )
        self.assertEqual(
            parsed, ["Limits", "Derivatives", "Integrals", "Series"]
        )

    # --- Step 2: plan generation --------------------------------------

    def test_generate_plan_schedules_all_topics_when_enough_time(self):
        plan = app.generate_plan(
            _topics(3), _all_week_slots(), date.today() + timedelta(days=7)
        )
        learn = [s for s in plan["sessions"] if s["kind"] == "learn"]
        self.assertEqual(len(learn), 3)
        self.assertEqual(plan["unscheduled"], [])
        # One topic per session, in order, 50 minutes each.
        self.assertEqual([s["topic_title"] for s in learn],
                         ["Topic 1", "Topic 2", "Topic 3"])
        # Each session is 50 minutes, with a 10-minute break between
        # two learning sessions in the same window (Step 3).
        self.assertEqual(learn[0]["start_time"], "09:00")
        self.assertEqual(learn[0]["end_time"], "09:50")
        breaks = [s for s in plan["sessions"] if s["kind"] == "break"]
        self.assertEqual(len(breaks), 2)
        self.assertEqual(breaks[0]["start_time"], "09:50")
        self.assertEqual(breaks[0]["end_time"], "10:00")
        self.assertEqual(learn[1]["start_time"], "10:00")

    def test_generate_plan_reports_unscheduled_topics(self):
        # Only one 60-minute window before the exam: a single session fits.
        # (Updated for Step 4: learn_pct=100, because with the default
        # 40% learn share the learn bucket would be 24 of the 60 free
        # minutes — too small for one 50-minute session.)
        slots = [{"weekday": date.today().weekday(),
                  "start_time": "09:00", "end_time": "10:00"}]
        plan = app.generate_plan(
            _topics(10), slots, date.today() + timedelta(days=2),
            learn_pct=100, review_pct=0, exercise_pct=0, simulation_pct=0,
        )
        learn = [s for s in plan["sessions"] if s["kind"] == "learn"]
        self.assertEqual(len(learn), 1)
        self.assertEqual(len(plan["unscheduled"]), 9)
        self.assertEqual(plan["unscheduled"][0]["title"], "Topic 2")

    def test_generate_plan_skips_learned_topics(self):
        plan = app.generate_plan(
            _topics(3, learned={1}), _all_week_slots(),
            date.today() + timedelta(days=7),
        )
        learn = [s for s in plan["sessions"] if s["kind"] == "learn"]
        self.assertEqual([s["topic_title"] for s in learn],
                         ["Topic 2", "Topic 3"])

    def test_quiz_plan_only_includes_scoped_topics(self):
        # In the database: a quiz with only 2 of 5 topics in scope.
        with app.connect() as conn:
            course_id = conn.execute(
                "SELECT id FROM courses WHERE name = 'Calculus 2'"
            ).fetchone()["id"]
            for i in range(5):
                conn.execute(
                    "INSERT INTO topics (course_id, title) VALUES (?, ?)",
                    (course_id, f"Topic {i + 1}"),
                )
            topic_ids = [r["id"] for r in conn.execute(
                "SELECT id FROM topics ORDER BY id"
            )]
            cursor = conn.execute(
                "INSERT INTO exams (course_id, kind, title, exam_date)"
                " VALUES (?, 'quiz', 'Midterm quiz', ?)",
                (course_id, (date.today() + timedelta(days=7)).isoformat()),
            )
            exam_id = cursor.lastrowid
            for topic_id in topic_ids[1:3]:  # only topics 2 and 3
                conn.execute(
                    "INSERT INTO exam_topics (exam_id, topic_id) VALUES (?, ?)",
                    (exam_id, topic_id),
                )
            exam = conn.execute(
                "SELECT * FROM exams WHERE id = ?", (exam_id,)
            ).fetchone()
            scoped = [dict(r) for r in app.get_scoped_topics(conn, exam)]
            plan = app.generate_plan(
                scoped, _all_week_slots(), exam["exam_date"]
            )
        learn = [s for s in plan["sessions"] if s["kind"] == "learn"]
        self.assertEqual([s["topic_title"] for s in learn],
                         ["Topic 2", "Topic 3"])

    def test_review_session_lands_day_before_exam(self):
        # (Updated for spaced repetition, upgrade 10: reviews no
        # longer fill the last windows first. One topic learned
        # today is reviewed 1 and 3 days later (+7 is past the
        # exam, so it is skipped) — 2 reviews of 30 minutes each.
        # The simulation bucket is still one 120-minute session,
        # on the day before the exam.)
        exam_date = date.today() + timedelta(days=5)
        plan = app.generate_plan(
            _topics(1), _all_week_slots(), exam_date
        )
        review = [s for s in plan["sessions"] if s["kind"] == "review"]
        self.assertEqual(len(review), 2)
        self.assertEqual(
            sorted(s["session_date"] for s in review),
            [date.today() + timedelta(days=1),
             date.today() + timedelta(days=3)])
        # Every review is 30 minutes.
        for s in review:
            self.assertEqual(
                app._to_minutes(s["end_time"])
                - app._to_minutes(s["start_time"]),
                30,
            )
        simulation = [s for s in plan["sessions"]
                      if s["kind"] == "simulation"]
        self.assertEqual(len(simulation), 1)
        self.assertEqual(simulation[0]["session_date"],
                         exam_date - timedelta(days=1))
        self.assertEqual(
            app._to_minutes(simulation[0]["end_time"])
            - app._to_minutes(simulation[0]["start_time"]),
            120,
        )

    def test_free_slot_must_end_after_it_starts(self):
        self.assertTrue(app.is_valid_slot("09:00", "10:00"))
        self.assertFalse(app.is_valid_slot("10:00", "09:00"))
        self.assertFalse(app.is_valid_slot("09:00", "09:00"))

    # --- Step 3: routine, breaks, whole-block assignments -----------

    def test_routine_prefs_saved_and_effective(self):
        with app.connect() as conn:
            self.assertEqual(app.effective_session_minutes(conn), 50)
            self.assertEqual(app.effective_break_minutes(conn), 10)
            self.assertEqual(app.effective_homework_pref(conn), "any")
            self.assertFalse(app.prefs_are_set(conn))
            app.save_prefs(conn, 40, 5, "morning")
            self.assertTrue(app.prefs_are_set(conn))
            self.assertEqual(app.effective_session_minutes(conn), 40)
            self.assertEqual(app.effective_break_minutes(conn), 5)
            self.assertEqual(app.effective_homework_pref(conn), "morning")

    def test_generate_plan_uses_routine_lengths(self):
        plan = app.generate_plan(
            _topics(2), _all_week_slots(), date.today() + timedelta(days=7),
            session_minutes=30, break_minutes=5,
        )
        learn = [s for s in plan["sessions"] if s["kind"] == "learn"]
        self.assertEqual(learn[0]["start_time"], "09:00")
        self.assertEqual(learn[0]["end_time"], "09:30")
        breaks = [s for s in plan["sessions"] if s["kind"] == "break"]
        self.assertEqual(len(breaks), 1)
        self.assertEqual(breaks[0]["start_time"], "09:30")
        self.assertEqual(breaks[0]["end_time"], "09:35")
        self.assertEqual(learn[1]["start_time"], "09:35")

    def test_no_break_after_last_session(self):
        # One topic only: no break at the end of the window.
        plan = app.generate_plan(
            _topics(1), _all_week_slots(), date.today() + timedelta(days=7)
        )
        kinds = [s["kind"] for s in plan["sessions"]]
        self.assertNotIn("break", kinds)

    def test_schedule_assignment_whole_blocks_per_part(self):
        # 4 hours, 2 parts: two whole 2-hour blocks, no breaks inside.
        result = app.schedule_assignment(
            {"id": 1, "due_date": (date.today() + timedelta(days=2)).isoformat(),
             "estimated_hours": 4, "num_parts": 2},
            _all_week_slots(), [],
        )
        self.assertEqual(len(result["blocks"]), 2)
        self.assertEqual(result["unscheduled_minutes"], 0)
        for block in result["blocks"]:
            self.assertEqual(
                app._to_minutes(block["end_time"])
                - app._to_minutes(block["start_time"]),
                120,
            )
        self.assertEqual(result["blocks"][0]["part_label"], "חלק 1")
        self.assertEqual(result["blocks"][1]["part_label"], "חלק 2")

    def test_schedule_assignment_prefers_morning_windows(self):
        # Morning slot first when homework_pref is "morning".
        slots = [
            {"weekday": date.today().weekday(),
             "start_time": "18:00", "end_time": "20:00"},
            {"weekday": date.today().weekday(),
             "start_time": "09:00", "end_time": "11:00"},
        ]
        result = app.schedule_assignment(
            {"id": 1, "due_date": date.today().isoformat(),
             "estimated_hours": 1, "num_parts": 1},
            slots, [], homework_pref="morning",
        )
        self.assertEqual(result["blocks"][0]["start_time"], "09:00")


    def test_breaks_between_sessions_in_long_window(self):
        # One 3-hour window, 3 topics: learn, break, learn, break,
        # learn — a break between sessions, never after the last one.
        # (Updated for Step 4: learn_pct=100, so the learn bucket is
        # the whole 180 free minutes and all three 50-minute
        # sessions fit; with the default 40% the bucket would be
        # 72 minutes — one session only.)
        slots = [{"weekday": date.today().weekday(),
                  "start_time": "09:00", "end_time": "12:00"}]
        plan = app.generate_plan(
            _topics(3), slots, date.today() + timedelta(days=2),
            learn_pct=100, review_pct=0, exercise_pct=0, simulation_pct=0,
        )
        kinds = [s["kind"] for s in plan["sessions"]]
        self.assertEqual(kinds, ["learn", "break", "learn", "break", "learn"])
        breaks = [s for s in plan["sessions"] if s["kind"] == "break"]
        self.assertEqual(len(breaks), 2)
        self.assertEqual(breaks[0]["start_time"], "09:50")
        self.assertEqual(breaks[0]["end_time"], "10:00")
        self.assertEqual(breaks[1]["start_time"], "10:50")
        self.assertEqual(breaks[1]["end_time"], "11:00")

    def test_schedule_assignment_four_parts_whole_blocks(self):
        # 4 hours, 4 parts: exactly 4 whole 60-minute blocks, each
        # one part, no breaks inside, no overlap between blocks.
        result = app.schedule_assignment(
            {"id": 1, "due_date": (date.today() + timedelta(days=2)).isoformat(),
             "estimated_hours": 4, "num_parts": 4},
            _all_week_slots(), [],
        )
        self.assertEqual(len(result["blocks"]), 4)
        self.assertEqual(result["unscheduled_minutes"], 0)
        labels = [b["part_label"] for b in result["blocks"]]
        self.assertEqual(labels, ["חלק 1", "חלק 2", "חלק 3", "חלק 4"])
        for block in result["blocks"]:
            self.assertEqual(
                app._to_minutes(block["end_time"])
                - app._to_minutes(block["start_time"]),
                60,
            )
        spans = [
            (b["block_date"], app._to_minutes(b["start_time"]),
             app._to_minutes(b["end_time"])) for b in result["blocks"]
        ]
        for first, second in zip(spans, spans[1:]):
            self.assertTrue(first[0] < second[0] or first[2] <= second[1])

    def test_schedule_assignment_does_not_overlap_exam_session(self):
        # An exam session occupies 09:00-09:50 today; the assignment
        # block must start after it, not on top of it.
        occupied = [{"session_date": date.today(),
                     "start_time": "09:00", "end_time": "09:50"}]
        slots = [{"weekday": date.today().weekday(),
                  "start_time": "09:00", "end_time": "12:00"}]
        result = app.schedule_assignment(
            {"id": 1, "due_date": date.today().isoformat(),
             "estimated_hours": 1, "num_parts": 1},
            slots, occupied,
        )
        self.assertEqual(len(result["blocks"]), 1)
        self.assertEqual(result["blocks"][0]["start_time"], "09:50")
        self.assertEqual(result["blocks"][0]["end_time"], "10:50")

    def test_schedule_assignment_reports_unscheduled_minutes(self):
        # 10 hours, one part, but only one 60-minute window before
        # the due date: 60 minutes scheduled, 540 honestly reported.
        slots = [{"weekday": date.today().weekday(),
                  "start_time": "09:00", "end_time": "10:00"}]
        result = app.schedule_assignment(
            {"id": 1, "due_date": (date.today() + timedelta(days=2)).isoformat(),
             "estimated_hours": 10, "num_parts": 1},
            slots, [],
        )
        self.assertEqual(result["scheduled_minutes"], 60)
        self.assertEqual(result["unscheduled_minutes"], 540)

    # --- Step 4: importance, percentages, busy blocks, today view ---

    def test_validate_percentages(self):
        self.assertTrue(app.validate_percentages(40, 20, 20, 20))
        self.assertTrue(app.validate_percentages(50, 50, 0, 0))
        self.assertTrue(app.validate_percentages(100, 0, 0, 0))
        self.assertFalse(app.validate_percentages(40, 20, 20, 10))
        self.assertFalse(app.validate_percentages(0, 0, 0, 0))
        self.assertFalse(app.validate_percentages(50, 50, 50, 50))
        self.assertFalse(app.validate_percentages(-10, 50, 30, 30))
        self.assertFalse(app.validate_percentages(40, 20, 20, None))

    def test_importance_ordering_when_time_is_short(self):
        # Three topics, but only one 50-minute learning session fits:
        # the high-importance topic is scheduled first, and the low
        # one is left unscheduled first. (learn_pct=100 so the learn
        # bucket is the whole 60 free minutes.)
        topics = [
            {"id": 1, "title": "Low topic", "learned": 0,
             "importance": "low"},
            {"id": 2, "title": "High topic", "learned": 0,
             "importance": "high"},
            {"id": 3, "title": "Medium topic", "learned": 0,
             "importance": "medium"},
        ]
        slots = [{"weekday": date.today().weekday(),
                  "start_time": "09:00", "end_time": "10:00"}]
        plan = app.generate_plan(
            topics, slots, date.today() + timedelta(days=2),
            learn_pct=100, review_pct=0, exercise_pct=0, simulation_pct=0,
        )
        learn = [s for s in plan["sessions"] if s["kind"] == "learn"]
        self.assertEqual(len(learn), 1)
        self.assertEqual(learn[0]["topic_title"], "High topic")
        unscheduled_titles = [t["title"] for t in plan["unscheduled"]]
        self.assertEqual(unscheduled_titles, ["Medium topic", "Low topic"])

    def test_importance_not_set_counts_as_medium(self):
        # A topic with no importance set is treated as medium: it
        # comes after high and before low.
        self.assertEqual(app.importance_rank({"importance": None}), 2)
        self.assertEqual(app.importance_rank({"importance": "high"}), 3)
        self.assertEqual(app.importance_rank({"importance": "low"}), 1)
        topics = [
            {"id": 1, "title": "Low", "learned": 0, "importance": "low"},
            {"id": 2, "title": "Unset", "learned": 0, "importance": None},
            {"id": 3, "title": "High", "learned": 0, "importance": "high"},
        ]
        ordered = [t["title"]
                   for t in app.sort_topics_by_importance(topics)]
        self.assertEqual(ordered, ["High", "Unset", "Low"])

    def test_percentages_split_learn_and_review(self):
        # 7 days x 3 hours = 1260 free minutes. learn 50% = 630,
        # review 50% = 630, the rest 0. Learn needs 10 topics x 50
        # = 500 minutes (capped by the topics, not the bucket).
        # (Updated for spaced repetition, upgrade 10: reviews land
        # 1, 3 and 7 days after each topic's learning day. Topics
        # 1–3 are learned on day 0, 4–6 on day 1, 7–9 on day 2,
        # 10 on day 3. Days 1 and 2 are full of learning, so their
        # reviews have no room; day 3 fits 2, day 4 fits 2, day 5
        # fits 2 and day 6 fits 1 — 7 reviews in total, at most 2
        # a day. No exercise or simulation at all.
        plan = app.generate_plan(
            _topics(10), _all_week_slots(), date.today() + timedelta(days=7),
            learn_pct=50, review_pct=50, exercise_pct=0, simulation_pct=0,
        )
        counts = plan["session_counts"]
        self.assertEqual(counts["learn"], 10)
        self.assertEqual(counts["review"], 7)
        self.assertEqual(counts["exercise"], 0)
        self.assertEqual(counts["simulation"], 0)
        kinds = {s["kind"] for s in plan["sessions"]}
        self.assertEqual(kinds, {"learn", "review", "break"})
        self.assertEqual(plan["unscheduled"], [])

    def test_learn_pct_zero_reports_exact_reason(self):
        # The old bug: with learn 0% the app blamed "not enough free
        # time". The real reason is that the learn bucket is zero.
        plan = app.generate_plan(
            _topics(3), _all_week_slots(), date.today() + timedelta(days=7),
            learn_pct=0, review_pct=40, exercise_pct=30, simulation_pct=30,
        )
        learn = [s for s in plan["sessions"] if s["kind"] == "learn"]
        self.assertEqual(learn, [])
        self.assertEqual(len(plan["unscheduled"]), 3)
        self.assertEqual(plan["unscheduled_reason"], "learning share is 0%")
        self.assertEqual(
            app.unscheduled_reason(0, plan["unscheduled"]),
            "learning share is 0%")
        self.assertEqual(
            app.unscheduled_reason(40, plan["unscheduled"]),
            "not enough free time")
        self.assertIsNone(app.unscheduled_reason(40, []))

    def test_busy_block_prevents_overlapping_sessions(self):
        # Today is busy 09:00–12:00 (the whole free window): no
        # session may land there, and none may overlap the block.
        busy = [{"block_date": date.today(), "start_time": "09:00",
                 "end_time": "12:00", "reason": "evening with friends"}]
        plan = app.generate_plan(
            _topics(2), _all_week_slots(), date.today() + timedelta(days=3),
            learn_pct=100, review_pct=0, exercise_pct=0, simulation_pct=0,
            busy_blocks=busy,
        )
        learn = [s for s in plan["sessions"] if s["kind"] == "learn"]
        self.assertEqual(len(learn), 2)
        for session in plan["sessions"]:
            self.assertFalse(
                session["session_date"] == date.today()
                and session["start_time"] < "12:00"
                and "09:00" < session["end_time"]
            )
        self.assertNotIn(date.today(),
                         [s["session_date"] for s in learn])

    def test_todays_items_only_today_in_time_order(self):
        today = date.today()
        tomorrow = today + timedelta(days=1)
        items = [
            {"session_date": today, "start_time": "11:00",
             "end_time": "11:50"},
            {"session_date": tomorrow, "start_time": "08:00",
             "end_time": "08:50"},
            {"session_date": today, "start_time": "09:00",
             "end_time": "09:50"},
            {"block_date": today, "start_time": "10:00",
             "end_time": "11:00"},
            {"session_date": today - timedelta(days=1),
             "start_time": "09:00", "end_time": "09:50"},
        ]
        todays = app.todays_items(items, today)
        self.assertEqual(len(todays), 3)
        self.assertEqual([i["start_time"] for i in todays],
                         ["09:00", "10:00", "11:00"])
        # Default day is today.
        self.assertEqual(len(app.todays_items(items)), 3)

    def test_exam_percentage_defaults_and_importance_stored(self):
        with app.connect() as conn:
            course_id = conn.execute(
                "SELECT id FROM courses WHERE name = 'Calculus 2'"
            ).fetchone()["id"]
            cursor = conn.execute(
                "INSERT INTO topics (course_id, title, importance)"
                " VALUES (?, 'Limits', 'high')",
                (course_id,),
            )
            topic_id = cursor.lastrowid
            stored = conn.execute(
                "SELECT importance FROM topics WHERE id = ?", (topic_id,)
            ).fetchone()["importance"]
            self.assertEqual(stored, "high")
            cursor = conn.execute(
                "INSERT INTO exams (course_id, kind, title, exam_date)"
                " VALUES (?, 'exam', 'Final', ?)",
                (course_id, (date.today() + timedelta(days=14)).isoformat()),
            )
            exam = conn.execute(
                "SELECT * FROM exams WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            self.assertEqual(
                (exam["learn_pct"], exam["review_pct"],
                 exam["exercise_pct"], exam["simulation_pct"]),
                (40, 20, 20, 20),
            )
            # A busy block is stored and returned by get_busy_blocks.
            conn.execute(
                "INSERT INTO busy_blocks"
                " (block_date, start_time, end_time, reason)"
                " VALUES (?, '18:00', '21:00', 'friends')",
                (date.today().isoformat(),),
            )
            blocks = app.get_busy_blocks(conn)
            self.assertEqual(len(blocks), 1)
            self.assertEqual(blocks[0]["reason"], "friends")

    def test_done_clears_blocks_and_delete_course_removes_assignments(self):
        with app.connect() as conn:
            course_id = conn.execute(
                "SELECT id FROM courses WHERE name = 'Algorithms'"
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO free_slots (weekday, start_time, end_time)"
                " VALUES (?, '09:00', '12:00')",
                (date.today().weekday(),),
            )
            cursor = conn.execute(
                "INSERT INTO assignments"
                " (course_id, title, due_date, estimated_hours, num_parts)"
                " VALUES (?, 'Homework', ?, 2, 2)",
                (course_id, (date.today() + timedelta(days=2)).isoformat()),
            )
            assignment_id = cursor.lastrowid
            app.reschedule_assignment(conn, assignment_id)
            self.assertEqual(
                len(app.get_assignment_blocks(conn, assignment_id)), 2)
            app.mark_assignment_done(conn, assignment_id)
            self.assertEqual(
                len(app.get_assignment_blocks(conn, assignment_id)), 0)
            # A second assignment, then deleting the course removes it.
            cursor = conn.execute(
                "INSERT INTO assignments (course_id, title)"
                " VALUES (?, 'Second')",
                (course_id,),
            )
            app.delete_course(conn, course_id)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM assignments WHERE course_id = ?",
                    (course_id,),
                ).fetchone()[0], 0)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM assignment_blocks"
                    " WHERE assignment_id = ?",
                    (assignment_id,),
                ).fetchone()[0], 0)

    def test_quiz_with_no_topics_is_rejected(self):
        # The real path: resolve_exam_scope is what /add-exam calls
        # before inserting anything. A quiz with nothing picked
        # returns None, so the handler does not save the exam at all.
        with app.connect() as conn:
            course_id = conn.execute(
                "SELECT id FROM courses WHERE name = 'Calculus 2'"
            ).fetchone()["id"]
            for title in ("Limits", "Derivatives"):
                conn.execute(
                    "INSERT INTO topics (course_id, title) VALUES (?, ?)",
                    (course_id, title),
                )
            scope = app.resolve_exam_scope(conn, "quiz", course_id, [])
            self.assertIsNone(scope)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM exams").fetchone()[0], 0)

    def test_quiz_with_topics_picked_saves_exactly_those(self):
        with app.connect() as conn:
            course_id = conn.execute(
                "SELECT id FROM courses WHERE name = 'Calculus 2'"
            ).fetchone()["id"]
            ids = []
            for title in ("Limits", "Derivatives", "Integrals"):
                cursor = conn.execute(
                    "INSERT INTO topics (course_id, title) VALUES (?, ?)",
                    (course_id, title),
                )
                ids.append(str(cursor.lastrowid))
            scope = app.resolve_exam_scope(
                conn, "quiz", course_id, ids[1:])
            self.assertEqual(scope, ids[1:])
            cursor = conn.execute(
                "INSERT INTO exams (course_id, kind, title, exam_date)"
                " VALUES (?, 'quiz', 'Midterm', ?)",
                (course_id, (date.today() + timedelta(days=7)).isoformat()),
            )
            exam_id = cursor.lastrowid
            for topic_id in scope:
                conn.execute(
                    "INSERT INTO exam_topics (exam_id, topic_id)"
                    " VALUES (?, ?)",
                    (exam_id, topic_id),
                )
            exam = conn.execute(
                "SELECT * FROM exams WHERE id = ?", (exam_id,)
            ).fetchone()
            scoped = [t["title"] for t in app.get_scoped_topics(conn, exam)]
            self.assertEqual(scoped, ["Derivatives", "Integrals"])

    def test_exam_with_no_topics_defaults_to_whole_course(self):
        with app.connect() as conn:
            course_id = conn.execute(
                "SELECT id FROM courses WHERE name = 'Calculus 2'"
            ).fetchone()["id"]
            for title in ("Limits", "Derivatives", "Integrals"):
                conn.execute(
                    "INSERT INTO topics (course_id, title) VALUES (?, ?)",
                    (course_id, title),
                )
            scope = app.resolve_exam_scope(conn, "exam", course_id, [])
            self.assertEqual(len(scope), 3)
            cursor = conn.execute(
                "INSERT INTO exams (course_id, kind, title, exam_date)"
                " VALUES (?, 'exam', 'Final', ?)",
                (course_id, (date.today() + timedelta(days=14)).isoformat()),
            )
            exam_id = cursor.lastrowid
            for topic_id in scope:
                conn.execute(
                    "INSERT INTO exam_topics (exam_id, topic_id)"
                    " VALUES (?, ?)",
                    (exam_id, topic_id),
                )
            exam = conn.execute(
                "SELECT * FROM exams WHERE id = ?", (exam_id,)
            ).fetchone()
            scoped = [t["title"] for t in app.get_scoped_topics(conn, exam)]
            self.assertEqual(
                scoped, ["Limits", "Derivatives", "Integrals"])

    def test_exam_form_shows_empty_state_with_no_topics(self):
        # With no topics anywhere, the exam form shows a plain
        # message and no checkboxes — never an empty silent space.
        with app.connect() as conn:
            html = app.page(
                app.get_courses(conn), app.get_assignments(conn), topics=[])
        self.assertIn("עדיין אין נושאים", html)
        self.assertNotIn('name="topic_id"', html)

    def test_exam_form_groups_checkboxes_by_course(self):
        with app.connect() as conn:
            calc_id = conn.execute(
                "SELECT id FROM courses WHERE name = 'Calculus 2'"
            ).fetchone()["id"]
            algo_id = conn.execute(
                "SELECT id FROM courses WHERE name = 'Algorithms'"
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO topics (course_id, title, importance)"
                " VALUES (?, 'Limits', 'high')",
                (calc_id,),
            )
            conn.execute(
                "INSERT INTO topics (course_id, title, importance)"
                " VALUES (?, 'Sorting', 'low')",
                (algo_id,),
            )
            draft = {
                "course_id": str(calc_id), "kind": "exam",
                "title": "Midterm",
                "exam_date": (
                    date.today() + timedelta(days=14)).isoformat(),
                "topic_id": [],
            }
            html = app.exam_flow_step2_html(conn, draft)
        # One collapsed <details> per course in step 2 of the exam
        # flow, the topic under its own course, and the topic's
        # current importance shown on the checkbox.
        self.assertIn("<summary>Calculus 2 — 1 נושא</summary>", html)
        self.assertIn("Limits — מאוד חשוב", html)
        self.assertIn("<summary>Algorithms — 1 נושא</summary>", html)
        self.assertIn("Sorting — לא מאוד חשוב", html)
        self.assertIn('name="topic_id"', html)
        self.assertNotIn('<details class="course-topics" open', html)

    def test_importance_buttons_mark_current_choice(self):
        with app.connect() as conn:
            calc_id = conn.execute(
                "SELECT id FROM courses WHERE name = 'Calculus 2'"
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO topics (course_id, title, importance)"
                " VALUES (?, 'Limits', 'high')",
                (calc_id,),
            )
            html = app.page(
                app.get_courses(conn), app.get_assignments(conn),
                topics=app.get_topics(conn))
        self.assertIn("נושאים חשובים יותר מתוכננים קודם", html)
        self.assertIn("imp-current", html)
        self.assertIn("/topic-importance?", html)

    # --- Fixes: DB next to app.py, stay in section, wizard ---------

    def test_db_path_is_absolute_next_to_app_file(self):
        # The module default must be an absolute path in the same
        # folder as app.py, even when the process runs from a
        # different folder — that is the whole fix (a relative
        # "planner.db" used to land wherever CMD happened to be).
        import importlib
        old_cwd = os.getcwd()
        try:
            os.chdir(self.tmp.name)  # not the folder app.py lives in
            importlib.reload(app)
            self.assertTrue(os.path.isabs(app.DB_PATH))
            self.assertEqual(
                os.path.dirname(app.DB_PATH),
                os.path.dirname(os.path.abspath(app.__file__)),
            )
            self.assertEqual(os.path.basename(app.DB_PATH), "planner.db")
        finally:
            os.chdir(old_cwd)

    def test_redirect_location_mapping(self):
        self.assertEqual(
            app.redirect_location("exams"), "/?open=exams#exams")
        self.assertEqual(
            app.redirect_location("today"), "/?open=today#today")
        self.assertEqual(
            app.redirect_location("topics"), "/?open=topics#topics")
        self.assertEqual(app.redirect_location(None), "/")
        self.assertEqual(app.redirect_location("nope"), "/")
        # QA round 2, fix 10: a save adds ?saved=1; a warning adds
        # ?warn=<code> — both keep the section and its anchor.
        self.assertEqual(
            app.redirect_location("exams", saved=True),
            "/?open=exams&saved=1#exams")
        self.assertEqual(
            app.redirect_location("exams", saved=True, warn="sameday"),
            "/?open=exams&saved=1&warn=sameday#exams")
        self.assertEqual(
            app.redirect_location("slots", warn="slot_overlap"),
            "/?open=slots&warn=slot_overlap#slots")
        for path, section in app.POST_SECTIONS.items():
            self.assertIn(section, app.SECTION_IDS, path)

    def test_open_section_renders_that_details_open(self):
        with app.connect() as conn:
            html = app.page(
                app.get_courses(conn), app.get_assignments(conn),
                open_section="exams")
        self.assertIn('<details class="card" id="exams" open>', html)
        self.assertNotIn('<details class="card" id="topics" open>', html)

    def test_setup_banner_shown_with_no_topics_hidden_with_topics(self):
        with app.connect() as conn:
            html = app.page(
                app.get_courses(conn), app.get_assignments(conn), topics=[])
        self.assertIn('<p class="setup-banner">', html)
        self.assertIn("/setup", html)
        calc_id = conn.execute(
            "SELECT id FROM courses WHERE name = 'Calculus 2'"
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO topics (course_id, title) VALUES (?, 'Limits')",
            (calc_id,),
        )
        html = app.page(
            app.get_courses(conn), app.get_assignments(conn),
            topics=app.get_topics(conn))
        self.assertNotIn('<p class="setup-banner">', html)


class DateFocusAndMissedTest(unittest.TestCase):
    """Fix batch: real dates, focus vs variety, done cascade, missed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        app.DB_PATH = os.path.join(self.tmp.name, "test.db")
        app.init_db()

    def tearDown(self):
        self.tmp.cleanup()

    def _course_id(self, name="Calculus 2"):
        with app.connect() as conn:
            return conn.execute(
                "SELECT id FROM courses WHERE name = ?", (name,)
            ).fetchone()["id"]

    def _add_topics(self, course_id, *titles):
        with app.connect() as conn:
            ids = []
            for title in titles:
                cursor = conn.execute(
                    "INSERT INTO topics (course_id, title) VALUES (?, ?)",
                    (course_id, title),
                )
                ids.append(cursor.lastrowid)
            return ids

    def _add_exam(self, course_id, title, kind="exam", learn_pct=100,
                  exam_date=None):
        exam_date = exam_date or (
            date.today() + timedelta(days=7)).isoformat()
        with app.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO exams (course_id, kind, title, exam_date,"
                " learn_pct, review_pct, exercise_pct, simulation_pct)"
                " VALUES (?, ?, ?, ?, ?, 0, 0, 0)",
                (course_id, kind, title, exam_date, learn_pct),
            )
            return cursor.lastrowid

    # --- Fix 1: real dates -------------------------------------------

    def test_validate_date_rejects_year_1212_and_impossible_dates(self):
        self.assertIsNone(app.validate_date("1212-06-01"))
        self.assertIsNone(app.validate_date("2026-02-30"))
        self.assertIsNone(app.validate_date("not-a-date"))
        self.assertIsNone(app.validate_date(""))
        self.assertIsNone(app.validate_date(None))

    def test_validate_date_rejects_past_and_far_future(self):
        self.assertIsNone(
            app.validate_date((date.today() - timedelta(days=1)).isoformat()))
        self.assertIsNone(
            app.validate_date(f"{date.today().year + 2}-01-01"))

    def test_validate_date_accepts_today_this_year_and_next_year(self):
        today = date.today()
        self.assertEqual(app.validate_date(today.isoformat()), today)
        self.assertIsNotNone(
            app.validate_date((today + timedelta(days=14)).isoformat()))
        self.assertIsNotNone(
            app.validate_date(f"{today.year + 1}-01-15"))

    def test_form_date_defaults_are_sensible(self):
        with app.connect() as conn:
            html = app.page(
                app.get_courses(conn), app.get_assignments(conn))
        assignment_default = (date.today() + timedelta(days=7)).isoformat()
        self.assertIn(f'value="{assignment_default}"', html)
        self.assertIn(f'value="{date.today().isoformat()}"', html)
        # The exam date default now lives on step 1 of the stepped
        # exam flow, not on the home page.
        with app.connect() as conn:
            step1 = app.exam_flow_step1_html(
                conn, app._exam_draft_from_query({}))
        exam_default = (date.today() + timedelta(days=14)).isoformat()
        self.assertIn(f'value="{exam_default}"', step1)

    # --- Fix 3: focus vs variety --------------------------------------

    def test_effective_study_style_defaults_to_variety(self):
        with app.connect() as conn:
            self.assertEqual(app.effective_study_style(conn), "variety")
            app.save_prefs(conn, 50, 10, "any", "focus")
            self.assertEqual(app.effective_study_style(conn), "focus")
            app.save_prefs(conn, 50, 10, "any", "variety")
            self.assertEqual(app.effective_study_style(conn), "variety")

    def test_focus_keeps_learning_off_blocked_days(self):
        blocked = {date.today() + timedelta(days=1)}
        focus = app.generate_plan(
            _topics(6), _all_week_slots(),
            date.today() + timedelta(days=7), learn_pct=100,
            study_style="focus", focus_blocked_dates=blocked)
        focus_learn_days = {
            s["session_date"] for s in focus["sessions"]
            if s["kind"] == "learn"}
        self.assertNotIn(date.today() + timedelta(days=1),
                         focus_learn_days)
        self.assertEqual(len(focus_learn_days), 2)
        variety = app.generate_plan(
            _topics(6), _all_week_slots(),
            date.today() + timedelta(days=7), learn_pct=100,
            study_style="variety", focus_blocked_dates=blocked)
        variety_learn_days = {
            s["session_date"] for s in variety["sessions"]
            if s["kind"] == "learn"}
        self.assertIn(date.today() + timedelta(days=1),
                      variety_learn_days)

    def test_focus_two_exams_use_different_days(self):
        calc_id = self._course_id()
        alg_id = self._course_id("Algorithms")
        with app.connect() as conn:
            # Six-hour windows, so that in variety mode the second
            # exam can still learn on the same day as the first —
            # at a different time (QA fix 1 forbids sharing the
            # same time, not the same day). With 3-hour windows
            # the first exam would fill the whole first day and
            # the variety check below could never pass.
            conn.execute(
                "INSERT INTO free_slots (weekday, start_time, end_time)"
                " VALUES (?, '09:00', '15:00'), (?, '09:00', '15:00'),"
                " (?, '09:00', '15:00'), (?, '09:00', '15:00'),"
                " (?, '09:00', '15:00'), (?, '09:00', '15:00'),"
                " (?, '09:00', '15:00')",
                tuple(d for d in range(7)))
        topics_a = self._add_topics(calc_id, "Limits", "Derivatives",
                                    "Integrals")
        topics_b = self._add_topics(alg_id, "Sorting", "Graphs", "DP")
        exam_a = self._add_exam(calc_id, "Calculus exam")
        exam_b = self._add_exam(alg_id, "Algorithms exam")
        with app.connect() as conn:
            for topic_id in topics_a:
                conn.execute(
                    "INSERT INTO exam_topics (exam_id, topic_id)"
                    " VALUES (?, ?)", (exam_a, topic_id))
            for topic_id in topics_b:
                conn.execute(
                    "INSERT INTO exam_topics (exam_id, topic_id)"
                    " VALUES (?, ?)", (exam_b, topic_id))
            app.regenerate_exam_plan(conn, exam_a)
            a_days = {
                s["session_date"] for s in
                app.get_plan_sessions(conn, exam_a)
                if s["kind"] == "learn"}
            # Variety (the default): B may learn on the same day as A.
            app.regenerate_exam_plan(conn, exam_b)
            b_days = {
                s["session_date"] for s in
                app.get_plan_sessions(conn, exam_b)
                if s["kind"] == "learn"}
            self.assertEqual(a_days & b_days, a_days)
            # Focus: B's learning moves off A's days entirely.
            app.save_prefs(conn, 50, 10, "any", "focus")
            app.regenerate_exam_plan(conn, exam_b)
            b_days = {
                s["session_date"] for s in
                app.get_plan_sessions(conn, exam_b)
                if s["kind"] == "learn"}
            self.assertEqual(a_days & b_days, set())
            self.assertEqual(len(b_days), 1)

    # --- Fix 4: done cascade and missed work --------------------------

    def test_mark_done_frees_time_for_other_assignment(self):
        calc_id = self._course_id()
        today = date.today()
        with app.connect() as conn:
            conn.execute(
                "INSERT INTO free_slots (weekday, start_time, end_time)"
                " VALUES (?, '09:00', '12:00')", (today.weekday(),))
            cursor_a = conn.execute(
                "INSERT INTO assignments"
                " (course_id, title, due_date, estimated_hours, num_parts)"
                " VALUES (?, 'A', ?, 2, 1)", (calc_id, today.isoformat()))
            a_id = cursor_a.lastrowid
            cursor_b = conn.execute(
                "INSERT INTO assignments"
                " (course_id, title, due_date, estimated_hours, num_parts)"
                " VALUES (?, 'B', ?, 2, 1)", (calc_id, today.isoformat()))
            b_id = cursor_b.lastrowid
            app.reschedule_assignment(conn, a_id)
            app.reschedule_assignment(conn, b_id)
            b_blocks = app.get_assignment_blocks(conn, b_id)
            # Only 3 hours in the window: B got 1 hour, 1 hour short.
            self.assertEqual(
                sum(app._to_minutes(b["end_time"])
                    - app._to_minutes(b["start_time"]) for b in b_blocks),
                60)
            app.mark_assignment_done(conn, a_id)
            self.assertEqual(
                len(app.get_assignment_blocks(conn, a_id)), 0)
            b_blocks = app.get_assignment_blocks(conn, b_id)
            # A is done, so B is rescheduled into the freed morning.
            self.assertEqual(
                sum(app._to_minutes(b["end_time"])
                    - app._to_minutes(b["start_time"]) for b in b_blocks),
                120)
            self.assertEqual(b_blocks[0]["start_time"], "09:00")

    def test_missed_items_lists_only_past_undone(self):
        today = date.today()
        items = [
            {"session_date": today - timedelta(days=1),
             "start_time": "09:00", "done": 0},
            {"session_date": today - timedelta(days=1),
             "start_time": "10:00", "done": 1},
            {"session_date": today, "start_time": "09:00", "done": 0},
            {"session_date": today + timedelta(days=1),
             "start_time": "09:00", "done": 0},
        ]
        missed = app.missed_items(items, today)
        self.assertEqual(len(missed), 1)
        self.assertEqual(missed[0]["start_time"], "09:00")
        self.assertEqual(missed[0]["session_date"],
                         today - timedelta(days=1))

    def test_today_view_has_done_links_and_missed_section(self):
        calc_id = self._course_id()
        today = date.today()
        with app.connect() as conn:
            conn.execute(
                "INSERT INTO free_slots (weekday, start_time, end_time)"
                " VALUES (?, '09:00', '12:00')", (today.weekday(),))
        topic_ids = self._add_topics(calc_id, "Limits")
        exam_id = self._add_exam(calc_id, "Calculus exam")
        with app.connect() as conn:
            conn.execute(
                "INSERT INTO exam_topics (exam_id, topic_id)"
                " VALUES (?, ?)", (exam_id, topic_ids[0]))
            conn.execute(
                "INSERT INTO plan_sessions"
                " (exam_id, topic_id, session_date, start_time, end_time,"
                "  kind, done) VALUES (?, ?, ?, '09:00', '09:50',"
                "  'learn', 0)",
                (exam_id, topic_ids[0],
                 (today - timedelta(days=1)).isoformat()))
            conn.execute(
                "INSERT INTO plan_sessions"
                " (exam_id, topic_id, session_date, start_time, end_time,"
                "  kind, done) VALUES (?, ?, ?, '10:00', '10:50',"
                "  'learn', 0)",
                (exam_id, topic_ids[0], today.isoformat()))
            cursor = conn.execute(
                "INSERT INTO assignments"
                " (course_id, title, due_date, estimated_hours, num_parts)"
                " VALUES (?, 'Homework', ?, 1, 1)",
                (calc_id, today.isoformat()))
            assignment_id = cursor.lastrowid
            app.reschedule_assignment(conn, assignment_id)
            html = app.page(
                app.get_courses(conn), app.get_assignments(conn),
                topics=app.get_topics(conn),
                free_slots=app.get_free_slots(conn),
                exams=app.get_exams(conn),
                sessions_by_exam={exam_id: app.get_plan_sessions(
                    conn, exam_id)},
                assignment_blocks_by_assignment={
                    assignment_id: app.get_assignment_blocks(
                        conn, assignment_id)},
                prefs=app.get_prefs(conn))
        # Today's assignment block and session can be ticked done.
        self.assertIn(f"/done?id={assignment_id}", html)
        self.assertIn("/session-done?id=", html)
        # Yesterday's un-done session is shown, not dropped.
        self.assertIn("פספסת", html)
        self.assertIn((today - timedelta(days=1)).isoformat(), html)
        self.assertIn("תזמן מחדש", html)


class QAFixTest(unittest.TestCase):
    """QA fix batch 1–9: no double-booking, no practice before
    learning, focus for every kind, busy blocks move sessions,
    edit/delete, plan on creation, review cap, grouped exam page,
    now + next markers."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        app.DB_PATH = os.path.join(self.tmp.name, "test.db")
        app.init_db()

    def tearDown(self):
        self.tmp.cleanup()

    def _course_id(self, name="Calculus 2"):
        with app.connect() as conn:
            return conn.execute(
                "SELECT id FROM courses WHERE name = ?", (name,)
            ).fetchone()["id"]

    def _add_slots(self, start="09:00", end="12:00"):
        with app.connect() as conn:
            conn.execute(
                "INSERT INTO free_slots (weekday, start_time, end_time)"
                " VALUES (?, ?, ?), (?, ?, ?), (?, ?, ?), (?, ?, ?),"
                " (?, ?, ?), (?, ?, ?), (?, ?, ?)",
                tuple(v for d in range(7) for v in (d, start, end)))

    def _add_topics(self, course_id, *titles):
        with app.connect() as conn:
            ids = []
            for title in titles:
                cursor = conn.execute(
                    "INSERT INTO topics (course_id, title) VALUES (?, ?)",
                    (course_id, title),
                )
                ids.append(cursor.lastrowid)
            return ids

    def _add_exam(self, course_id, title, kind="exam", exam_date=None,
                  learn_pct=40, review_pct=20, exercise_pct=20,
                  simulation_pct=20):
        exam_date = exam_date or (
            date.today() + timedelta(days=7)).isoformat()
        with app.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO exams (course_id, kind, title, exam_date,"
                " learn_pct, review_pct, exercise_pct, simulation_pct)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (course_id, kind, title, exam_date, learn_pct,
                 review_pct, exercise_pct, simulation_pct),
            )
            return cursor.lastrowid

    def _all_plan_sessions(self, conn):
        return conn.execute(
            "SELECT * FROM plan_sessions ORDER BY session_date, start_time"
        ).fetchall()

    def _assert_no_cross_exam_overlaps(self, sessions):
        """No two sessions of different exams share date + time —
        whatever kind they are (learn, review, exercise,
        simulation, break)."""
        spans = [
            (s["session_date"],
             app._to_minutes(s["start_time"]),
             app._to_minutes(s["end_time"]), s["exam_id"], s["kind"])
            for s in sessions
        ]
        for i, first in enumerate(spans):
            for second in spans[i + 1:]:
                if first[3] == second[3]:
                    continue
                overlap = (first[0] == second[0]
                           and first[1] < second[2]
                           and second[1] < first[2])
                self.assertFalse(
                    overlap,
                    f"exams {first[3]} and {second[3]} overlap: "
                    f"{first} vs {second}")

    # --- Fix 1: no double-booking ------------------------------------

    def test_two_exams_never_share_time(self):
        # The QA repro: a quiz scoped to 2 topics + a whole-course
        # exam, same dates, same free time — 12 overlapping pairs
        # before the fix, zero after it.
        calc_id = self._course_id()
        alg_id = self._course_id("Algorithms")
        self._add_slots()
        calc_topics = self._add_topics(
            calc_id, "Limits", "Derivatives", "Integrals", "Series")
        alg_topics = self._add_topics(
            alg_id, "Sorting", "Graphs", "DP", "Trees")
        quiz_id = self._add_exam(calc_id, "Calculus quiz", kind="quiz")
        exam_id = self._add_exam(alg_id, "Algorithms exam")
        with app.connect() as conn:
            for topic_id in calc_topics[:2]:
                conn.execute(
                    "INSERT INTO exam_topics (exam_id, topic_id)"
                    " VALUES (?, ?)", (quiz_id, topic_id))
            for topic_id in alg_topics:
                conn.execute(
                    "INSERT INTO exam_topics (exam_id, topic_id)"
                    " VALUES (?, ?)", (exam_id, topic_id))
            app.regenerate_all_exam_plans(conn)
            sessions = self._all_plan_sessions(conn)
        self.assertTrue(any(s["exam_id"] == quiz_id for s in sessions))
        self.assertTrue(any(s["exam_id"] == exam_id for s in sessions))
        self._assert_no_cross_exam_overlaps(sessions)

    def test_earliest_exam_keeps_its_time(self):
        calc_id = self._course_id()
        alg_id = self._course_id("Algorithms")
        self._add_slots()
        calc_topics = self._add_topics(calc_id, "Limits", "Derivatives")
        alg_topics = self._add_topics(alg_id, "Sorting", "Graphs")
        exam_a = self._add_exam(
            calc_id, "Calculus exam",
            exam_date=(date.today() + timedelta(days=7)).isoformat())
        with app.connect() as conn:
            for topic_id in calc_topics:
                conn.execute(
                    "INSERT INTO exam_topics (exam_id, topic_id)"
                    " VALUES (?, ?)", (exam_a, topic_id))
            app.regenerate_exam_plan(conn, exam_a)
            before = [
                (s["session_date"], s["start_time"], s["end_time"],
                 s["kind"], s["topic_id"])
                for s in app.get_plan_sessions(conn, exam_a)
            ]
            self.assertTrue(before)
        exam_b = self._add_exam(
            alg_id, "Algorithms exam",
            exam_date=(date.today() + timedelta(days=14)).isoformat())
        with app.connect() as conn:
            for topic_id in alg_topics:
                conn.execute(
                    "INSERT INTO exam_topics (exam_id, topic_id)"
                    " VALUES (?, ?)", (exam_b, topic_id))
            # Creating the later exam rebuilds all plans in date
            # order: the earlier exam is rebuilt first, so it
            # keeps exactly the sessions it had.
            app.regenerate_all_exam_plans(conn)
            after = [
                (s["session_date"], s["start_time"], s["end_time"],
                 s["kind"], s["topic_id"])
                for s in app.get_plan_sessions(conn, exam_a)
            ]
            self.assertEqual(after, before)
            self._assert_no_cross_exam_overlaps(
                self._all_plan_sessions(conn))

    def test_done_sessions_survive_regeneration(self):
        calc_id = self._course_id()
        self._add_slots()
        topic_ids = self._add_topics(calc_id, "Limits", "Derivatives")
        exam_id = self._add_exam(calc_id, "Calculus exam", learn_pct=100,
                                 review_pct=0, exercise_pct=0,
                                 simulation_pct=0)
        with app.connect() as conn:
            for topic_id in topic_ids:
                conn.execute(
                    "INSERT INTO exam_topics (exam_id, topic_id)"
                    " VALUES (?, ?)", (exam_id, topic_id))
            app.regenerate_exam_plan(conn, exam_id)
            first = conn.execute(
                "SELECT * FROM plan_sessions WHERE exam_id = ?"
                " AND kind = 'learn' ORDER BY session_date, start_time",
                (exam_id,)).fetchone()
            conn.execute(
                "UPDATE plan_sessions SET done = 1 WHERE id = ?",
                (first["id"],))
            done_key = (first["session_date"], first["start_time"],
                        first["end_time"], first["kind"])
            app.regenerate_exam_plan(conn, exam_id)
            kept = conn.execute(
                "SELECT * FROM plan_sessions WHERE id = ?",
                (first["id"],)).fetchone()
            self.assertIsNotNone(kept)
            self.assertEqual(kept["done"], 1)
            self.assertEqual(
                (kept["session_date"], kept["start_time"],
                 kept["end_time"], kept["kind"]),
                done_key)

    # --- Fix 2: no practice before learning ---------------------------

    def test_exercise_and_review_never_before_first_learn(self):
        plan = app.generate_plan(
            _topics(6), _all_week_slots(), date.today() + timedelta(days=7)
        )
        first_learn = {}
        for s in plan["sessions"]:
            if s["kind"] == "learn":
                first_learn.setdefault(s["topic_id"], s["session_date"])
        for s in plan["sessions"]:
            if s["kind"] in ("exercise", "review"):
                self.assertIsNotNone(
                    s["topic_id"],
                    f"{s['kind']} session without a topic: {s}")
                self.assertGreaterEqual(
                    s["session_date"], first_learn[s["topic_id"]],
                    f"{s['kind']} for {s['topic_title']} on "
                    f"{s['session_date']} is before its first learn "
                    f"on {first_learn[s['topic_id']]}")

    def test_no_learn_means_no_exercise_or_review(self):
        # 0% learning: no topic is ever learned, so there is
        # nothing to practise or review — only the simulation
        # (which has no topic) may still exist.
        plan = app.generate_plan(
            _topics(3), _all_week_slots(), date.today() + timedelta(days=7),
            learn_pct=0, review_pct=40, exercise_pct=30, simulation_pct=30,
        )
        kinds = [s["kind"] for s in plan["sessions"]]
        self.assertNotIn("exercise", kinds)
        self.assertNotIn("review", kinds)
        self.assertIn("simulation", kinds)

    # --- Fix 3: focus mode, every kind --------------------------------

    def test_focus_mode_has_no_mixed_subject_day_for_any_kind(self):
        calc_id = self._course_id()
        alg_id = self._course_id("Algorithms")
        self._add_slots()
        calc_topics = self._add_topics(
            calc_id, "Limits", "Derivatives", "Integrals")
        alg_topics = self._add_topics(
            alg_id, "Sorting", "Graphs", "DP")
        exam_a = self._add_exam(calc_id, "Calculus exam")
        exam_b = self._add_exam(alg_id, "Algorithms exam")
        with app.connect() as conn:
            for topic_id in calc_topics:
                conn.execute(
                    "INSERT INTO exam_topics (exam_id, topic_id)"
                    " VALUES (?, ?)", (exam_a, topic_id))
            for topic_id in alg_topics:
                conn.execute(
                    "INSERT INTO exam_topics (exam_id, topic_id)"
                    " VALUES (?, ?)", (exam_b, topic_id))
            app.save_prefs(conn, 50, 10, "any", "focus")
            app.regenerate_all_exam_plans(conn)
            sessions = self._all_plan_sessions(conn)
            by_date = {}
            for s in sessions:
                by_date.setdefault(s["session_date"], set()).add(
                    s["exam_id"])
            self.assertTrue(by_date)
            for day, exams in by_date.items():
                self.assertEqual(
                    len(exams), 1,
                    f"mixed-subject day {day}: exams {exams}")
            # And the same setup in variety mode DOES mix subjects
            # on at least one day (the check can tell the difference).
            app.save_prefs(conn, 50, 10, "any", "variety")
            app.regenerate_all_exam_plans(conn)
            sessions = self._all_plan_sessions(conn)
            by_date = {}
            for s in sessions:
                by_date.setdefault(s["session_date"], set()).add(
                    s["exam_id"])
            self.assertTrue(any(len(exams) > 1
                                for exams in by_date.values()))

    # --- Fix 4: busy block moves sessions ------------------------------

    def test_busy_block_moves_exam_sessions(self):
        calc_id = self._course_id()
        self._add_slots()
        topic_ids = self._add_topics(calc_id, "Limits", "Derivatives")
        exam_id = self._add_exam(calc_id, "Calculus exam", learn_pct=100,
                                 review_pct=0, exercise_pct=0,
                                 simulation_pct=0)
        with app.connect() as conn:
            for topic_id in topic_ids:
                conn.execute(
                    "INSERT INTO exam_topics (exam_id, topic_id)"
                    " VALUES (?, ?)", (exam_id, topic_id))
            app.regenerate_exam_plan(conn, exam_id)
            before = app.get_plan_sessions(conn, exam_id)
            # 2 learning sessions + the break between them.
            self.assertEqual(len(before), 3)
            self.assertTrue(any(
                s["session_date"] == date.today().isoformat()
                for s in before))
            # Block the whole of today: the sessions must move.
            conn.execute(
                "INSERT INTO busy_blocks"
                " (block_date, start_time, end_time, reason)"
                " VALUES (?, '09:00', '12:00', 'friends')",
                (date.today().isoformat(),))
            app.reschedule_everything(conn)
            after = app.get_plan_sessions(conn, exam_id)
            self.assertEqual(len(after), len(before))
            for s in after:
                self.assertFalse(
                    s["session_date"] == date.today().isoformat()
                    and s["start_time"] < "12:00"
                    and "09:00" < s["end_time"],
                    f"session still on the busy block: {dict(s)}")
            self.assertTrue(any(
                s["session_date"] != date.today().isoformat()
                for s in after))

    def test_busy_block_moves_assignment_blocks(self):
        calc_id = self._course_id()
        self._add_slots()
        with app.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO assignments"
                " (course_id, title, due_date, estimated_hours, num_parts)"
                " VALUES (?, 'Homework', ?, 2, 1)",
                (calc_id, (date.today() + timedelta(days=2)).isoformat()))
            assignment_id = cursor.lastrowid
            app.reschedule_assignment(conn, assignment_id)
            blocks = app.get_assignment_blocks(conn, assignment_id)
            self.assertEqual(len(blocks), 1)
            self.assertEqual(blocks[0]["block_date"],
                             date.today().isoformat())
            conn.execute(
                "INSERT INTO busy_blocks"
                " (block_date, start_time, end_time, reason)"
                " VALUES (?, '09:00', '12:00', 'friends')",
                (date.today().isoformat(),))
            app.reschedule_everything(conn)
            blocks = app.get_assignment_blocks(conn, assignment_id)
            self.assertEqual(len(blocks), 1)
            self.assertNotEqual(blocks[0]["block_date"],
                                date.today().isoformat())

    # --- Fix 7: review cap ---------------------------------------------

    def test_review_capped_at_two_per_day_and_each_has_a_topic(self):
        # A huge review bucket (70% of 7 days x 3 hours) used to
        # produce 71 identical reviews, 8 in one day. Now at most
        # 2 per day, and every review names the topic it reviews.
        # (Updated for QA round 3, fix 8: at most 3 reviews per
        # topic. With learn_pct=10 only 2 of the 4 topics get a
        # learning session, and only learned topics can be
        # reviewed.)
        # (Updated for spaced repetition, upgrade 10: the 2
        # learned topics are reviewed 1 and 3 days after learning
        # (+7 is past the exam, so it is skipped) — 2 topics x 2
        # reviews = 4, not 6.)
        plan = app.generate_plan(
            _topics(4), _all_week_slots(), date.today() + timedelta(days=7),
            learn_pct=10, review_pct=70, exercise_pct=10, simulation_pct=10,
        )
        review = [s for s in plan["sessions"] if s["kind"] == "review"]
        self.assertEqual(len(review), 4)
        per_day = {}
        per_topic = {}
        for s in review:
            per_day[s["session_date"]] = \
                per_day.get(s["session_date"], 0) + 1
            per_topic[s["topic_id"]] = \
                per_topic.get(s["topic_id"], 0) + 1
            self.assertIsNotNone(s["topic_id"])
            self.assertTrue(s["topic_title"])
        self.assertTrue(all(count <= 2 for count in per_day.values()))
        self.assertTrue(all(count <= 3 for count in per_topic.values()))
        self.assertEqual(len(per_topic), 2)

    # --- Fix 9: now + next markers --------------------------------------

    def test_today_markers_now_and_next(self):
        today = date.today()
        items = [
            {"session_date": today, "start_time": "09:00",
             "end_time": "09:50", "done": 0, "kind": "learn"},
            {"session_date": today, "start_time": "10:00",
             "end_time": "10:50", "done": 0, "kind": "learn"},
            {"session_date": today, "start_time": "11:00",
             "end_time": "11:50", "done": 0, "kind": "learn"},
        ]
        # 09:30: first is now, second is next, third is neither.
        marked = app.annotate_today_items(items, 9 * 60 + 30)
        self.assertEqual(marked[0]["marker"], " 🔵 עכשיו")
        self.assertEqual(marked[1]["marker"], " ➡️ הבא בתור")
        self.assertEqual(marked[2]["marker"], "")
        self.assertEqual(marked[0]["marker_class"], " now")
        self.assertEqual(marked[1]["marker_class"], " next")
        # 08:00, before everything: first is next, none is now.
        marked = app.annotate_today_items(items, 8 * 60)
        self.assertEqual(marked[0]["marker"], " ➡️ הבא בתור")
        self.assertEqual(marked[0]["marker_class"], " next")
        self.assertEqual(marked[1]["marker"], "")
        self.assertEqual(marked[2]["marker"], "")
        # The current item is already done: no "now", the next
        # upcoming not-done item is still "next".
        items[0]["done"] = 1
        marked = app.annotate_today_items(items, 9 * 60 + 30)
        self.assertEqual(marked[0]["marker"], "")
        self.assertEqual(marked[1]["marker"], " ➡️ הבא בתור")
        # After everything: no markers at all.
        marked = app.annotate_today_items(items, 23 * 60)
        self.assertTrue(all(m["marker"] == "" for m in marked))

    def test_today_page_shows_now_marker(self):
        # An all-day item always covers "now", so the page shows
        # the עכשיו marker however late the test runs.
        calc_id = self._course_id()
        with app.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO exams (course_id, kind, title, exam_date)"
                " VALUES (?, 'exam', 'Final', ?)",
                (calc_id, (date.today() + timedelta(days=7)).isoformat()))
            exam_id = cursor.lastrowid
            conn.execute(
                "INSERT INTO plan_sessions"
                " (exam_id, topic_id, session_date, start_time, end_time,"
                "  kind, done) VALUES (?, NULL, ?, '00:00', '23:59',"
                "  'learn', 0)",
                (exam_id, date.today().isoformat()))
            html = app.page(
                app.get_courses(conn), app.get_assignments(conn),
                exams=app.get_exams(conn),
                sessions_by_exam={exam_id: app.get_plan_sessions(
                    conn, exam_id)})
        self.assertIn("עכשיו", html)

    # --- Fix 5: the 0% message points to the edit form -----------------

    def test_zero_learn_message_points_to_edit_not_recreate(self):
        with app.connect() as conn:
            calc_id = self._course_id()
            topic_ids = self._add_topics(calc_id, "Limits")
            cursor = conn.execute(
                "INSERT INTO exams (course_id, kind, title, exam_date,"
                " learn_pct, review_pct, exercise_pct, simulation_pct)"
                " VALUES (?, 'exam', 'Final', ?, 0, 40, 30, 30)",
                (calc_id, (date.today() + timedelta(days=7)).isoformat()))
            exam_id = cursor.lastrowid
            exams = app.get_exams(conn)
            topics = [dict(r) for r in conn.execute(
                "SELECT * FROM topics WHERE id = ?", (topic_ids[0],))]
            html = app.page(
                app.get_courses(conn), app.get_assignments(conn),
                topics=app.get_topics(conn), exams=exams,
                unscheduled_by_exam={exam_id: topics},
                unscheduled_reason_by_exam={
                    exam_id: "learning share is 0%"},
                session_counts_by_exam={exam_id: {}})
        self.assertNotIn("הוסף את המבחן מחדש", html)
        self.assertIn("ערוך את אחוז הלמידה", html)
        self.assertIn(f"/exam?id={exam_id}", html)


class ServerTest(unittest.TestCase):
    """Live HTTP checks: real requests against a server on a temp DB,
    so the 303 Location headers and the wizard flow are the real
    ones, not a re-implementation of them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        app.DB_PATH = os.path.join(self.tmp.name, "test.db")
        app.init_db()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _request(self, method, path, fields=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        body = urlencode(fields or {}, doseq=True) if fields else None
        headers = {"Content-Type": "application/x-www-form-urlencoded"} \
            if body else {}
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read().decode()
        location = resp.getheader("Location")
        status = resp.status
        conn.close()
        return status, location, data

    def _course_id(self, name="Calculus 2"):
        with app.connect() as conn:
            return conn.execute(
                "SELECT id FROM courses WHERE name = ?", (name,)
            ).fetchone()["id"]

    def test_save_redirects_back_to_its_section(self):
        status, location, _ = self._request("POST", "/add-course", {
            "name": "Physics", "teacher": ""})
        self.assertEqual(status, 303)
        # QA round 2, fix 10: a successful save redirects with
        # ?saved=1, so the page says "נשמר ✓".
        self.assertEqual(location, "/?open=courses&saved=1#courses")
        status, location, _ = self._request("POST", "/add-slot", {
            "weekday": "0", "start_time": "09:00", "end_time": "12:00"})
        self.assertEqual(status, 303)
        self.assertEqual(location, "/?open=slots&saved=1#slots")
        status, location, _ = self._request("POST", "/save-prefs", {
            "session_minutes": "50", "break_minutes": "10",
            "homework_pref": "morning"})
        self.assertEqual(status, 303)
        self.assertEqual(location, "/?open=routine&saved=1#routine")
        # Following the redirect shows the confirmation.
        _, _, data = self._request("GET", location)
        self.assertIn("נשמר ✓", data)

    def test_home_page_opens_section_from_query(self):
        status, _, data = self._request("GET", "/?open=exams#exams")
        self.assertEqual(status, 200)
        self.assertIn('<details class="card" id="exams" open>', data)

    def test_quiz_error_renders_with_exams_section_open(self):
        status, _, data = self._request("POST", "/add-exam", {
            "course_id": str(self._course_id()), "kind": "quiz",
            "title": "Bad Quiz",
            "exam_date": (date.today() + timedelta(days=7)).isoformat(),
            "learn_pct": "40", "review_pct": "20",
            "exercise_pct": "20", "simulation_pct": "20"})
        self.assertEqual(status, 200)  # error page, not a redirect
        self.assertIn("בוחן חייב לכלול לפחות נושא אחד", data)
        self.assertIn('<details class="card" id="exams" open>', data)
        with app.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM exams").fetchone()[0], 0)

    def test_wizard_steps_render_save_and_advance(self):
        status, _, data = self._request("GET", "/setup")
        self.assertEqual(status, 200)
        self.assertIn("שלב 1 מתוך 6", data)
        self.assertIn("/setup?step=2", data)  # skip link
        self.assertIn("חזרה למתכנן", data)
        # Step 4 (routine) saves and advances to step 5, with
        # "נשמר ✓" on the step it lands on.
        status, location, _ = self._request("POST", "/save-prefs", {
            "session_minutes": "45", "break_minutes": "5",
            "homework_pref": "evening", "next": "/setup?step=5"})
        self.assertEqual(status, 303)
        self.assertEqual(location, "/setup?step=5&saved=1")
        status, _, data = self._request("GET", "/setup?step=5&saved=1")
        self.assertIn("שלב 5 מתוך 6", data)
        self.assertIn("נשמר ✓", data)
        # Step 3 saves and advances to step 4 (the wizard's forms
        # now point at the next step): the back link is how a
        # second slot is added.
        status, location, _ = self._request("POST", "/add-slot", {
            "weekday": "0", "start_time": "09:00", "end_time": "12:00",
            "next": "/setup?step=4", "wizard_step": "3"})
        self.assertEqual(status, 303)
        self.assertEqual(location, "/setup?step=4&saved=1")
        status, _, data = self._request("GET", "/setup?step=6")
        self.assertEqual(status, 200)
        self.assertIn("שלב 6 מתוך 6", data)
        self.assertIn("1 חלונות פנויים", data)

    def test_wizard_quiz_error_stays_on_step_5(self):
        with app.connect() as conn:
            conn.execute(
                "INSERT INTO topics (course_id, title) VALUES (?, 'Limits')",
                (self._course_id(),),
            )
        status, _, data = self._request("POST", "/add-exam", {
            "course_id": str(self._course_id()), "kind": "quiz",
            "title": "Bad Quiz",
            "exam_date": (date.today() + timedelta(days=7)).isoformat(),
            "learn_pct": "40", "review_pct": "20",
            "exercise_pct": "20", "simulation_pct": "20",
            "next": "/setup?step=6", "wizard_step": "5"})
        self.assertEqual(status, 200)
        self.assertIn("שלב 5 מתוך 6", data)
        self.assertIn("בוחן חייב לכלול לפחות נושא אחד", data)
        with app.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM exams").fetchone()[0], 0)

    def test_setup_banner_appears_with_no_topics(self):
        _, _, data = self._request("GET", "/")
        self.assertIn('<p class="setup-banner">', data)
        with app.connect() as conn:
            conn.execute(
                "INSERT INTO topics (course_id, title) VALUES (?, 'Limits')",
                (self._course_id(),),
            )
        _, _, data = self._request("GET", "/")
        self.assertNotIn('<p class="setup-banner">', data)

    # --- Fix batch: dates, exam page, missed work --------------------

    def test_year_1212_exam_date_is_rejected(self):
        status, _, data = self._request("POST", "/add-exam", {
            "course_id": str(self._course_id()), "kind": "exam",
            "title": "Ancient Exam", "exam_date": "1212-06-01",
            "learn_pct": "40", "review_pct": "20",
            "exercise_pct": "20", "simulation_pct": "20"})
        self.assertEqual(status, 200)  # error page, not a redirect
        self.assertIn("התאריך לא הגיוני", data)
        with app.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM exams").fetchone()[0], 0)

    def test_bad_assignment_and_busy_dates_are_rejected(self):
        status, _, data = self._request("POST", "/add-assignment", {
            "course_id": str(self._course_id()), "title": "Ancient HW",
            "due_date": "1212-01-01", "estimated_hours": "2",
            "num_parts": "1"})
        self.assertEqual(status, 200)
        self.assertIn("התאריך לא הגיוני", data)
        with app.connect() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM assignments").fetchone()[0], 0)
        status, _, data = self._request("POST", "/add-busy", {
            "block_date": "1212-01-01", "start_time": "18:00",
            "end_time": "21:00", "reason": "friends"})
        self.assertEqual(status, 200)
        self.assertIn("התאריך לא הגיוני", data)
        with app.connect() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM busy_blocks").fetchone()[0], 0)
        # A real date is accepted.
        status, location, _ = self._request("POST", "/add-assignment", {
            "course_id": str(self._course_id()), "title": "Real HW",
            "due_date": (date.today() + timedelta(days=7)).isoformat(),
            "estimated_hours": "2", "num_parts": "1"})
        self.assertEqual(status, 303)
        self.assertEqual(location, "/?open=assignments&saved=1#assignments")

    def test_exam_page_shows_scope_and_importance_and_saves(self):
        with app.connect() as conn:
            calc_id = self._course_id()
            conn.execute(
                "INSERT INTO topics (course_id, title, importance)"
                " VALUES (?, 'Limits', 'high'), (?, 'Derivatives',"
                " 'medium')",
                (calc_id, calc_id))
            topic_ids = [r["id"] for r in conn.execute(
                "SELECT id FROM topics ORDER BY id").fetchall()]
            conn.execute(
                "INSERT INTO free_slots (weekday, start_time, end_time)"
                " VALUES (?, '09:00', '12:00')", (date.today().weekday(),))
        # Create a quiz scoped to Limits only.
        status, location, _ = self._request("POST", "/add-exam", {
            "course_id": str(calc_id), "kind": "quiz", "title": "Midterm",
            "exam_date": (date.today() + timedelta(days=7)).isoformat(),
            "learn_pct": "100", "review_pct": "0",
            "exercise_pct": "0", "simulation_pct": "0",
            "topic_id": str(topic_ids[0])})
        self.assertEqual(status, 303)
        with app.connect() as conn:
            exam_id = conn.execute(
                "SELECT id FROM exams").fetchone()["id"]
        # The exam page shows both topics, their importance, and
        # which one is in scope — scope and importance together.
        status, _, data = self._request("GET", f"/exam?id={exam_id}")
        self.assertEqual(status, 200)
        self.assertIn("Midterm", data)
        self.assertIn("Limits", data)
        self.assertIn("Derivatives", data)
        self.assertIn("מאוד חשוב", data)
        self.assertIn("די חשוב", data)
        self.assertIn("/save-exam-scope", data)
        self.assertIn("/topic-importance?", data)
        self.assertIn(f'value="{topic_ids[0]}" checked', data)
        # Save a wider scope: both topics, and a plan is generated.
        status, location, _ = self._request("POST", "/save-exam-scope", {
            "exam_id": str(exam_id),
            "topic_id": [str(t) for t in topic_ids]})
        self.assertEqual(status, 303)
        self.assertEqual(location, f"/exam?id={exam_id}&saved=1")
        with app.connect() as conn:
            scoped = conn.execute(
                "SELECT COUNT(*) FROM exam_topics WHERE exam_id = ?",
                (exam_id,)).fetchone()[0]
            self.assertEqual(scoped, 2)
            learn = conn.execute(
                "SELECT COUNT(*) FROM plan_sessions WHERE exam_id = ?"
                " AND kind = 'learn'", (exam_id,)).fetchone()[0]
            self.assertEqual(learn, 2)
        # A quiz with zero topics is still rejected on the exam page.
        status, _, data = self._request("POST", "/save-exam-scope", {
            "exam_id": str(exam_id)})
        self.assertEqual(status, 200)
        self.assertIn("בוחן חייב לכלול לפחות נושא אחד", data)
        with app.connect() as conn:
            scoped = conn.execute(
                "SELECT COUNT(*) FROM exam_topics WHERE exam_id = ?",
                (exam_id,)).fetchone()[0]
            self.assertEqual(scoped, 2)

    def test_missed_section_appears_on_home_page(self):
        with app.connect() as conn:
            calc_id = self._course_id()
            conn.execute(
                "INSERT INTO topics (course_id, title) VALUES (?, 'Limits')",
                (calc_id,))
            topic_id = conn.execute(
                "SELECT id FROM topics").fetchone()["id"]
            conn.execute(
                "INSERT INTO exams (course_id, kind, title, exam_date)"
                " VALUES (?, 'exam', 'Final', ?)",
                (calc_id, (date.today() + timedelta(days=7)).isoformat()))
            exam_id = conn.execute(
                "SELECT id FROM exams").fetchone()["id"]
            conn.execute(
                "INSERT INTO plan_sessions"
                " (exam_id, topic_id, session_date, start_time, end_time,"
                "  kind, done) VALUES (?, ?, ?, '09:00', '09:50',"
                "  'learn', 0)",
                (exam_id, topic_id,
                 (date.today() - timedelta(days=1)).isoformat()))
        _, _, data = self._request("GET", "/")
        self.assertIn("פספסת", data)
        self.assertIn("תזמן מחדש", data)

    # --- QA fixes 4–6, 8 (live HTTP) ----------------------------------

    def _add_week_slots(self):
        with app.connect() as conn:
            for d in range(7):
                conn.execute(
                    "INSERT INTO free_slots (weekday, start_time, end_time)"
                    " VALUES (?, '09:00', '12:00')", (d,))

    def test_new_exam_has_plan_immediately(self):
        # Fix 6: no "create plan" click needed — the plan exists
        # right after the exam is saved, on the main form and in
        # the wizard's step 5 alike.
        calc_id = self._course_id()
        self._add_week_slots()
        with app.connect() as conn:
            conn.execute(
                "INSERT INTO topics (course_id, title)"
                " VALUES (?, 'Limits'), (?, 'Derivatives')",
                (calc_id, calc_id))
        status, location, _ = self._request("POST", "/add-exam", {
            "course_id": str(calc_id), "kind": "exam", "title": "Final",
            "exam_date": (date.today() + timedelta(days=7)).isoformat(),
            "learn_pct": "100", "review_pct": "0",
            "exercise_pct": "0", "simulation_pct": "0"})
        self.assertEqual(status, 303)
        self.assertEqual(location, "/?open=exams&saved=1#exams")
        with app.connect() as conn:
            sessions = conn.execute(
                "SELECT kind, COUNT(*) AS n FROM plan_sessions"
                " GROUP BY kind").fetchall()
            counts = {r["kind"]: r["n"] for r in sessions}
        # 2 learning sessions + the break between them.
        self.assertEqual(counts, {"learn": 2, "break": 1})
        # Wizard step 5: same handler, same immediate plan — and the
        # wizard advance now carries ?saved=1, so step 6 shows
        # "נשמר ✓" where the user lands.
        status, location, _ = self._request("POST", "/add-exam", {
            "course_id": str(calc_id), "kind": "exam",
            "title": "Second exam",
            "exam_date": (date.today() + timedelta(days=14)).isoformat(),
            "learn_pct": "100", "review_pct": "0",
            "exercise_pct": "0", "simulation_pct": "0",
            "next": "/setup?step=6", "wizard_step": "5"})
        self.assertEqual(status, 303)
        self.assertEqual(location, "/setup?step=6&saved=1")
        with app.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM plan_sessions").fetchone()[0]
        self.assertGreater(count, 2)

    def test_exam_edit_save_and_delete(self):
        # Fix 5: the exam page has a prefilled edit form and a
        # delete link; saving validates like creation and rebuilds
        # the plan; deleting removes sessions + scope + the exam.
        calc_id = self._course_id()
        self._add_week_slots()
        with app.connect() as conn:
            conn.execute(
                "INSERT INTO topics (course_id, title)"
                " VALUES (?, 'Limits'), (?, 'Derivatives')",
                (calc_id, calc_id))
        status, _, _ = self._request("POST", "/add-exam", {
            "course_id": str(calc_id), "kind": "exam", "title": "Final",
            "exam_date": (date.today() + timedelta(days=7)).isoformat(),
            "learn_pct": "100", "review_pct": "0",
            "exercise_pct": "0", "simulation_pct": "0"})
        self.assertEqual(status, 303)
        with app.connect() as conn:
            exam_id = conn.execute(
                "SELECT id FROM exams").fetchone()["id"]
        status, _, data = self._request("GET", f"/exam?id={exam_id}")
        self.assertEqual(status, 200)
        self.assertIn("עריכת המבחן", data)
        self.assertIn('value="Final"', data)
        self.assertIn("/save-exam", data)
        self.assertIn(f"/delete-exam?id={exam_id}", data)
        # Save an edit: new title, later date, new percentages.
        status, location, _ = self._request("POST", "/save-exam", {
            "exam_id": str(exam_id), "title": "Final v2",
            "exam_date": (date.today() + timedelta(days=14)).isoformat(),
            "kind": "exam", "learn_pct": "40", "review_pct": "20",
            "exercise_pct": "20", "simulation_pct": "20"})
        self.assertEqual(status, 303)
        self.assertEqual(location, f"/exam?id={exam_id}&saved=1")
        with app.connect() as conn:
            exam = conn.execute(
                "SELECT * FROM exams WHERE id = ?", (exam_id,)).fetchone()
            self.assertEqual(exam["title"], "Final v2")
            self.assertEqual(
                (exam["learn_pct"], exam["review_pct"],
                 exam["exercise_pct"], exam["simulation_pct"]),
                (40, 20, 20, 20))
            self.assertGreater(
                conn.execute(
                    "SELECT COUNT(*) FROM plan_sessions WHERE exam_id = ?",
                    (exam_id,)).fetchone()[0], 0)
        # Bad percentages are rejected and nothing is saved.
        status, _, data = self._request("POST", "/save-exam", {
            "exam_id": str(exam_id), "title": "Broken",
            "exam_date": (date.today() + timedelta(days=14)).isoformat(),
            "kind": "exam", "learn_pct": "50", "review_pct": "50",
            "exercise_pct": "50", "simulation_pct": "50"})
        self.assertEqual(status, 200)
        self.assertIn("אחוזי ההכנה חייבים להסתכם ב-100", data)
        with app.connect() as conn:
            title = conn.execute(
                "SELECT title FROM exams WHERE id = ?",
                (exam_id,)).fetchone()["title"]
        self.assertEqual(title, "Final v2")
        # A past date is rejected too.
        status, _, data = self._request("POST", "/save-exam", {
            "exam_id": str(exam_id), "title": "Broken",
            "exam_date": (date.today() - timedelta(days=1)).isoformat(),
            "kind": "exam", "learn_pct": "40", "review_pct": "20",
            "exercise_pct": "20", "simulation_pct": "20"})
        self.assertEqual(status, 200)
        self.assertIn("התאריך לא הגיוני", data)
        # 0% learning: the exam page shows the honest reason and
        # points at the edit form above, not at recreating.
        status, _, _ = self._request("POST", "/save-exam", {
            "exam_id": str(exam_id), "title": "Final v2",
            "exam_date": (date.today() + timedelta(days=14)).isoformat(),
            "kind": "exam", "learn_pct": "0", "review_pct": "40",
            "exercise_pct": "30", "simulation_pct": "30"})
        self.assertEqual(status, 303)
        _, _, data = self._request("GET", f"/exam?id={exam_id}")
        self.assertIn("אחוז הלמידה הוא 0%", data)
        self.assertIn("ערוך את אחוז הלמידה בטופס למעלה", data)
        self.assertNotIn("הוסף את המבחן מחדש", data)
        # Delete removes the exam, its scope and its sessions.
        status, location, _ = self._request(
            "GET", f"/delete-exam?id={exam_id}")
        self.assertEqual(status, 303)
        self.assertEqual(location, "/?open=exams#exams")
        with app.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM exams").fetchone()[0], 0)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM plan_sessions").fetchone()[0], 0)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM exam_topics").fetchone()[0], 0)

    def test_exam_edit_to_quiz_without_scope_is_rejected(self):
        calc_id = self._course_id()
        self._add_week_slots()
        with app.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO topics (course_id, title) VALUES (?, 'Limits')",
                (calc_id,))
            topic_id = cursor.lastrowid
        status, _, _ = self._request("POST", "/add-exam", {
            "course_id": str(calc_id), "kind": "quiz", "title": "Quiz",
            "exam_date": (date.today() + timedelta(days=7)).isoformat(),
            "learn_pct": "100", "review_pct": "0",
            "exercise_pct": "0", "simulation_pct": "0",
            "topic_id": str(topic_id)})
        self.assertEqual(status, 303)
        with app.connect() as conn:
            exam_id = conn.execute(
                "SELECT id FROM exams").fetchone()["id"]
            # Drop the scope behind the form's back, then try to
            # save it as a quiz again: a quiz with no topics must
            # be rejected, exactly like at creation.
            conn.execute(
                "DELETE FROM exam_topics WHERE exam_id = ?", (exam_id,))
        status, _, data = self._request("POST", "/save-exam", {
            "exam_id": str(exam_id), "title": "Quiz",
            "exam_date": (date.today() + timedelta(days=7)).isoformat(),
            "kind": "quiz", "learn_pct": "100", "review_pct": "0",
            "exercise_pct": "0", "simulation_pct": "0"})
        self.assertEqual(status, 200)
        self.assertIn("בוחן חייב לכלול לפחות נושא אחד", data)

    def test_assignment_edit_save_and_delete(self):
        # Fix 5: an assignment has its own edit page; saving
        # validates like creation and reschedules the blocks;
        # deleting removes the assignment and its blocks.
        calc_id = self._course_id()
        self._add_week_slots()
        status, _, _ = self._request("POST", "/add-assignment", {
            "course_id": str(calc_id), "title": "HW1",
            "due_date": (date.today() + timedelta(days=7)).isoformat(),
            "estimated_hours": "2", "num_parts": "1"})
        self.assertEqual(status, 303)
        with app.connect() as conn:
            assignment_id = conn.execute(
                "SELECT id FROM assignments").fetchone()["id"]
            blocks = app.get_assignment_blocks(conn, assignment_id)
        self.assertEqual(len(blocks), 1)
        status, _, data = self._request(
            "GET", f"/assignment?id={assignment_id}")
        self.assertEqual(status, 200)
        self.assertIn("עריכת המטלה", data)
        self.assertIn('value="HW1"', data)
        self.assertIn("/save-assignment", data)
        self.assertIn(f"/delete-assignment?id={assignment_id}", data)
        # Save an edit: 4 hours in 2 parts reschedules to 2 blocks.
        status, location, _ = self._request("POST", "/save-assignment", {
            "assignment_id": str(assignment_id), "title": "HW1 revised",
            "due_date": (date.today() + timedelta(days=7)).isoformat(),
            "estimated_hours": "4", "num_parts": "2"})
        self.assertEqual(status, 303)
        self.assertEqual(location, "/?open=assignments&saved=1#assignments")
        with app.connect() as conn:
            row = conn.execute(
                "SELECT * FROM assignments WHERE id = ?",
                (assignment_id,)).fetchone()
            self.assertEqual(row["title"], "HW1 revised")
            self.assertEqual(row["estimated_hours"], 4)
            blocks = app.get_assignment_blocks(conn, assignment_id)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(
            sum(app._to_minutes(b["end_time"])
                - app._to_minutes(b["start_time"]) for b in blocks),
            240)
        # A nonsense due date is rejected and nothing is saved.
        status, _, data = self._request("POST", "/save-assignment", {
            "assignment_id": str(assignment_id), "title": "Broken",
            "due_date": "1212-01-01",
            "estimated_hours": "4", "num_parts": "2"})
        self.assertEqual(status, 200)
        self.assertIn("התאריך לא הגיוני", data)
        with app.connect() as conn:
            title = conn.execute(
                "SELECT title FROM assignments WHERE id = ?",
                (assignment_id,)).fetchone()["title"]
        self.assertEqual(title, "HW1 revised")
        # Delete removes the assignment and its blocks.
        status, location, _ = self._request(
            "GET", f"/delete-assignment?id={assignment_id}")
        self.assertEqual(status, 303)
        # A delete is not a save: no "נשמר ✓" in its redirect.
        self.assertEqual(location, "/?open=assignments#assignments")
        with app.connect() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM assignments").fetchone()[0], 0)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM assignment_blocks"
                    ).fetchone()[0], 0)

    def test_busy_block_post_moves_sessions(self):
        # Fix 4: adding a busy block through the real route moves
        # the sessions that sat on the blocked time.
        calc_id = self._course_id()
        self._add_week_slots()
        with app.connect() as conn:
            conn.execute(
                "INSERT INTO topics (course_id, title)"
                " VALUES (?, 'Limits'), (?, 'Derivatives')",
                (calc_id, calc_id))
        status, _, _ = self._request("POST", "/add-exam", {
            "course_id": str(calc_id), "kind": "exam", "title": "Final",
            "exam_date": (date.today() + timedelta(days=7)).isoformat(),
            "learn_pct": "100", "review_pct": "0",
            "exercise_pct": "0", "simulation_pct": "0"})
        self.assertEqual(status, 303)
        with app.connect() as conn:
            exam_id = conn.execute(
                "SELECT id FROM exams").fetchone()["id"]
            before = app.get_plan_sessions(conn, exam_id)
        self.assertTrue(any(
            s["session_date"] == date.today().isoformat()
            for s in before))
        status, location, _ = self._request("POST", "/add-busy", {
            "block_date": date.today().isoformat(),
            "start_time": "09:00", "end_time": "12:00",
            "reason": "friends"})
        self.assertEqual(status, 303)
        self.assertEqual(location, "/?open=busy&saved=1#busy")
        with app.connect() as conn:
            after = app.get_plan_sessions(conn, exam_id)
        self.assertEqual(len(after), len(before))
        for s in after:
            self.assertFalse(
                s["session_date"] == date.today().isoformat()
                and s["start_time"] < "12:00"
                and "09:00" < s["end_time"])

    def test_exam_page_groups_sessions_by_date(self):
        # Fix 8: the exam page groups sessions by date (one
        # summary line per day), like the home page's "בהמשך" —
        # each session date appears exactly once, as a heading.
        calc_id = self._course_id()
        self._add_week_slots()
        with app.connect() as conn:
            conn.execute(
                "INSERT INTO topics (course_id, title)"
                " VALUES (?, 'Limits'), (?, 'Derivatives'),"
                " (?, 'Integrals'), (?, 'Series')",
                (calc_id, calc_id, calc_id, calc_id))
        status, _, _ = self._request("POST", "/add-exam", {
            "course_id": str(calc_id), "kind": "exam", "title": "Final",
            "exam_date": (date.today() + timedelta(days=7)).isoformat(),
            "learn_pct": "100", "review_pct": "0",
            "exercise_pct": "0", "simulation_pct": "0"})
        self.assertEqual(status, 303)
        with app.connect() as conn:
            exam_id = conn.execute(
                "SELECT id FROM exams").fetchone()["id"]
            session_dates = {
                s["session_date"]
                for s in app.get_plan_sessions(conn, exam_id)
            }
        self.assertGreaterEqual(len(session_dates), 2)
        _, _, data = self._request("GET", f"/exam?id={exam_id}")
        self.assertEqual(data.count("פריטים"), len(session_dates))
        for session_date in session_dates:
            self.assertEqual(data.count(session_date), 1,
                             f"date {session_date} should appear once")


class QARound2Test(unittest.TestCase):
    """QA round 2 fixes: 12 more bugs found by testing, all fixed.

    Live HTTP checks run against a server on a temp DB, like
    ServerTest, so the 200/303 statuses and Location headers are
    the real ones."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        app.DB_PATH = os.path.join(self.tmp.name, "test.db")
        app.init_db()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _request(self, method, path, fields=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        body = urlencode(fields or {}, doseq=True) if fields else None
        headers = {"Content-Type": "application/x-www-form-urlencoded"} \
            if body else {}
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read().decode()
        location = resp.getheader("Location")
        status = resp.status
        conn.close()
        return status, location, data

    def _course_id(self, name="Calculus 2"):
        with app.connect() as conn:
            return conn.execute(
                "SELECT id FROM courses WHERE name = ?", (name,)
            ).fetchone()["id"]

    def _add_week_slots(self):
        with app.connect() as conn:
            conn.execute(
                "INSERT INTO free_slots (weekday, start_time, end_time)"
                " VALUES (?, ?, ?), (?, ?, ?), (?, ?, ?), (?, ?, ?),"
                " (?, ?, ?), (?, ?, ?), (?, ?, ?)",
                tuple(v for d in range(7) for v in (d, "09:00", "12:00")))

    def _add_topics(self, course_id, *titles):
        with app.connect() as conn:
            ids = []
            for title in titles:
                cursor = conn.execute(
                    "INSERT INTO topics (course_id, title) VALUES (?, ?)",
                    (course_id, title),
                )
                ids.append(cursor.lastrowid)
            return ids

    # --- Fix 1: bad free slots are rejected, bad rows cannot crash ---

    def test_validate_slot_rejects_bad_weekday_and_times(self):
        self.assertIsNone(app.validate_slot(0, "09:00", "12:00"))
        self.assertIsNone(app.validate_slot("3", "09:00", "12:00"))
        self.assertIsNotNone(app.validate_slot(7, "09:00", "12:00"))
        self.assertIsNotNone(app.validate_slot(-1, "09:00", "12:00"))
        self.assertIsNotNone(app.validate_slot("x", "09:00", "12:00"))
        self.assertIsNotNone(app.validate_slot("", "09:00", "12:00"))
        self.assertIsNotNone(app.validate_slot(0, "24:00", "25:00"))
        self.assertIsNotNone(app.validate_slot(0, "25:00", "26:00"))
        self.assertIsNotNone(app.validate_slot(0, "09:00", "09:00"))
        self.assertIsNotNone(app.validate_slot(0, "12:00", "09:00"))
        self.assertTrue(app.is_valid_time("00:00"))
        self.assertTrue(app.is_valid_time("23:59"))
        # "9:00" is a real time, just without a leading zero.
        self.assertTrue(app.is_valid_time("9:00"))
        self.assertFalse(app.is_valid_time("24:00"))
        self.assertFalse(app.is_valid_time("25:00"))
        self.assertFalse(app.is_valid_time(""))
        self.assertFalse(app.is_valid_time(None))

    def test_weekday_name_is_defensive(self):
        self.assertEqual(app.weekday_name(0), "שני")
        self.assertEqual(app.weekday_name(6), "ראשון")
        self.assertEqual(app.weekday_name(7), "יום לא תקין")
        self.assertEqual(app.weekday_name(-1), "יום לא תקין")
        self.assertEqual(app.weekday_name("x"), "יום לא תקין")
        self.assertEqual(app.weekday_name(None), "יום לא תקין")

    def test_bad_slot_post_is_rejected_live_and_page_still_loads(self):
        # The QA repro: weekday=7 used to be stored and then crash
        # the home page. Now it is rejected with a Hebrew error.
        status, _, data = self._request("POST", "/add-slot", {
            "weekday": "7", "start_time": "09:00", "end_time": "10:00"})
        self.assertEqual(status, 200)
        self.assertIn("יום בשבוע", data)
        status, _, data = self._request("POST", "/add-slot", {
            "weekday": "0", "start_time": "25:00", "end_time": "26:00"})
        self.assertEqual(status, 200)
        self.assertIn("שעות אמיתיות", data)
        with app.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM free_slots")
                .fetchone()[0], 0)
        # A bad row stored before validation existed (inserted
        # directly, as the old code would have) must not crash
        # the home page: it shows "יום לא תקין" instead.
        with app.connect() as conn:
            conn.execute(
                "INSERT INTO free_slots (weekday, start_time, end_time)"
                " VALUES (7, '09:00', '10:00')")
            conn.execute(
                "INSERT INTO free_slots (weekday, start_time, end_time)"
                " VALUES (0, '09:00', '10:00')")
        status, _, data = self._request("GET", "/slots")
        self.assertEqual(status, 200)
        self.assertIn("יום לא תקין", data)
        self.assertIn("שני 09:00", data)

    def test_generate_plan_skips_bad_slot_times(self):
        # A slot with a garbage time (stored before validation)
        # is skipped by the planner instead of crashing it.
        slots = [
            {"weekday": date.today().weekday(),
             "start_time": "25:00", "end_time": "26:00"},
            {"weekday": date.today().weekday(),
             "start_time": "09:00", "end_time": "12:00"},
        ]
        plan = app.generate_plan(
            _topics(1), slots, date.today() + timedelta(days=2),
            learn_pct=100, review_pct=0, exercise_pct=0, simulation_pct=0)
        learn = [s for s in plan["sessions"] if s["kind"] == "learn"]
        self.assertEqual(len(learn), 1)
        self.assertEqual(learn[0]["start_time"], "09:00")

    # --- Fix 2: an exam with no topics gets zero sessions ----------

    def test_exam_with_no_topics_gets_zero_sessions(self):
        plan = app.generate_plan(
            [], _all_week_slots(), date.today() + timedelta(days=7))
        self.assertEqual(plan["sessions"], [])
        self.assertEqual(plan["unscheduled"], [])
        self.assertTrue(plan["empty_scope"])
        self.assertEqual(plan["session_counts"],
                         {"learn": 0, "review": 0, "exercise": 0,
                          "simulation": 0, "break": 0})

    def test_exam_on_empty_course_live(self):
        # A course with zero topics + an exam used to produce 22
        # meaningless reviews + 1 simulation. Now: zero sessions,
        # and the exam page says so plainly.
        status, _, _ = self._request("POST", "/add-course", {
            "name": "EmptyCourse", "teacher": ""})
        self.assertEqual(status, 303)
        with app.connect() as conn:
            empty_id = conn.execute(
                "SELECT id FROM courses WHERE name = 'EmptyCourse'"
            ).fetchone()["id"]
        status, location, _ = self._request("POST", "/add-exam", {
            "course_id": str(empty_id), "kind": "exam",
            "title": "Final", "exam_date": (
                date.today() + timedelta(days=14)).isoformat(),
            "learn_pct": "40", "review_pct": "20",
            "exercise_pct": "20", "simulation_pct": "20"})
        self.assertEqual(status, 303)
        with app.connect() as conn:
            exam_id = conn.execute(
                "SELECT id FROM exams").fetchone()["id"]
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM plan_sessions WHERE exam_id = ?",
                    (exam_id,)).fetchone()[0], 0)
        _, _, data = self._request("GET", f"/exam?id={exam_id}")
        self.assertIn("אין נושאים בקורס", data)
        _, _, data = self._request("GET", "/")
        self.assertIn("אין נושאים בקורס", data)

    # --- Fix 3: creating without an existing course -----------------

    def test_course_exists(self):
        with app.connect() as conn:
            calc_id = self._course_id()
            self.assertTrue(app.course_exists(conn, calc_id))
            self.assertTrue(app.course_exists(conn, str(calc_id)))
            self.assertFalse(app.course_exists(conn, ""))
            self.assertFalse(app.course_exists(conn, "999"))
            self.assertFalse(app.course_exists(conn, None))
            self.assertFalse(app.course_exists(conn, "x"))

    def test_add_forms_hidden_with_no_courses(self):
        with app.connect() as conn:
            conn.execute("DELETE FROM courses")
            html = app.page(
                app.get_courses(conn), app.get_assignments(conn))
        self.assertIn("הוסף קורס קודם", html)
        self.assertNotIn('action="/add-assignment"', html)
        self.assertNotIn('action="/add-topic"', html)
        self.assertNotIn('action="/add-exam"', html)
        self.assertNotIn('action="/parse-syllabus"', html)

    def test_empty_course_id_shows_hebrew_error_live(self):
        # The QA repro: course_id="" used to raise an unhandled
        # database error and drop the connection. Now: a Hebrew
        # error page, status 200, nothing saved, server alive.
        for path, fields in (
            ("/add-exam", {
                "course_id": "", "kind": "exam", "title": "No course",
                "exam_date": (date.today() + timedelta(days=7)).isoformat(),
                "learn_pct": "40", "review_pct": "20",
                "exercise_pct": "20", "simulation_pct": "20"}),
            ("/add-assignment", {
                "course_id": "", "title": "No course",
                "due_date": (date.today() + timedelta(days=7)).isoformat(),
                "estimated_hours": "2", "num_parts": "1"}),
            ("/add-topic", {"course_id": "", "title": "No course"}),
            ("/add-exam", {
                "course_id": "999", "kind": "exam", "title": "Ghost",
                "exam_date": (date.today() + timedelta(days=7)).isoformat(),
                "learn_pct": "40", "review_pct": "20",
                "exercise_pct": "20", "simulation_pct": "20"}),
        ):
            status, _, data = self._request("POST", path, fields)
            self.assertEqual(status, 200, path)
            self.assertIn("בחר קורס קיים", data, path)
        with app.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM exams").fetchone()[0], 0)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM assignments").fetchone()[0], 0)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0], 0)
        # And the server is still alive afterwards.
        status, _, _ = self._request("GET", "/")
        self.assertEqual(status, 200)

    # --- Fix 4: messy syllabus paste loses its junk lines ----------

    def test_parse_syllabus_drops_junk_lines(self):
        # The QA repro paste: 12 lines, 7 of them junk.
        text = (
            "קורס חדו\"א 2 — סילבוס\n"
            "מרצה: ד\"ר לנדסמן\n"
            "Week 1: Limits and Continuity\n"
            "Week 2: Derivatives\n"
            "Midterm: 20.11.2026\n"
            "Office hours: Sunday 10:00\n"
            "Grading: 80% exam, 20% homework\n"
            "Integrals\n"
            "ספר: Calculus by Stewart\n"
            "Series\n"
        )
        self.assertEqual(
            app.parse_syllabus(text),
            ["Limits and Continuity", "Derivatives", "Integrals",
             "Series"])

    def test_parse_syllabus_keeps_real_topics(self):
        self.assertEqual(
            app.parse_syllabus("1. Limits\n2) Derivatives\n- Integrals\n"),
            ["Limits", "Derivatives", "Integrals"])

    # --- BGU page paste: 261 junk topics -> the 13 weekly ones -----

    def test_parse_syllabus_bgu_page_returns_weekly_topics(self):
        # Small redacted fixture modelled on Daniel's real BGU
        # course-page paste: admin header, staff table, then the
        # weekly-topics table, then the bibliography.
        text = (
            "נגישות\n"
            "חדו\"א 2 למדעי המחשב והנדסת תוכנה\n"
            "פרטי הקורס\n"
            "מספר הקורס\n"
            "0-212.1.2371\n"
            "שנה אקדמית\n"
            "תשפ\"ז\n"
            "צוות הקורס\n"
            "פרופ' י. אופנהיים\n"
            "izharo@bgu.ac.il\n"
            "שעור\n"
            "היברידי\n"
            "18.10.2026-15.01.2027\n"
            "[32] גוטמן\n"
            "חדר 307\n"
            "יום שני\n"
            "12:00-14:00\n"
            "מר א. טסלר\n"
            "תיאור הקורס\n"
            "אינטגרלים במשתנה אחד וחשבון דיפרציאלי\n"
            "נושאי הקורס בחלוקה לשבועות\n"
            "תאריך\n"
            "נושא\n"
            "מקורות לשיעור\n"
            "1.\n"
            "האינטגרל המסוים וסכומי רימן\n"
            "Multivariable Calculus with Applications Lax-Terrell. פרק 4\n"
            "2.\n"
            "פונקציות קדומות והמשפט היסודי של חדו\"א\n"
            "Multivariable Calculus with Applications Lax-Terrell. פרק 4\n"
            "3.\n"
            "שיטות אינטגרציה - החלפת משתנים\n"
            "Multivariable Calculus with Applications Lax-Terrell. פרקים 4,6\n"
            "ביבליוגרפיה\n"
            "Calculus With Applications. Lax-Terrell. 2nd edition. פרק 6\n"
            "Multivariable Calculus with Applications Lax-Terrell. פרק 1\n"
        )
        self.assertEqual(
            app.parse_syllabus(text),
            ["האינטגרל המסוים וסכומי רימן",
             "פונקציות קדומות והמשפט היסודי של חדו\"א",
             "שיטות אינטגרציה - החלפת משתנים"])

    def test_parse_syllabus_fallback_drops_admin_lines(self):
        # No "נושאי הקורס" heading: the whole text is the list,
        # but admin lines are still dropped.
        text = (
            "prof@bgu.ac.il\n12:00-14:00\n7\n0-212.1.2371\n"
            "פרופ' י. אופנהיים\nמר א. טסלר\nגב' ע. מולקנדוב\n"
            "ד\"ר ג. לנדסמן\n[32] גוטמן\nחדר 307\nקבוצה 1\n"
            "יום שני\nLimits\nDerivatives\n"
        )
        self.assertEqual(
            app.parse_syllabus(text), ["Limits", "Derivatives"])

    def test_parse_syllabus_drops_source_lines_keeps_numbered_topics(self):
        text = (
            "גיאומטריה אנליטית ב- R^n\n"
            "Multivariable Calculus with Applications Lax-Terrell. פרק 1\n"
            "פרק 6\n"
            "פרקים 4,6\n"
            "דיפרנציאביליות. המישור המשיק. נגזרות חלקיות\n"
        )
        self.assertEqual(
            app.parse_syllabus(text),
            ["גיאומטריה אנליטית ב- R^n",
             "דיפרנציאביליות. המישור המשיק. נגזרות חלקיות"])

    def test_preview_shows_topic_count(self):
        status, _, data = self._request("POST", "/parse-syllabus", {
            "course_id": str(self._course_id()),
            "syllabus": "Limits\nDerivatives\n"})
        self.assertEqual(status, 200)
        self.assertIn("נמצאו 2 נושאים", data)

    def test_clear_topics_removes_course_topics_and_keeps_app_working(self):
        course_id = self._course_id()
        topic_ids = self._add_topics(course_id, "Limits", "Derivatives")
        self._add_week_slots()
        status, _, _ = self._request("POST", "/add-exam", {
            "course_id": str(course_id), "kind": "quiz",
            "title": "Quiz",
            "exam_date": (date.today() + timedelta(days=14)).isoformat(),
            "learn_pct": "40", "review_pct": "20",
            "exercise_pct": "20", "simulation_pct": "20",
            "topic_id": [str(t) for t in topic_ids]})
        self.assertEqual(status, 303)
        with app.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0], 2)
            self.assertGreater(
                conn.execute(
                    "SELECT COUNT(*) FROM plan_sessions").fetchone()[0], 0)
        # The everyday list no longer has a direct delete-all link:
        # it links to the edit page instead.
        _, _, data = self._request("GET", "/topics")
        self.assertIn("עריכת נושאים", data)
        self.assertNotIn('href="/clear-topics', data)
        # GET /clear-topics is only the confirmation page now:
        # it states the count and deletes nothing.
        status, _, data = self._request(
            "GET", f"/clear-topics?id={course_id}")
        self.assertEqual(status, 200)
        self.assertIn("2 נושאים", data)
        self.assertIn("אי אפשר לבטל", data)
        with app.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0], 2)
        # Only the POST from the confirm button deletes.
        status, location, _ = self._request(
            "POST", "/confirm-clear-topics", {"course_id": str(course_id)})
        self.assertEqual(status, 303)
        self.assertIn("topics", location)
        with app.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0], 0)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM exam_topics").fetchone()[0], 0)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM plan_sessions").fetchone()[0], 0)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM exams").fetchone()[0], 1)
        status, _, data = self._request("GET", "/courses")
        self.assertEqual(status, 200)
        self.assertIn("Calculus 2", data)

    def test_clear_topics_empty_course_is_noop(self):
        status, _, _ = self._request("GET", "/clear-topics?id=999")
        self.assertEqual(status, 303)
        status, _, _ = self._request("GET", "/")
        self.assertEqual(status, 200)

    # --- Week starts on Sunday (Israel) -----------------------------

    def test_weekday_dropdown_is_sunday_first(self):
        _, _, data = self._request("GET", "/slots")
        select = data.split(
            '<select id="slot_weekday"')[1].split("</select>")[0]
        values = re.findall(r'value="(\d+)"', select)
        self.assertEqual(values, ["6", "0", "1", "2", "3", "4", "5"])

    def test_wizard_weekday_dropdown_is_sunday_first(self):
        _, _, data = self._request("GET", "/setup?step=3")
        select = data.split(
            '<select id="w_slot_weekday"')[1].split("</select>")[0]
        values = re.findall(r'value="(\d+)"', select)
        self.assertEqual(values, ["6", "0", "1", "2", "3", "4", "5"])

    def test_slot_list_is_sunday_first(self):
        with app.connect() as conn:
            conn.execute(
                "INSERT INTO free_slots (weekday, start_time, end_time)"
                " VALUES (0, '09:00', '10:00'), (6, '09:00', '10:00'),"
                " (2, '09:00', '10:00')")
        _, _, data = self._request("GET", "/slots")
        slots = data.split('id="slots"')[1].split("</section>")[0]
        self.assertLess(
            slots.index("ראשון 09:00"), slots.index("שני 09:00"))
        self.assertLess(
            slots.index("שני 09:00"), slots.index("רביעי 09:00"))

    def test_monday_slot_still_plans_on_monday(self):
        # Stored value 0 still means Monday: a Monday-only slot
        # must plan sessions on Mondays, and the slot list must
        # show it as "שני".
        with app.connect() as conn:
            conn.execute(
                "INSERT INTO free_slots (weekday, start_time, end_time)"
                " VALUES (0, '09:00', '12:00')")
        self._add_topics(self._course_id(), "Limits")
        status, _, _ = self._request("POST", "/add-exam", {
            "course_id": str(self._course_id()), "kind": "exam",
            "title": "Exam",
            "exam_date": (date.today() + timedelta(days=14)).isoformat(),
            "learn_pct": "100", "review_pct": "0",
            "exercise_pct": "0", "simulation_pct": "0"})
        self.assertEqual(status, 303)
        with app.connect() as conn:
            sessions = conn.execute(
                "SELECT session_date FROM plan_sessions").fetchall()
        self.assertGreater(len(sessions), 0)
        for row in sessions:
            self.assertEqual(
                date.fromisoformat(row["session_date"]).weekday(), 0)
        _, _, data = self._request("GET", "/slots")
        self.assertIn("שני 09:00", data)

    # --- Exam topic checkboxes grouped per course -------------------

    def test_exam_form_checkboxes_grouped_by_course_in_details(self):
        alg_id = self._course_id("Algorithms")
        calc_id = self._course_id("Calculus 2")
        self._add_topics(alg_id, "Sorting", "Graphs")
        self._add_topics(calc_id, "Limits")
        # Step 2 of the flow, reached with a draft in the URL —
        # the grouped checkboxes live there now, not on "/".
        exam_date = (date.today() + timedelta(days=14)).isoformat()
        _, _, data = self._request(
            "GET", "/add-exam?step=2&course_id=" + str(alg_id) +
            "&kind=exam&title=Quiz&exam_date=" + exam_date)
        alg = data.split("Algorithms — 2 נושאים")[1].split(
            "</details>")[0]
        self.assertIn("Sorting", alg)
        self.assertIn("Graphs", alg)
        self.assertNotIn("Limits", alg)
        calc = data.split("Calculus 2 — 1 נושא")[1].split(
            "</details>")[0]
        self.assertIn("Limits", calc)
        self.assertNotIn("Sorting", calc)
        self.assertNotIn("Graphs", calc)
        # Collapsed by default: no "open" on the course details.
        self.assertNotIn('<details class="course-topics" open', data)

    def test_wizard_step5_checkboxes_grouped_by_course(self):
        alg_id = self._course_id("Algorithms")
        calc_id = self._course_id("Calculus 2")
        self._add_topics(alg_id, "Sorting")
        self._add_topics(calc_id, "Limits")
        _, _, data = self._request("GET", "/setup?step=5")
        self.assertIn("<summary>Algorithms — 1 נושא</summary>", data)
        self.assertIn("<summary>Calculus 2 — 1 נושא</summary>", data)
        alg = data.split("Algorithms — 1 נושא")[1].split(
            "</details>")[0]
        self.assertIn("Sorting", alg)
        self.assertNotIn("Limits", alg)

    def test_exam_page_scope_grouped_and_open(self):
        course_id = self._course_id()
        topic_ids = self._add_topics(course_id, "Limits", "Derivatives")
        self._add_week_slots()
        status, _, _ = self._request("POST", "/add-exam", {
            "course_id": str(course_id), "kind": "quiz",
            "title": "Quiz",
            "exam_date": (date.today() + timedelta(days=14)).isoformat(),
            "learn_pct": "40", "review_pct": "20",
            "exercise_pct": "20", "simulation_pct": "20",
            "topic_id": [str(t) for t in topic_ids]})
        self.assertEqual(status, 303)
        with app.connect() as conn:
            exam_id = conn.execute(
                "SELECT id FROM exams").fetchone()["id"]
        _, _, data = self._request("GET", f"/exam?id={exam_id}")
        # The dedicated page already shows only its own course's
        # topics, so its grouping starts open, not collapsed.
        self.assertIn('<details class="course-topics" open>', data)
        self.assertIn("Calculus 2 — 2 נושאים", data)
        self.assertIn("Limits", data)
        self.assertIn("Derivatives", data)

    # --- Fix 5: same-day exam warns, and the page says why ---------

    def test_same_day_exam_has_no_learning_day(self):
        plan = app.generate_plan(
            _topics(3), _all_week_slots(), date.today(),
            learn_pct=100, review_pct=0, exercise_pct=0, simulation_pct=0)
        self.assertEqual(plan["sessions"], [])
        self.assertEqual(len(plan["unscheduled"]), 3)

    def test_same_day_exam_warns_live(self):
        self._add_week_slots()
        self._add_topics(self._course_id(), "Limits", "Derivatives")
        status, location, _ = self._request("POST", "/add-exam", {
            "course_id": str(self._course_id()), "kind": "exam",
            "title": "Today!", "exam_date": date.today().isoformat(),
            "learn_pct": "40", "review_pct": "20",
            "exercise_pct": "20", "simulation_pct": "20"})
        self.assertEqual(status, 303)
        self.assertIn("warn=sameday", location)
        _, _, data = self._request("GET", location)
        self.assertIn("המבחן היום", data)
        with app.connect() as conn:
            exam_id = conn.execute(
                "SELECT id FROM exams").fetchone()["id"]
        _, _, data = self._request("GET", f"/exam?id={exam_id}")
        self.assertIn("המבחן קרוב מדי", data)

    # --- Fix 6: the today view says the plan may shift -------------

    def test_rescheduled_notice_text(self):
        self.assertIn(
            "התוכנית עודכנה",
            app.WARN_TEXTS["rescheduled"])
        with app.connect() as conn:
            html = app.page(
                app.get_courses(conn), app.get_assignments(conn),
                warn="rescheduled")
        self.assertIn("התוכנית עודכנה", html)
        self.assertIn("שעות המפגשים הבאים עלולות לזוז", html)

    def test_session_done_redirect_carries_rescheduled_notice(self):
        self._add_week_slots()
        calc_id = self._course_id()
        topic_ids = self._add_topics(calc_id, "Limits")
        with app.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO exams (course_id, kind, title, exam_date,"
                " learn_pct, review_pct, exercise_pct, simulation_pct)"
                " VALUES (?, 'exam', 'Final', ?, 100, 0, 0, 0)",
                (calc_id, (date.today() + timedelta(days=7)).isoformat()))
            exam_id = cursor.lastrowid
            conn.execute(
                "INSERT INTO exam_topics (exam_id, topic_id) VALUES (?, ?)",
                (exam_id, topic_ids[0]))
            app.regenerate_exam_plan(conn, exam_id)
            session_id = conn.execute(
                "SELECT id FROM plan_sessions WHERE exam_id = ?",
                (exam_id,)).fetchone()["id"]
        # The done link on the today view carries the notice back.
        from urllib.parse import quote
        next_url = quote("/?open=today&warn=rescheduled#today", safe="")
        status, location, _ = self._request(
            "GET", f"/session-done?id={session_id}&next={next_url}")
        self.assertEqual(status, 303)
        self.assertIn("warn=rescheduled", location)
        _, _, data = self._request("GET", location)
        self.assertIn("התוכנית עודכנה", data)

    # --- Fix 7: 0%-learn shows its real per-kind counts ------------

    def test_zero_learn_shows_all_four_counts(self):
        with app.connect() as conn:
            calc_id = self._course_id()
            topic_ids = self._add_topics(calc_id, "Limits")
            cursor = conn.execute(
                "INSERT INTO exams (course_id, kind, title, exam_date,"
                " learn_pct, review_pct, exercise_pct, simulation_pct)"
                " VALUES (?, 'exam', 'Final', ?, 0, 40, 30, 30)",
                (calc_id, (date.today() + timedelta(days=7)).isoformat()))
            exam_id = cursor.lastrowid
            exams = app.get_exams(conn)
            html = app.page(
                app.get_courses(conn), app.get_assignments(conn),
                topics=app.get_topics(conn), exams=exams,
                session_counts_by_exam={
                    exam_id: {"learn": 0, "review": 0, "exercise": 0,
                              "simulation": 1}})
        self.assertIn("למידה 0", html)
        self.assertIn("חזרה 0", html)
        self.assertIn("תרגול 0", html)
        self.assertIn("סימולציה 1", html)
        self.assertIn("עם 0% למידה לא יהיו גם מפגשי חזרה ותרגול", html)

    def test_zero_learn_exam_warns_on_save_live(self):
        self._add_week_slots()
        self._add_topics(self._course_id(), "Limits")
        status, location, _ = self._request("POST", "/add-exam", {
            "course_id": str(self._course_id()), "kind": "exam",
            "title": "Zero learn",
            "exam_date": (date.today() + timedelta(days=7)).isoformat(),
            "learn_pct": "0", "review_pct": "40",
            "exercise_pct": "30", "simulation_pct": "30"})
        self.assertEqual(status, 303)
        self.assertIn("warn=zerolearn", location)
        _, _, data = self._request("GET", location)
        self.assertIn("עם 0% למידה", data)

    # --- Fix 8: tiny assignment parts get a 15-minute minimum ------

    def test_tiny_parts_get_fifteen_minute_minimum_and_warning(self):
        # The QA repro: 1 hour / 10 parts used to make ten
        # 6-minute blocks. Now every part is 15 minutes, with a
        # warning that the parts exceed the estimate.
        result = app.schedule_assignment(
            {"id": 1, "due_date": (date.today() + timedelta(days=2)).isoformat(),
             "estimated_hours": 1, "num_parts": 10},
            _all_week_slots(), [],
        )
        self.assertEqual(len(result["blocks"]), 10)
        for block in result["blocks"]:
            self.assertEqual(
                app._to_minutes(block["end_time"])
                - app._to_minutes(block["start_time"]),
                15)
        self.assertIsNotNone(result["warning"])
        self.assertIn("15 דקות", result["warning"])
        self.assertEqual(result["scheduled_minutes"], 150)
        self.assertEqual(result["unscheduled_minutes"], 0)

    def test_assignment_part_warning_helper(self):
        self.assertIsNotNone(app.assignment_part_warning(
            {"estimated_hours": 1, "num_parts": 10}))
        self.assertIsNone(app.assignment_part_warning(
            {"estimated_hours": 4, "num_parts": 2}))
        self.assertIsNone(app.assignment_part_warning(
            {"estimated_hours": None, "num_parts": 10}))
        with app.connect() as conn:
            html = app.page(
                app.get_courses(conn), app.get_assignments(conn))
        # No assignments yet: no warning anywhere.
        self.assertNotIn("15 דקות מינימום", html)

    # --- Fix 9: garbage routine answers are rejected ---------------

    def test_validate_routine_rejects_garbage(self):
        values, error = app.validate_routine("abc", "999", "nope", "nope")
        self.assertIsNone(values)
        self.assertIn("אורך מפגש", error)
        self.assertIn("אורך הפסקה", error)
        values, error = app.validate_routine("50", "10", "morning", "focus")
        self.assertIsNone(error)
        self.assertEqual(values, (50, 10, "morning", "focus"))
        # Blank means "not answered": fine, no error.
        values, error = app.validate_routine("", "", "", "")
        self.assertIsNone(error)
        self.assertEqual(values, (None, None, None, None))
        # Out of range: 5-minute sessions, 70-minute breaks.
        _, error = app.validate_routine("5", "70", "any", None)
        self.assertIsNotNone(error)

    def test_garbage_routine_keeps_previous_values_live(self):
        # Save a good routine first, then send garbage: the old
        # code stored NULL silently. Now the save is rejected and
        # the previous values stay.
        status, _, _ = self._request("POST", "/save-prefs", {
            "session_minutes": "50", "break_minutes": "10",
            "homework_pref": "morning"})
        self.assertEqual(status, 303)
        status, _, data = self._request("POST", "/save-prefs", {
            "session_minutes": "abc", "break_minutes": "999",
            "homework_pref": "nope"})
        self.assertEqual(status, 200)
        self.assertIn("אורך מפגש", data)
        with app.connect() as conn:
            prefs = app.get_prefs(conn)
            self.assertEqual(prefs["session_minutes"], 50)
            self.assertEqual(prefs["break_minutes"], 10)
            self.assertEqual(prefs["homework_pref"], "morning")

    # --- Fix 10: every save says "נשמר ✓" ---------------------------

    def test_saved_notice_renders_only_when_saved(self):
        with app.connect() as conn:
            html = app.page(
                app.get_courses(conn), app.get_assignments(conn),
                saved=True)
        self.assertIn("נשמר ✓", html)
        with app.connect() as conn:
            html = app.page(
                app.get_courses(conn), app.get_assignments(conn))
        self.assertNotIn("נשמר ✓", html)

    # --- Fix 11: quiz turned exam keeps its narrowed scope ---------

    def test_narrowed_scope_note_on_exam_page(self):
        calc_id = self._course_id()
        topic_ids = self._add_topics(
            calc_id, "Limits", "Derivatives", "Integrals", "Series")
        with app.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO exams (course_id, kind, title, exam_date)"
                " VALUES (?, 'exam', 'Final', ?)",
                (calc_id, (date.today() + timedelta(days=7)).isoformat()))
            exam_id = cursor.lastrowid
            conn.execute(
                "INSERT INTO exam_topics (exam_id, topic_id) VALUES (?, ?)",
                (exam_id, topic_ids[0]))
            exam = conn.execute(
                "SELECT * FROM exams WHERE id = ?", (exam_id,)).fetchone()
            html = app.exam_page(conn, exam)
        self.assertIn("ההיקף נשאר מצומצם", html)
        self.assertIn("1 נושאים מתוך 4", html)
        # A whole-course exam shows no such note.
        with app.connect() as conn:
            conn.execute(
                "DELETE FROM exam_topics WHERE exam_id = ?", (exam_id,))
            for topic_id in topic_ids:
                conn.execute(
                    "INSERT INTO exam_topics (exam_id, topic_id)"
                    " VALUES (?, ?)", (exam_id, topic_id))
            exam = conn.execute(
                "SELECT * FROM exams WHERE id = ?", (exam_id,)).fetchone()
            html = app.exam_page(conn, exam)
        self.assertNotIn("ההיקף נשאר מצומצם", html)

    # --- Fix 12: duplicate and overlapping free slots --------------

    def test_duplicate_slot_rejected_and_overlap_warns_live(self):
        status, _, _ = self._request("POST", "/add-slot", {
            "weekday": "0", "start_time": "09:00", "end_time": "12:00"})
        self.assertEqual(status, 303)
        # The exact same slot again: rejected, count stays 1.
        status, _, data = self._request("POST", "/add-slot", {
            "weekday": "0", "start_time": "09:00", "end_time": "12:00"})
        self.assertEqual(status, 200)
        self.assertIn("חלון כזה כבר קיים", data)
        with app.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM free_slots")
                .fetchone()[0], 1)
        # An overlapping slot: saved, but the redirect warns.
        status, location, _ = self._request("POST", "/add-slot", {
            "weekday": "0", "start_time": "11:00", "end_time": "13:00"})
        self.assertEqual(status, 303)
        self.assertIn("warn=slot_overlap", location)
        _, _, data = self._request("GET", location)
        self.assertIn("חופף לחלון קיים", data)
        with app.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM free_slots")
                .fetchone()[0], 2)
        # A different day, same hours: fine, no warning.
        status, location, _ = self._request("POST", "/add-slot", {
            "weekday": "1", "start_time": "09:00", "end_time": "12:00"})
        self.assertEqual(status, 303)
        self.assertNotIn("warn=", location)


class UndoTest(unittest.TestCase):
    """Done is reversible: undo links for sessions, topics and
    assignments, and undo restores the work (blocks, learned
    state) instead of just flipping a flag."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        app.DB_PATH = os.path.join(self.tmp.name, "test.db")
        app.init_db()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _request(self, method, path, fields=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        body = urlencode(fields or {}, doseq=True) if fields else None
        headers = {"Content-Type": "application/x-www-form-urlencoded"} \
            if body else {}
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read().decode()
        location = resp.getheader("Location")
        status = resp.status
        conn.close()
        return status, location, data

    def _course_id(self, name="Calculus 2"):
        with app.connect() as conn:
            return conn.execute(
                "SELECT id FROM courses WHERE name = ?", (name,)
            ).fetchone()["id"]

    def _add_week_slots(self):
        with app.connect() as conn:
            conn.execute(
                "INSERT INTO free_slots (weekday, start_time, end_time)"
                " VALUES (?, ?, ?), (?, ?, ?), (?, ?, ?), (?, ?, ?),"
                " (?, ?, ?), (?, ?, ?), (?, ?, ?)",
                tuple(v for d in range(7) for v in (d, "09:00", "12:00")))

    def _add_topics(self, course_id, *titles):
        with app.connect() as conn:
            ids = []
            for title in titles:
                cursor = conn.execute(
                    "INSERT INTO topics (course_id, title) VALUES (?, ?)",
                    (course_id, title),
                )
                ids.append(cursor.lastrowid)
            return ids

    def _add_assignment(self, course_id, title="Homework", days=7,
                        hours=1, parts=1):
        with app.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO assignments"
                " (course_id, title, due_date, estimated_hours, num_parts)"
                " VALUES (?, ?, ?, ?, ?)",
                (course_id, title,
                 (date.today() + timedelta(days=days)).isoformat(),
                 hours, parts),
            )
            return cursor.lastrowid

    def _add_session(self, exam_id, topic_id, day, kind="learn",
                     start="10:00", end="10:50", done=0):
        with app.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO plan_sessions"
                " (exam_id, topic_id, session_date, start_time, end_time,"
                "  kind, done) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (exam_id, topic_id, day.isoformat(), start, end,
                 kind, done),
            )
            return cursor.lastrowid

    def _add_exam(self, course_id, title="Exam"):
        with app.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO exams (course_id, kind, title, exam_date)"
                " VALUES (?, 'exam', ?, ?)",
                (course_id, title,
                 (date.today() + timedelta(days=7)).isoformat()),
            )
            return cursor.lastrowid

    # --- Assignment undo restores its blocks -----------------------

    def test_assignment_undone_restores_blocks(self):
        calc_id = self._course_id()
        self._add_week_slots()
        assignment_id = self._add_assignment(calc_id)
        with app.connect() as conn:
            app.reschedule_assignment(conn, assignment_id)
            self.assertEqual(
                len(app.get_assignment_blocks(conn, assignment_id)), 1)
            app.mark_assignment_done(conn, assignment_id)
            row = conn.execute(
                "SELECT done FROM assignments WHERE id = ?",
                (assignment_id,)).fetchone()
            self.assertEqual(row["done"], 1)
            self.assertEqual(
                len(app.get_assignment_blocks(conn, assignment_id)), 0)
            result = app.mark_assignment_undone(conn, assignment_id)
            row = conn.execute(
                "SELECT done FROM assignments WHERE id = ?",
                (assignment_id,)).fetchone()
            self.assertEqual(row["done"], 0)
            self.assertEqual(result["scheduled_minutes"], 60)
            self.assertEqual(
                len(app.get_assignment_blocks(conn, assignment_id)), 1)

    def test_assignment_undone_after_due_date_reports_unscheduled(self):
        calc_id = self._course_id()
        self._add_week_slots()
        # Past due date: inserted directly, the way a real row
        # whose deadline has passed looks — the handler would
        # never create it, but undo must still handle it honestly.
        assignment_id = self._add_assignment(calc_id, days=-1)
        with app.connect() as conn:
            app.mark_assignment_done(conn, assignment_id)
            result = app.mark_assignment_undone(conn, assignment_id)
            self.assertEqual(result["blocks"], [])
            self.assertEqual(result["unscheduled_minutes"], 60)
            row = conn.execute(
                "SELECT done FROM assignments WHERE id = ?",
                (assignment_id,)).fetchone()
            self.assertEqual(row["done"], 0)
        # The assignments page says so, instead of silently
        # dropping it.
        _, _, data = self._request("GET", "/assignments")
        self.assertIn("לא הצלחתי לתזמן 60 דקות", data)

    # --- Topic undo -------------------------------------------------

    def test_topic_undone_via_route(self):
        calc_id = self._course_id()
        topic_id = self._add_topics(calc_id, "Limits")[0]
        status, location, _ = self._request(
            "GET", f"/topic-done?id={topic_id}")
        self.assertEqual(status, 303)
        with app.connect() as conn:
            learned = conn.execute(
                "SELECT learned FROM topics WHERE id = ?",
                (topic_id,)).fetchone()["learned"]
        self.assertEqual(learned, 1)
        status, location, _ = self._request(
            "GET", f"/topic-undone?id={topic_id}")
        self.assertEqual(status, 303)
        self.assertIn("#topics", location)
        with app.connect() as conn:
            learned = conn.execute(
                "SELECT learned FROM topics WHERE id = ?",
                (topic_id,)).fetchone()["learned"]
        self.assertEqual(learned, 0)

    # --- Session undo ----------------------------------------------

    def test_session_undone_unlearns_topic(self):
        calc_id = self._course_id()
        topic_id = self._add_topics(calc_id, "Limits")[0]
        exam_id = self._add_exam(calc_id)
        session_id = self._add_session(
            exam_id, topic_id, date.today())
        with app.connect() as conn:
            # What /session-done does: tick the session, learn topic.
            conn.execute(
                "UPDATE plan_sessions SET done = 1 WHERE id = ?",
                (session_id,))
            conn.execute(
                "UPDATE topics SET learned = 1 WHERE id = ?", (topic_id,))
            app.mark_session_undone(conn, session_id)
            done = conn.execute(
                "SELECT done FROM plan_sessions WHERE id = ?",
                (session_id,)).fetchone()["done"]
            learned = conn.execute(
                "SELECT learned FROM topics WHERE id = ?", (topic_id,)
            ).fetchone()["learned"]
        self.assertEqual(done, 0)
        self.assertEqual(learned, 0)

    def test_session_undone_keeps_topic_learned_when_covered(self):
        calc_id = self._course_id()
        topic_id = self._add_topics(calc_id, "Limits")[0]
        exam_id = self._add_exam(calc_id)
        first = self._add_session(
            exam_id, topic_id, date.today(), start="09:00", end="09:50")
        second = self._add_session(
            exam_id, topic_id, date.today(), start="10:00", end="10:50")
        with app.connect() as conn:
            conn.execute(
                "UPDATE plan_sessions SET done = 1 WHERE id IN (?, ?)",
                (first, second))
            conn.execute(
                "UPDATE topics SET learned = 1 WHERE id = ?", (topic_id,))
            # Undoing one of two done learn sessions: the topic
            # is still covered by the other one.
            app.mark_session_undone(conn, first)
            learned = conn.execute(
                "SELECT learned FROM topics WHERE id = ?", (topic_id,)
            ).fetchone()["learned"]
            self.assertEqual(learned, 1)
            # Undoing the last one un-learns it.
            app.mark_session_undone(conn, second)
            learned = conn.execute(
                "SELECT learned FROM topics WHERE id = ?", (topic_id,)
            ).fetchone()["learned"]
            self.assertEqual(learned, 0)

    def test_review_session_undone_keeps_topic_learned(self):
        calc_id = self._course_id()
        topic_id = self._add_topics(calc_id, "Limits")[0]
        exam_id = self._add_exam(calc_id)
        session_id = self._add_session(
            exam_id, topic_id, date.today(), kind="review",
            start="11:00", end="11:30")
        with app.connect() as conn:
            conn.execute(
                "UPDATE plan_sessions SET done = 1 WHERE id = ?",
                (session_id,))
            conn.execute(
                "UPDATE topics SET learned = 1 WHERE id = ?", (topic_id,))
            app.mark_session_undone(conn, session_id)
            done = conn.execute(
                "SELECT done FROM plan_sessions WHERE id = ?",
                (session_id,)).fetchone()["done"]
            learned = conn.execute(
                "SELECT learned FROM topics WHERE id = ?", (topic_id,)
            ).fetchone()["learned"]
        self.assertEqual(done, 0)
        # A review tick never taught the topic, so undoing it
        # must not un-teach it either.
        self.assertEqual(learned, 1)

    # --- Undo links render, and routes redirect back ---------------

    def test_undo_links_on_today_view_live(self):
        calc_id = self._course_id()
        today = date.today()
        with app.connect() as conn:
            conn.execute(
                "INSERT INTO free_slots (weekday, start_time, end_time)"
                " VALUES (?, '09:00', '12:00')", (today.weekday(),))
        topic_id = self._add_topics(calc_id, "Limits")[0]
        exam_id = self._add_exam(calc_id)
        session_id = self._add_session(exam_id, topic_id, today)
        assignment_id = self._add_assignment(calc_id, days=0)
        with app.connect() as conn:
            app.reschedule_assignment(conn, assignment_id)
        # Tick everything done, the way Daniel would by mistake.
        self._request("GET", f"/session-done?id={session_id}")
        self._request("GET", f"/done?id={assignment_id}")
        self._request("GET", f"/topic-done?id={topic_id}")
        # The session undo lives on the dashboard (today hero);
        # the assignment and topic undo links live on their own
        # pages now.
        _, _, data = self._request("GET", "/")
        self.assertIn("בטל סימון", data)
        self.assertIn(f"/session-undone?id={session_id}", data)
        _, _, data = self._request("GET", "/assignments")
        self.assertIn(f"/undone?id={assignment_id}", data)
        _, _, data = self._request("GET", "/topics")
        self.assertIn(f"/topic-undone?id={topic_id}", data)
        # Undo all three: the tick links come back.
        self._request("GET", f"/session-undone?id={session_id}")
        self._request("GET", f"/undone?id={assignment_id}")
        self._request("GET", f"/topic-undone?id={topic_id}")
        _, _, data = self._request("GET", "/")
        self.assertIn("סמן כבוצע", data)
        _, _, data = self._request("GET", "/topics")
        self.assertIn("סמן כנלמד", data)
        with app.connect() as conn:
            done = conn.execute(
                "SELECT done FROM plan_sessions WHERE id = ?",
                (session_id,)).fetchone()["done"]
            learned = conn.execute(
                "SELECT learned FROM topics WHERE id = ?", (topic_id,)
            ).fetchone()["learned"]
            a_done = conn.execute(
                "SELECT done FROM assignments WHERE id = ?",
                (assignment_id,)).fetchone()["done"]
            blocks = app.get_assignment_blocks(conn, assignment_id)
        self.assertEqual(done, 0)
        self.assertEqual(learned, 0)
        self.assertEqual(a_done, 0)
        self.assertEqual(len(blocks), 1)

    def test_undo_routes_redirect_back(self):
        calc_id = self._course_id()
        topic_id = self._add_topics(calc_id, "Limits")[0]
        exam_id = self._add_exam(calc_id)
        session_id = self._add_session(exam_id, topic_id, date.today())
        assignment_id = self._add_assignment(calc_id)
        status, location, _ = self._request(
            "GET", f"/undone?id={assignment_id}")
        self.assertEqual(status, 303)
        self.assertIn("#assignments", location)
        status, location, _ = self._request(
            "GET", f"/topic-undone?id={topic_id}")
        self.assertEqual(status, 303)
        self.assertIn("#topics", location)
        status, location, _ = self._request(
            "GET", f"/session-undone?id={session_id}")
        self.assertEqual(status, 303)
        self.assertIn("#today", location)
        # A "next" URL is honoured, like the done routes do.
        status, location, _ = self._request(
            "GET", f"/session-undone?id={session_id}"
                   f"&next=%2Fexam%3Fid%3D{exam_id}")
        self.assertEqual(status, 303)
        self.assertEqual(location, f"/exam?id={exam_id}")

    def test_exam_page_shows_undo_for_done_session(self):
        calc_id = self._course_id()
        topic_id = self._add_topics(calc_id, "Limits")[0]
        exam_id = self._add_exam(calc_id)
        session_id = self._add_session(exam_id, topic_id, date.today())
        with app.connect() as conn:
            exam = conn.execute(
                "SELECT * FROM exams WHERE id = ?", (exam_id,)).fetchone()
            html = app.exam_page(conn, exam)
        self.assertIn("סמן כבוצע", html)
        with app.connect() as conn:
            conn.execute(
                "UPDATE plan_sessions SET done = 1 WHERE id = ?",
                (session_id,))
            exam = conn.execute(
                "SELECT * FROM exams WHERE id = ?", (exam_id,)).fetchone()
            html = app.exam_page(conn, exam)
        self.assertIn("בטל סימון", html)
        self.assertIn(f"/session-undone?id={session_id}", html)


class QARound3Test(unittest.TestCase):
    """QA round 3: one corrupt row must not kill a page, a failed
    save must never say "נשמר ✓", deletes rebuild the plans,
    past records can still be edited, assignments need a date
    and hours, kinds are validated, reviews are capped per topic,
    and an orphan exam shows up as "קורס שנמחק"."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        app.DB_PATH = os.path.join(self.tmp.name, "test.db")
        app.init_db()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _request(self, method, path, fields=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        body = urlencode(fields or {}, doseq=True) if fields else None
        headers = {"Content-Type": "application/x-www-form-urlencoded"} \
            if body else {}
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read().decode()
        location = resp.getheader("Location")
        status = resp.status
        conn.close()
        return status, location, data

    def _course_id(self, name="Calculus 2"):
        with app.connect() as conn:
            return conn.execute(
                "SELECT id FROM courses WHERE name = ?", (name,)
            ).fetchone()["id"]

    def _add_week_slots(self, start="09:00", end="12:00"):
        with app.connect() as conn:
            conn.execute(
                "INSERT INTO free_slots (weekday, start_time, end_time)"
                " VALUES (?, ?, ?), (?, ?, ?), (?, ?, ?), (?, ?, ?),"
                " (?, ?, ?), (?, ?, ?), (?, ?, ?)",
                tuple(v for d in range(7) for v in (d, start, end)))

    def _add_topics(self, course_id, *titles):
        with app.connect() as conn:
            ids = []
            for title in titles:
                cursor = conn.execute(
                    "INSERT INTO topics (course_id, title) VALUES (?, ?)",
                    (course_id, title),
                )
                ids.append(cursor.lastrowid)
            return ids

    def _add_exam(self, course_id, title="Final", kind="exam",
                  days=7, learn_pct=100):
        status, _, _ = self._request("POST", "/add-exam", {
            "course_id": str(course_id), "kind": kind, "title": title,
            "exam_date": (date.today() + timedelta(days=days)).isoformat(),
            "learn_pct": str(learn_pct), "review_pct": "0",
            "exercise_pct": "0", "simulation_pct": "0"})
        self.assertEqual(status, 303)
        with app.connect() as conn:
            return conn.execute(
                "SELECT id FROM exams WHERE title = ?",
                (title,)).fetchone()["id"]

    # --- Fix 1: a corrupt time row must not crash a page -----------

    def test_corrupt_session_row_does_not_crash_home_page(self):
        calc_id = self._course_id()
        topic_id = self._add_topics(calc_id, "Limits")[0]
        exam_id = self._add_exam(calc_id)
        with app.connect() as conn:
            conn.execute(
                "INSERT INTO plan_sessions"
                " (exam_id, topic_id, session_date, start_time,"
                "  end_time, kind, done)"
                " VALUES (?, ?, ?, 'xx', 'yy', 'learn', 0)",
                (exam_id, topic_id, date.today().isoformat()))
        # The QA repro: GET / used to drop the connection.
        status, _, data = self._request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("מה עושים היום", data)
        # The bad row itself is skipped by the markers, not fatal.
        marked = app.annotate_today_items(
            [{"start_time": "xx", "end_time": "yy", "done": 0}], 600)
        self.assertEqual(marked[0]["marker"], "")

    def test_corrupt_busy_block_does_not_crash_plan_generation(self):
        calc_id = self._course_id()
        self._add_topics(calc_id, "Limits")
        self._add_week_slots()
        exam_id = self._add_exam(calc_id)
        with app.connect() as conn:
            conn.execute(
                "INSERT INTO busy_blocks"
                " (block_date, start_time, end_time, reason)"
                " VALUES (?, 'zz', 'ww', 'broken')",
                (date.today().isoformat(),))
        # The QA repro: /generate-plan used to drop the connection.
        status, location, _ = self._request(
            "GET", f"/generate-plan?exam_id={exam_id}")
        self.assertEqual(status, 303)
        # And the occupied-time grouping simply skips the bad row.
        self.assertEqual(
            app._occupied_by_date([{
                "session_date": date.today().isoformat(),
                "start_time": "zz", "end_time": "ww"}]), {})
        self.assertEqual(
            app._occupied_by_date([{
                "session_date": date.today().isoformat(),
                "start_time": "09:00", "end_time": "10:00"}]),
            {date.today(): [(540, 600)]})

    # --- Fix 2: no fake "נשמר ✓" ------------------------------------

    def test_empty_and_duplicate_course_are_rejected(self):
        for name in ("", "   "):
            status, _, data = self._request(
                "POST", "/add-course", {"name": name, "teacher": ""})
            self.assertEqual(status, 200)
            self.assertIn("שם הקורס חסר", data)
        with app.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM courses").fetchone()[0]
        self.assertEqual(count, 3)  # only the seeded courses
        status, _, data = self._request(
            "POST", "/add-course", {"name": "Calculus 2", "teacher": ""})
        self.assertEqual(status, 200)
        self.assertIn("כבר קיים", data)
        with app.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM courses").fetchone()[0]
        self.assertEqual(count, 3)
        # A genuinely new course still saves.
        status, location, _ = self._request(
            "POST", "/add-course", {"name": "Physics", "teacher": ""})
        self.assertEqual(status, 303)
        self.assertIn("saved=1", location)

    def test_empty_titles_are_rejected_not_saved(self):
        calc_id = self._course_id()
        cases = (
            ("/add-exam", {
                "course_id": str(calc_id), "kind": "exam", "title": "",
                "exam_date": (date.today() + timedelta(days=7)).isoformat(),
                "learn_pct": "40", "review_pct": "20",
                "exercise_pct": "20", "simulation_pct": "20"},
             "שם המבחן חסר", "exams"),
            ("/add-assignment", {
                "course_id": str(calc_id), "title": "",
                "due_date": (date.today() + timedelta(days=7)).isoformat(),
                "estimated_hours": "2", "num_parts": "1"},
             "שם המטלה חסר", "assignments"),
            ("/add-topic", {
                "course_id": str(calc_id), "title": ""},
             "שם הנושא חסר", "topics"),
        )
        for path, fields, message, table in cases:
            status, _, data = self._request("POST", path, fields)
            self.assertEqual(status, 200, path)
            self.assertIn(message, data, path)
            with app.connect() as conn:
                self.assertEqual(
                    conn.execute(
                        f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                    0, path)
        status, _, data = self._request(
            "POST", "/save-topics",
            {"course_id": str(calc_id), "topics": ""})
        self.assertEqual(status, 200)
        self.assertIn("לא נמצאו נושאים", data)
        with app.connect() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM topics").fetchone()[0], 0)

    def test_busy_block_with_bad_times_is_rejected_not_saved(self):
        for fields, message in (
            ({"block_date": date.today().isoformat(),
              "start_time": "25:00", "end_time": "26:00",
              "reason": "friends"}, "השעות חייבות"),
            ({"block_date": date.today().isoformat(),
              "start_time": "", "end_time": "", "reason": ""},
             "השעות חייבות"),
            ({"block_date": "", "start_time": "18:00",
              "end_time": "21:00", "reason": "friends"}, "תאריך חסר"),
        ):
            status, _, data = self._request("POST", "/add-busy", fields)
            self.assertEqual(status, 200, fields)
            self.assertIn(message, data, fields)
            with app.connect() as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM busy_blocks").fetchone()[0],
                    0)

    # --- Fix 3: deleting a slot rebuilds the plans -------------------

    def test_delete_slot_moves_sessions_out_of_the_window(self):
        calc_id = self._course_id()
        self._add_topics(
            calc_id, "T1", "T2", "T3", "T4", "T5", "T6")
        self._add_week_slots()
        exam_id = self._add_exam(calc_id, days=7)
        tomorrow = date.today() + timedelta(days=1)
        with app.connect() as conn:
            before = conn.execute(
                "SELECT COUNT(*) FROM plan_sessions WHERE exam_id = ?"
                " AND session_date = ?",
                (exam_id, tomorrow.isoformat())).fetchone()[0]
            slot_id = conn.execute(
                "SELECT id FROM free_slots WHERE weekday = ?",
                (tomorrow.weekday(),)).fetchone()["id"]
        self.assertGreater(before, 0)
        status, location, _ = self._request(
            "GET", f"/delete-slot?id={slot_id}")
        self.assertEqual(status, 303)
        with app.connect() as conn:
            after = conn.execute(
                "SELECT COUNT(*) FROM plan_sessions WHERE exam_id = ?"
                " AND session_date = ?",
                (exam_id, tomorrow.isoformat())).fetchone()[0]
            learn_total = conn.execute(
                "SELECT COUNT(*) FROM plan_sessions WHERE exam_id = ?"
                " AND kind = 'learn'",
                (exam_id,)).fetchone()[0]
        self.assertEqual(after, 0)
        # All 6 learning sessions moved to other days, not lost
        # (breaks between them are separate rows, not counted).
        self.assertEqual(learn_total, 6)

    # --- Fix 4: deleting a course rebuilds other exams' plans --------

    def test_delete_course_frees_time_for_other_exams(self):
        calc_id = self._course_id()
        other_id = self._course_id("Algorithms")
        self._add_topics(calc_id, "A1", "A2", "A3", "A4")
        self._add_topics(
            other_id, "B1", "B2", "B3", "B4", "B5", "B6",
            "B7", "B8", "B9", "B10", "B11", "B12")
        self._add_week_slots()
        exam_a = self._add_exam(calc_id, title="Exam A", days=3)
        exam_b = self._add_exam(other_id, title="Exam B", days=3)
        with app.connect() as conn:
            before = conn.execute(
                "SELECT COUNT(*) FROM plan_sessions WHERE exam_id = ?",
                (exam_b,)).fetchone()[0]
        status, location, _ = self._request(
            "GET", f"/delete-course?id={calc_id}")
        self.assertEqual(status, 303)
        with app.connect() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM exams WHERE id = ?",
                    (exam_a,)).fetchone()[0], 0)
            after = conn.execute(
                "SELECT COUNT(*) FROM plan_sessions WHERE exam_id = ?",
                (exam_b,)).fetchone()[0]
        self.assertGreater(after, before)

    # --- Fix 5: a past record can still be edited ---------------------

    def test_editing_a_past_exam_keeps_its_date(self):
        calc_id = self._course_id()
        self._add_topics(calc_id, "Limits")
        self._add_week_slots()
        exam_id = self._add_exam(calc_id)
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        with app.connect() as conn:
            conn.execute(
                "UPDATE exams SET exam_date = ? WHERE id = ?",
                (yesterday, exam_id))
        # Rename, keeping the past date: allowed now.
        status, location, _ = self._request("POST", "/save-exam", {
            "exam_id": str(exam_id), "title": "Final renamed",
            "exam_date": yesterday, "kind": "exam",
            "learn_pct": "100", "review_pct": "0",
            "exercise_pct": "0", "simulation_pct": "0"})
        self.assertEqual(status, 303)
        with app.connect() as conn:
            row = conn.execute(
                "SELECT * FROM exams WHERE id = ?", (exam_id,)).fetchone()
        self.assertEqual(row["title"], "Final renamed")
        self.assertEqual(row["exam_date"], yesterday)
        # Moving it to a DIFFERENT past date is still rejected.
        older = (date.today() - timedelta(days=2)).isoformat()
        status, _, data = self._request("POST", "/save-exam", {
            "exam_id": str(exam_id), "title": "Broken",
            "exam_date": older, "kind": "exam",
            "learn_pct": "100", "review_pct": "0",
            "exercise_pct": "0", "simulation_pct": "0"})
        self.assertEqual(status, 200)
        self.assertIn("התאריך לא הגיוני", data)

    def test_editing_a_past_assignment_keeps_its_date(self):
        calc_id = self._course_id()
        self._add_week_slots()
        status, _, _ = self._request("POST", "/add-assignment", {
            "course_id": str(calc_id), "title": "HW1",
            "due_date": (date.today() + timedelta(days=7)).isoformat(),
            "estimated_hours": "2", "num_parts": "1"})
        self.assertEqual(status, 303)
        with app.connect() as conn:
            assignment_id = conn.execute(
                "SELECT id FROM assignments").fetchone()["id"]
            conn.execute(
                "UPDATE assignments SET due_date = ? WHERE id = ?",
                ((date.today() - timedelta(days=1)).isoformat(),
                 assignment_id))
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        status, location, _ = self._request("POST", "/save-assignment", {
            "assignment_id": str(assignment_id), "title": "HW1 renamed",
            "due_date": yesterday, "estimated_hours": "2",
            "num_parts": "1"})
        self.assertEqual(status, 303)
        with app.connect() as conn:
            row = conn.execute(
                "SELECT * FROM assignments WHERE id = ?",
                (assignment_id,)).fetchone()
        self.assertEqual(row["title"], "HW1 renamed")
        self.assertEqual(row["due_date"], yesterday)
        # Garbage hours on edit are rejected, not silently wiped.
        status, _, data = self._request("POST", "/save-assignment", {
            "assignment_id": str(assignment_id), "title": "Broken",
            "due_date": yesterday, "estimated_hours": "abc",
            "num_parts": "1"})
        self.assertEqual(status, 200)
        self.assertIn("הערכת שעות לא תקינה", data)

    # --- Fix 6: an assignment needs a date and hours ------------------

    def test_assignment_without_date_or_hours_is_rejected(self):
        calc_id = self._course_id()
        due = (date.today() + timedelta(days=7)).isoformat()
        for fields, message in (
            ({"course_id": str(calc_id), "title": "No date",
              "due_date": "", "estimated_hours": "2", "num_parts": "1"},
             "תאריך הגשה חסר"),
            ({"course_id": str(calc_id), "title": "Bad hours",
              "due_date": due, "estimated_hours": "abc", "num_parts": "1"},
             "הערכת שעות לא תקינה"),
            ({"course_id": str(calc_id), "title": "No hours",
              "due_date": due, "estimated_hours": "", "num_parts": "1"},
             "הערכת שעות לא תקינה"),
            ({"course_id": str(calc_id), "title": "Zero hours",
              "due_date": due, "estimated_hours": "0", "num_parts": "1"},
             "הערכת שעות לא תקינה"),
        ):
            status, _, data = self._request(
                "POST", "/add-assignment", fields)
            self.assertEqual(status, 200, fields)
            self.assertIn(message, data, fields)
            with app.connect() as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM assignments").fetchone()[0],
                    0, fields)

    def test_home_page_says_why_an_old_assignment_has_no_blocks(self):
        calc_id = self._course_id()
        with app.connect() as conn:
            conn.execute(
                "INSERT INTO assignments (course_id, title)"
                " VALUES (?, 'No date HW')", (calc_id,))
            conn.execute(
                "INSERT INTO assignments"
                " (course_id, title, due_date)"
                " VALUES (?, 'No hours HW', ?)",
                (calc_id,
                 (date.today() + timedelta(days=7)).isoformat()))
        _, _, data = self._request("GET", "/assignments")
        self.assertIn("לא שובצה — אין תאריך הגשה", data)
        self.assertIn("לא שובצה — אין הערכת שעות", data)

    # --- Fix 7: kind=banana is rejected -------------------------------

    def test_banana_kind_is_rejected_on_creation(self):
        calc_id = self._course_id()
        status, _, data = self._request("POST", "/add-exam", {
            "course_id": str(calc_id), "kind": "banana",
            "title": "Weird",
            "exam_date": (date.today() + timedelta(days=7)).isoformat(),
            "learn_pct": "40", "review_pct": "20",
            "exercise_pct": "20", "simulation_pct": "20"})
        self.assertEqual(status, 200)
        self.assertIn("סוג המבחן לא תקין", data)
        with app.connect() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM exams").fetchone()[0], 0)

    # --- Fix 8: reviews are capped per topic --------------------------

    def test_one_topic_gets_at_most_three_reviews(self):
        # The QA repro: 1 topic, 13 days x 4 hours, default split —
        # 22 identical reviews. Now at most 3, on the one topic.
        plan = app.generate_plan(
            _topics(1),
            [{"weekday": d, "start_time": "09:00", "end_time": "13:00"}
             for d in range(7)],
            date.today() + timedelta(days=13),
        )
        review = [s for s in plan["sessions"] if s["kind"] == "review"]
        self.assertEqual(len(review), 3)
        self.assertTrue(all(s["topic_id"] == 1 for s in review))
        self.assertEqual(app.MAX_REVIEW_PER_TOPIC, 3)

    # --- Fix 9: an orphan exam shows up --------------------------------

    def test_orphan_exam_shows_as_deleted_course(self):
        with app.connect() as conn:
            # The app enforces foreign keys, so an orphan can only
            # appear the way QA made one: a direct database edit
            # with enforcement off (the sqlite3 CLI default).
            conn.execute("PRAGMA foreign_keys = OFF")
            cursor = conn.execute(
                "INSERT INTO exams (course_id, kind, title, exam_date)"
                " VALUES (999, 'exam', 'Ghost exam', ?)",
                ((date.today() + timedelta(days=7)).isoformat(),))
            exam_id = cursor.lastrowid
            conn.execute("PRAGMA foreign_keys = ON")
            exams = app.get_exams(conn)
        self.assertEqual(len(exams), 1)
        self.assertEqual(exams[0]["course_name"], "קורס שנמחק")
        _, _, data = self._request("GET", "/")
        self.assertIn("Ghost exam", data)
        self.assertIn("קורס שנמחק", data)
        status, _, data = self._request("GET", f"/exam?id={exam_id}")
        self.assertEqual(status, 200)
        self.assertIn("קורס שנמחק", data)


class UpgradesTest(unittest.TestCase):
    """The 12 upgrades Daniel approved (2026-11-06): phone
    viewport, screen-reader landmarks, session kind as text,
    the "needs attention" box, countdown + progress, print CSS,
    sample data, .ics export, busy-hours paste, spaced
    repetition, emergency mode, and heavy topics."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        app.DB_PATH = os.path.join(self.tmp.name, "test.db")
        app.init_db()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _request(self, method, path, fields=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        body = urlencode(fields or {}, doseq=True) if fields else None
        headers = {"Content-Type": "application/x-www-form-urlencoded"} \
            if body else {}
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read().decode()
        location = resp.getheader("Location")
        content_type = resp.getheader("Content-Type")
        status = resp.status
        conn.close()
        return status, location, data, content_type

    def _course_id(self, name="Calculus 2"):
        with app.connect() as conn:
            return conn.execute(
                "SELECT id FROM courses WHERE name = ?", (name,)
            ).fetchone()["id"]

    def _add_week_slots(self, start="09:00", end="12:00"):
        with app.connect() as conn:
            conn.execute(
                "INSERT INTO free_slots (weekday, start_time, end_time)"
                " VALUES (?, ?, ?), (?, ?, ?), (?, ?, ?), (?, ?, ?),"
                " (?, ?, ?), (?, ?, ?), (?, ?, ?)",
                tuple(v for d in range(7) for v in (d, start, end)))

    def _add_topics(self, course_id, *titles):
        with app.connect() as conn:
            ids = []
            for title in titles:
                cursor = conn.execute(
                    "INSERT INTO topics (course_id, title) VALUES (?, ?)",
                    (course_id, title),
                )
                ids.append(cursor.lastrowid)
            return ids

    def _add_exam(self, course_id, title="Final", kind="exam",
                  days=14, learn_pct=40, review_pct=20,
                  exercise_pct=20, simulation_pct=20, emergency=False):
        fields = {
            "course_id": str(course_id), "kind": kind, "title": title,
            "exam_date": (date.today() + timedelta(days=days)).isoformat(),
            "learn_pct": str(learn_pct), "review_pct": str(review_pct),
            "exercise_pct": str(exercise_pct),
            "simulation_pct": str(simulation_pct)}
        if emergency:
            fields["emergency"] = "1"
        status, _, _, _ = self._request("POST", "/add-exam", fields)
        self.assertEqual(status, 303)
        with app.connect() as conn:
            return conn.execute(
                "SELECT id FROM exams WHERE title = ?",
                (title,)).fetchone()["id"]

    def _add_assignment(self, course_id, title="Homework", days=7,
                        hours=2, parts=2):
        status, _, _, _ = self._request("POST", "/add-assignment", {
            "course_id": str(course_id), "title": title,
            "due_date": (date.today() + timedelta(days=days)).isoformat(),
            "estimated_hours": str(hours), "num_parts": str(parts)})
        self.assertEqual(status, 303)
        with app.connect() as conn:
            return conn.execute(
                "SELECT id FROM assignments WHERE title = ?",
                (title,)).fetchone()["id"]

    def _session_rows(self, html):
        """Every <li class="session ..."> row on a page."""
        return re.findall(
            r'<li class="session[^"]*">(.*?)</li>', html, re.S)

    # --- 1: phone viewport ------------------------------------------

    def test_viewport_meta_on_every_page(self):
        calc_id = self._course_id()
        self._add_topics(calc_id, "Limits")
        exam_id = self._add_exam(calc_id)
        assignment_id = self._add_assignment(calc_id)
        pages = [
            ("GET", "/", None),
            ("GET", "/courses", None),
            ("GET", "/exams", None),
            ("GET", "/topics", None),
            ("GET", "/assignments", None),
            ("GET", "/slots", None),
            ("GET", "/routine", None),
            ("GET", "/busy", None),
            ("GET", "/add-exam", None),
            ("GET", "/setup", None),
            ("GET", f"/exam?id={exam_id}", None),
            ("GET", f"/assignment?id={assignment_id}", None),
            ("POST", "/parse-syllabus", {
                "course_id": str(calc_id),
                "syllabus": "Topic A\nTopic B"}),
            ("POST", "/parse-busy", {
                "busy_lines": (
                    f"{(date.today() + timedelta(days=3)).isoformat()} "
                    "18:00-20:00 friends")}),
        ]
        for method, path, fields in pages:
            status, _, data, _ = self._request(method, path, fields)
            self.assertEqual(status, 200, path)
            self.assertIn(
                '<meta name="viewport" '
                'content="width=device-width, initial-scale=1">',
                data, path)

    # --- 2: screen-reader landmarks + skip link ---------------------

    def test_landmarks_and_skip_link(self):
        _, _, data, _ = self._request("GET", "/")
        self.assertIn('role="main"', data)
        self.assertIn('aria-label="ניווט ראשי"', data)
        self.assertIn('href="#today"', data)
        self.assertIn("דלג לתוכנית היום", data)
        _, _, exam_data, _ = self._request("GET", "/setup")
        self.assertIn('role="main"', exam_data)

    # --- 3: session kind as text ------------------------------------

    def test_session_kind_shown_as_text(self):
        calc_id = self._course_id()
        self._add_topics(calc_id, "Limits", "Derivatives", "Integrals")
        self._add_week_slots()
        exam_id = self._add_exam(calc_id)
        _, _, home, _ = self._request("GET", "/")
        _, _, exam_page, _ = self._request("GET", f"/exam?id={exam_id}")
        kinds = ("למידה", "חזרה", "תרגול", "סימולציה", "הפסקה",
                 "שיעורי בית")
        for html in (home, exam_page):
            rows = self._session_rows(html)
            self.assertTrue(rows, "no session rows found")
            for row in rows:
                self.assertTrue(
                    any(kind in row for kind in kinds),
                    f"session row without a kind in words: {row[:120]}")

    # --- 4: "needs attention" box -----------------------------------

    def test_attention_box_lists_topicless_exam_and_routine(self):
        calc_id = self._course_id()
        self._add_exam(calc_id)
        _, _, data, _ = self._request("GET", "/")
        self.assertIn("דורש תשומת לב", data)
        self.assertIn("אין נושאים", data)
        self.assertIn("רוטינה", data)

    def test_attention_box_absent_when_nothing_needs_attention(self):
        calc_id = self._course_id()
        self._add_topics(calc_id, "Limits", "Derivatives")
        self._add_week_slots()
        status, _, _, _ = self._request("POST", "/save-prefs", {
            "session_minutes": "50", "break_minutes": "10",
            "homework_pref": "any", "study_style": "variety"})
        self.assertEqual(status, 303)
        self._add_exam(calc_id)
        _, _, data, _ = self._request("GET", "/")
        self.assertNotIn("דורש תשומת לב", data)

    # --- 5: countdown + progress ------------------------------------

    def test_countdown_and_progress(self):
        calc_id = self._course_id()
        topic_ids = self._add_topics(calc_id, "Limits", "Derivatives")
        self._add_week_slots()
        exam_id = self._add_exam(calc_id, days=9)
        status, _, _, _ = self._request(
            "GET", f"/topic-done?id={topic_ids[0]}")
        self.assertEqual(status, 303)
        _, _, home, _ = self._request("GET", "/")
        self.assertIn("עוד 9 ימים — נלמדו 1 מתוך 2 נושאים", home)
        _, _, exam_page, _ = self._request("GET", f"/exam?id={exam_id}")
        self.assertIn("עוד 9 ימים — נלמדו 1 מתוך 2 נושאים", exam_page)

    def test_countdown_today_and_past(self):
        self.assertEqual(
            app.exam_progress_text(
                {"days_left": 0, "learned": 2, "total": 5}),
            "המבחן היום — נלמדו 2 מתוך 5 נושאים")
        self.assertEqual(
            app.exam_progress_text(
                {"days_left": -3, "learned": 5, "total": 5}),
            "המבחן היה לפני 3 ימים — נלמדו 5 מתוך 5 נושאים")

    # --- 6: print CSS ------------------------------------------------

    def test_print_css_hides_forms_nav_and_buttons(self):
        _, _, data, _ = self._request("GET", "/")
        self.assertIn("@media print", data)
        self.assertIn(
            "@media print{.topnav,form,button,.imp-btn,"
            ".setup-banner,.skip-link{display:none}",
            data)

    # --- 7: one-click sample data -----------------------------------

    def test_sample_data_creates_and_deletes_cleanly(self):
        calc_id = self._course_id()
        self._add_topics(calc_id, "Real topic")
        self._add_week_slots()
        # Creating twice must not duplicate the sample.
        for _ in range(2):
            status, _, _, _ = self._request("GET", "/add-sample-data")
            self.assertEqual(status, 303)
        with app.connect() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM courses WHERE name = ?",
                    (app.SAMPLE_COURSE_NAME,)).fetchone()[0], 1)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM topics WHERE course_id = "
                    "(SELECT id FROM courses WHERE name = ?)",
                    (app.SAMPLE_COURSE_NAME,)).fetchone()[0], 4)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM free_slots WHERE sample = 1"
                ).fetchone()[0], 3)
            sample_exams = conn.execute(
                "SELECT id FROM exams WHERE course_id = "
                "(SELECT id FROM courses WHERE name = ?)",
                (app.SAMPLE_COURSE_NAME,)).fetchall()
            self.assertEqual(len(sample_exams), 1)
            self.assertGreater(
                conn.execute(
                    "SELECT COUNT(*) FROM plan_sessions WHERE exam_id = ?",
                    (sample_exams[0]["id"],)).fetchone()[0], 0)
            # The real data is untouched.
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM topics WHERE title = ?",
                    ("Real topic",)).fetchone()[0], 1)
            real_slots = conn.execute(
                "SELECT COUNT(*) FROM free_slots WHERE sample = 0"
            ).fetchone()[0]
        status, _, _, _ = self._request("GET", "/delete-sample-data")
        self.assertEqual(status, 303)
        with app.connect() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM courses WHERE name = ?",
                    (app.SAMPLE_COURSE_NAME,)).fetchone()[0], 0)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM exams WHERE course_id NOT IN "
                    "(SELECT id FROM courses)").fetchone()[0], 0)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM free_slots WHERE sample = 1"
                ).fetchone()[0], 0)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM topics WHERE title = ?",
                    ("Real topic",)).fetchone()[0], 1)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM free_slots WHERE sample = 0"
                ).fetchone()[0], real_slots)
        # Deleting again is a harmless no-op.
        status, _, _, _ = self._request("GET", "/delete-sample-data")
        self.assertEqual(status, 303)

    # --- 8: .ics export ----------------------------------------------

    def _check_ics(self, data):
        self.assertIn("BEGIN:VCALENDAR", data)
        self.assertIn("VERSION:2.0", data)
        self.assertIn("END:VCALENDAR", data)
        events = data.split("BEGIN:VEVENT")[1:]
        self.assertGreater(len(events), 0)
        for event in events:
            for field in ("UID:", "DTSTAMP:", "DTSTART:",
                          "DTEND:", "SUMMARY:"):
                self.assertIn(field, event)
        return events

    def test_ics_export_whole_plan(self):
        calc_id = self._course_id()
        self._add_topics(calc_id, "Limits", "Derivatives")
        self._add_week_slots()
        exam_id = self._add_exam(calc_id)
        self._add_assignment(calc_id)
        status, _, data, content_type = self._request(
            "GET", "/export-plan.ics")
        self.assertEqual(status, 200)
        self.assertIn("text/calendar", content_type)
        events = self._check_ics(data)
        summaries = " ".join(events)
        self.assertIn("Limits", summaries)
        self.assertIn("שיעורי בית", summaries)
        # Per-exam export: this exam's sessions, no assignments.
        status, _, data, _ = self._request(
            "GET", f"/export-exam.ics?exam_id={exam_id}")
        self.assertEqual(status, 200)
        events = self._check_ics(data)
        self.assertNotIn("שיעורי בית", " ".join(events))
        # Unknown exam: back to the exams section.
        status, location, _, _ = self._request(
            "GET", "/export-exam.ics?exam_id=999")
        self.assertEqual(status, 303)
        self.assertIn("exams", location)

    def test_ics_escaping(self):
        calc_id = self._course_id()
        self._add_topics(calc_id, "נושא, עם; סימנים")
        self._add_week_slots()
        self._add_exam(calc_id)
        _, _, data, _ = self._request("GET", "/export-plan.ics")
        self.assertIn("נושא\\, עם\\; סימנים", data)

    # --- 9: busy-hours paste -----------------------------------------

    def test_parse_busy_lines(self):
        day = (date.today() + timedelta(days=3)).isoformat()
        day2 = (date.today() + timedelta(days=4)).isoformat()
        blocks, errors = app.parse_busy_lines(
            f"{day} 18:00-20:00 חברים\n"
            f"{day2} 09:00-10:30\n"
            "\n"
            "garbage line\n"
            f"{(date.today() - timedelta(days=1)).isoformat()} "
            "18:00-20:00 עבר\n"
            f"{day} 25:00-26:00 שעות לא חוקיות")
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0]["reason"], "חברים")
        self.assertEqual(blocks[1]["reason"], "")
        self.assertEqual(len(errors), 3)
        self.assertTrue(all("שורה" in e for e in errors))

    def test_busy_paste_preview_and_save(self):
        day = (date.today() + timedelta(days=3)).isoformat()
        day2 = (date.today() + timedelta(days=4)).isoformat()
        good = (f"{day} 18:00-20:00 חברים\n"
                f"{day2} 09:00-10:30 לימוד")
        status, _, data, _ = self._request(
            "POST", "/parse-busy",
            {"busy_lines": good + "\ngarbage line"})
        self.assertEqual(status, 200)
        self.assertIn("נמצאו 2 חסימות", data)
        self.assertIn("שורה 3", data)
        status, _, _, _ = self._request(
            "POST", "/save-busy", {"busy_lines": good})
        self.assertEqual(status, 303)
        with app.connect() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM busy_blocks").fetchone()[0], 2)
            self.assertEqual(
                conn.execute(
                    "SELECT reason FROM busy_blocks ORDER BY block_date"
                ).fetchall()[0]["reason"], "חברים")

    def test_busy_paste_bad_lines_never_saved(self):
        status, _, data, _ = self._request(
            "POST", "/parse-busy", {"busy_lines": "garbage line\nnope"})
        self.assertEqual(status, 200)
        self.assertIn("לא נמצאו חסימות תקינות", data)
        status, _, data, _ = self._request(
            "POST", "/save-busy", {"busy_lines": "garbage line"})
        self.assertEqual(status, 200)
        self.assertIn("לא נמצאו חסימות תקינות", data)
        with app.connect() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM busy_blocks").fetchone()[0], 0)

    # --- 10: spaced repetition ---------------------------------------

    def test_spaced_repetition_dates(self):
        # One topic learned today, 13 days of 4-hour windows:
        # reviews land exactly 1, 3 and 7 days after learning.
        plan = app.generate_plan(
            _topics(1),
            [{"weekday": d, "start_time": "09:00", "end_time": "13:00"}
             for d in range(7)],
            date.today() + timedelta(days=13),
        )
        review = [s for s in plan["sessions"] if s["kind"] == "review"]
        self.assertEqual(len(review), 3)
        self.assertEqual(
            sorted(s["session_date"] for s in review),
            [date.today() + timedelta(days=1),
             date.today() + timedelta(days=3),
             date.today() + timedelta(days=7)])

    def test_spaced_repetition_skips_blocked_days(self):
        # The +1 day is fully busy: that review is skipped
        # honestly, and the +3 and +7 reviews still happen.
        busy = [{"block_date": date.today() + timedelta(days=1),
                 "start_time": "09:00", "end_time": "13:00",
                 "reason": "busy"}]
        plan = app.generate_plan(
            _topics(1),
            [{"weekday": d, "start_time": "09:00", "end_time": "13:00"}
             for d in range(7)],
            date.today() + timedelta(days=13),
            busy_blocks=busy,
        )
        review = [s for s in plan["sessions"] if s["kind"] == "review"]
        self.assertEqual(len(review), 2)
        self.assertEqual(
            sorted(s["session_date"] for s in review),
            [date.today() + timedelta(days=3),
             date.today() + timedelta(days=7)])

    # --- 11: emergency mode ------------------------------------------

    def test_emergency_mode_only_very_important_no_simulation(self):
        calc_id = self._course_id()
        topic_ids = self._add_topics(calc_id, "A", "B", "C")
        with app.connect() as conn:
            conn.execute(
                "UPDATE topics SET importance = 'high' WHERE id = ?",
                (topic_ids[0],))
            conn.execute(
                "UPDATE topics SET importance = 'medium' WHERE id = ?",
                (topic_ids[1],))
            conn.execute(
                "UPDATE topics SET importance = 'low' WHERE id = ?",
                (topic_ids[2],))
        self._add_week_slots()
        exam_id = self._add_exam(calc_id, emergency=True)
        _, _, exam_page, _ = self._request("GET", f"/exam?id={exam_id}")
        self.assertIn("מצב חירום פעיל", exam_page)
        with app.connect() as conn:
            sessions = app.get_plan_sessions(conn, exam_id)
        kinds = {}
        for s in sessions:
            kinds[s["kind"]] = kinds.get(s["kind"], 0) + 1
        learn_topics = {s["topic_id"] for s in sessions
                        if s["kind"] == "learn"}
        self.assertEqual(learn_topics, {topic_ids[0]})
        self.assertEqual(kinds.get("simulation", 0), 0)
        # Turning it off restores the normal plan: all topics
        # learned, and a simulation session is back.
        status, _, _, _ = self._request("POST", "/save-exam", {
            "exam_id": str(exam_id), "title": "Final",
            "exam_date": (date.today() + timedelta(days=14)).isoformat(),
            "kind": "exam", "learn_pct": "40", "review_pct": "20",
            "exercise_pct": "20", "simulation_pct": "20"})
        self.assertEqual(status, 303)
        with app.connect() as conn:
            sessions = app.get_plan_sessions(conn, exam_id)
            self.assertEqual(
                {s["topic_id"] for s in sessions
                 if s["kind"] == "learn"},
                set(topic_ids))
            self.assertEqual(
                len([s for s in sessions
                     if s["kind"] == "simulation"]), 1)
        _, _, exam_page, _ = self._request("GET", f"/exam?id={exam_id}")
        self.assertNotIn("מצב חירום פעיל", exam_page)

    # --- 12: heavy topic ----------------------------------------------

    def test_heavy_topic_gets_two_learn_sessions(self):
        plan = app.generate_plan(
            [{"id": 1, "title": "Heavy", "learned": 0, "heavy": 1},
             {"id": 2, "title": "Light", "learned": 0, "heavy": 0}],
            _all_week_slots(), date.today() + timedelta(days=7),
            learn_pct=100, review_pct=0, exercise_pct=0,
            simulation_pct=0,
        )
        learn = [s for s in plan["sessions"] if s["kind"] == "learn"]
        counts = {}
        for s in learn:
            counts[s["topic_id"]] = counts.get(s["topic_id"], 0) + 1
        self.assertEqual(counts, {1: 2, 2: 1})
        self.assertEqual(plan["unscheduled"], [])

    def test_heavy_topic_toggle(self):
        calc_id = self._course_id()
        topic_id = self._add_topics(calc_id, "Hard topic")[0]
        status, _, _, _ = self._request(
            "GET", f"/topic-heavy?id={topic_id}&heavy=1")
        self.assertEqual(status, 303)
        with app.connect() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT heavy FROM topics WHERE id = ?",
                    (topic_id,)).fetchone()["heavy"], 1)
        _, _, data, _ = self._request("GET", "/?open=topics#topics")
        self.assertIn("נושא כבד", data)
        status, _, _, _ = self._request(
            "GET", f"/topic-heavy?id={topic_id}&heavy=0")
        self.assertEqual(status, 303)
        with app.connect() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT heavy FROM topics WHERE id = ?",
                    (topic_id,)).fetchone()["heavy"], 0)


class LeadDaysTest(unittest.TestCase):
    """Lead time (new feature, 2026-11-06): each exam carries how
    many days before it learning may start (1–30, default 14) —
    Daniel does not study three months ahead for an exam, and a
    month is the maximum."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        app.DB_PATH = os.path.join(self.tmp.name, "test.db")
        app.init_db()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _request(self, method, path, fields=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        body = urlencode(fields or {}, doseq=True) if fields else None
        headers = {"Content-Type": "application/x-www-form-urlencoded"} \
            if body else {}
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read().decode()
        location = resp.getheader("Location")
        status = resp.status
        conn.close()
        return status, location, data

    def _course_id(self, name="Calculus 2"):
        with app.connect() as conn:
            return conn.execute(
                "SELECT id FROM courses WHERE name = ?", (name,)
            ).fetchone()["id"]

    def _add_week_slots(self, start="09:00", end="12:00"):
        with app.connect() as conn:
            conn.execute(
                "INSERT INTO free_slots (weekday, start_time, end_time)"
                " VALUES (?, ?, ?), (?, ?, ?), (?, ?, ?), (?, ?, ?),"
                " (?, ?, ?), (?, ?, ?), (?, ?, ?)",
                tuple(v for d in range(7) for v in (d, start, end)))

    def _add_topics(self, course_id, *titles):
        with app.connect() as conn:
            ids = []
            for title in titles:
                cursor = conn.execute(
                    "INSERT INTO topics (course_id, title) VALUES (?, ?)",
                    (course_id, title),
                )
                ids.append(cursor.lastrowid)
            return ids

    def _add_exam(self, course_id, title="Final", kind="exam",
                  days=14, lead_days=None, emergency=False):
        fields = {
            "course_id": str(course_id), "kind": kind, "title": title,
            "exam_date": (date.today() + timedelta(days=days)).isoformat(),
            "learn_pct": "40", "review_pct": "20",
            "exercise_pct": "20", "simulation_pct": "20"}
        if lead_days is not None:
            fields["lead_days"] = str(lead_days)
        if emergency:
            fields["emergency"] = "1"
        status, _, _ = self._request("POST", "/add-exam", fields)
        self.assertEqual(status, 303)
        with app.connect() as conn:
            return conn.execute(
                "SELECT id FROM exams WHERE title = ?",
                (title,)).fetchone()["id"]

    def _session_dates(self, exam_id):
        with app.connect() as conn:
            return [date.fromisoformat(s["session_date"]) for s in
                    app.get_plan_sessions(conn, exam_id)]

    def _exam_count(self):
        with app.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM exams").fetchone()[0]

    # --- Validation -----------------------------------------------

    def test_parse_and_validate_lead_days(self):
        self.assertEqual(app.parse_lead_days(""), 14)
        self.assertEqual(app.parse_lead_days("30"), 30)
        self.assertEqual(app.parse_lead_days("1"), 1)
        self.assertIsNone(app.parse_lead_days("abc"))
        self.assertIsNone(app.parse_lead_days("3.5"))
        self.assertTrue(app.validate_lead_days(1))
        self.assertTrue(app.validate_lead_days(30))
        self.assertFalse(app.validate_lead_days(0))
        self.assertFalse(app.validate_lead_days(31))
        self.assertFalse(app.validate_lead_days(-5))
        self.assertFalse(app.validate_lead_days(None))
        self.assertFalse(app.validate_lead_days("14"))
        self.assertEqual(app.exam_lead_days({}), 14)
        self.assertEqual(app.exam_lead_days({"lead_days": 7}), 7)
        self.assertEqual(app.exam_lead_days({"lead_days": 99}), 14)
        self.assertEqual(app.exam_lead_days({"lead_days": "abc"}), 14)

    # --- The window rule, at the generate_plan level --------------

    def test_far_exam_starts_within_lead(self):
        # Exam 60 days out, lead 30: nothing before exam - 30,
        # and learning does start on that first window day.
        plan = app.generate_plan(
            _topics(3), _all_week_slots(),
            date.today() + timedelta(days=60), lead_days=30)
        dates = [s["session_date"] for s in plan["sessions"]]
        self.assertTrue(dates)
        self.assertEqual(min(dates), date.today() + timedelta(days=30))

    def test_near_exam_starts_today(self):
        # Exam 10 days out, lead 30: the window opened before
        # today, so today wins and learning starts today.
        plan = app.generate_plan(
            _topics(3), _all_week_slots(),
            date.today() + timedelta(days=10), lead_days=30)
        dates = [s["session_date"] for s in plan["sessions"]]
        self.assertTrue(dates)
        self.assertEqual(min(dates), date.today())

    # --- The window rule, live through the app --------------------

    def test_far_exam_live_starts_at_window(self):
        calc_id = self._course_id()
        self._add_topics(calc_id, "A", "B", "C")
        self._add_week_slots()
        exam_id = self._add_exam(calc_id, days=60, lead_days=30)
        exam_date = date.today() + timedelta(days=60)
        dates = self._session_dates(exam_id)
        self.assertTrue(dates)
        self.assertEqual(min(dates), exam_date - timedelta(days=30))

    def test_near_exam_live_starts_today(self):
        calc_id = self._course_id()
        self._add_topics(calc_id, "A", "B", "C")
        self._add_week_slots()
        exam_id = self._add_exam(calc_id, days=10, lead_days=30)
        dates = self._session_dates(exam_id)
        self.assertTrue(dates)
        self.assertEqual(min(dates), date.today())

    def test_default_lead_is_14(self):
        # No lead_days field at all: the exam stores 14, and an
        # exam 60 days out starts learning 14 days before it.
        calc_id = self._course_id()
        self._add_topics(calc_id, "A", "B", "C")
        self._add_week_slots()
        exam_id = self._add_exam(calc_id, days=60)
        with app.connect() as conn:
            stored = conn.execute(
                "SELECT lead_days FROM exams WHERE id = ?",
                (exam_id,)).fetchone()["lead_days"]
        self.assertEqual(stored, 14)
        exam_date = date.today() + timedelta(days=60)
        dates = self._session_dates(exam_id)
        self.assertTrue(dates)
        self.assertEqual(min(dates), exam_date - timedelta(days=14))

    # --- Invalid values are rejected, nothing saved ---------------

    def test_invalid_lead_rejected(self):
        calc_id = self._course_id()
        self._add_topics(calc_id, "A", "B")
        self._add_week_slots()
        for bad in ("0", "31", "-5", "abc"):
            status, _, data = self._request("POST", "/add-exam", {
                "course_id": str(calc_id), "kind": "exam",
                "title": "Final",
                "exam_date": (date.today()
                              + timedelta(days=14)).isoformat(),
                "learn_pct": "40", "review_pct": "20",
                "exercise_pct": "20", "simulation_pct": "20",
                "lead_days": bad})
            self.assertEqual(status, 200)  # error page, not a redirect
            self.assertIn("מספר הימים לא תקין", data)
            self.assertNotIn("נשמר ✓", data)
        self.assertEqual(self._exam_count(), 0)

    # --- The exam page shows the value, editing regenerates ------

    def test_exam_page_shows_lead_and_forms_offer_it(self):
        calc_id = self._course_id()
        self._add_topics(calc_id, "A", "B")
        self._add_week_slots()
        exam_id = self._add_exam(calc_id, days=60, lead_days=30)
        _, _, exam_page = self._request("GET", f"/exam?id={exam_id}")
        self.assertIn("30 ימים לפני המבחן", exam_page)
        self.assertIn('value="30"', exam_page)
        # lead_days is offered on step 3 of the flow now, not
        # on the home page.
        _, _, step3 = self._request(
            "GET", "/add-exam?step=3&course_id=" + str(calc_id) +
            "&kind=exam&title=Final&exam_date=" +
            (date.today() + timedelta(days=60)).isoformat())
        self.assertIn('name="lead_days"', step3)
        _, _, wizard = self._request("GET", "/setup?step=5")
        self.assertIn('name="lead_days"', wizard)

    def test_edit_lead_regenerates_plan(self):
        calc_id = self._course_id()
        self._add_topics(calc_id, "A", "B", "C")
        self._add_week_slots()
        exam_id = self._add_exam(calc_id, days=60, lead_days=30)
        exam_date = date.today() + timedelta(days=60)
        self.assertEqual(
            min(self._session_dates(exam_id)),
            exam_date - timedelta(days=30))
        status, _, _ = self._request("POST", "/save-exam", {
            "exam_id": str(exam_id), "title": "Final",
            "exam_date": exam_date.isoformat(), "kind": "exam",
            "learn_pct": "40", "review_pct": "20",
            "exercise_pct": "20", "simulation_pct": "20",
            "lead_days": "7"})
        self.assertEqual(status, 303)
        dates = self._session_dates(exam_id)
        self.assertTrue(dates)
        self.assertEqual(min(dates), exam_date - timedelta(days=7))
        _, _, exam_page = self._request("GET", f"/exam?id={exam_id}")
        self.assertIn("7 ימים לפני המבחן", exam_page)
        self.assertIn('value="7"', exam_page)

    def test_edit_with_bad_lead_rejected(self):
        calc_id = self._course_id()
        self._add_topics(calc_id, "A", "B")
        self._add_week_slots()
        exam_id = self._add_exam(calc_id, days=60, lead_days=30)
        exam_date = date.today() + timedelta(days=60)
        status, _, data = self._request("POST", "/save-exam", {
            "exam_id": str(exam_id), "title": "Final",
            "exam_date": exam_date.isoformat(), "kind": "exam",
            "learn_pct": "40", "review_pct": "20",
            "exercise_pct": "20", "simulation_pct": "20",
            "lead_days": "31"})
        self.assertEqual(status, 200)
        self.assertIn("מספר הימים לא תקין", data)
        with app.connect() as conn:
            stored = conn.execute(
                "SELECT lead_days FROM exams WHERE id = ?",
                (exam_id,)).fetchone()["lead_days"]
        self.assertEqual(stored, 30)
        self.assertEqual(
            min(self._session_dates(exam_id)),
            exam_date - timedelta(days=30))

    # --- Spaced repetition and emergency mode inside the window ---

    def test_spaced_reviews_stay_inside_window(self):
        calc_id = self._course_id()
        self._add_topics(calc_id, "A", "B")
        self._add_week_slots()
        exam_id = self._add_exam(calc_id, days=60, lead_days=30)
        exam_date = date.today() + timedelta(days=60)
        window_start = exam_date - timedelta(days=30)
        with app.connect() as conn:
            sessions = app.get_plan_sessions(conn, exam_id)
        first_learn = {}
        for s in sessions:
            if s["kind"] == "learn":
                day = date.fromisoformat(s["session_date"])
                first_learn.setdefault(s["topic_id"], day)
        reviews = [s for s in sessions if s["kind"] == "review"]
        self.assertTrue(reviews)
        for s in reviews:
            day = date.fromisoformat(s["session_date"])
            self.assertGreaterEqual(day, window_start)
            gap = (day - first_learn[s["topic_id"]]).days
            self.assertIn(gap, (1, 3, 7))

    def test_emergency_mode_inside_window(self):
        calc_id = self._course_id()
        topic_ids = self._add_topics(calc_id, "A", "B", "C")
        with app.connect() as conn:
            conn.execute(
                "UPDATE topics SET importance = 'high' WHERE id = ?",
                (topic_ids[0],))
            conn.execute(
                "UPDATE topics SET importance = 'medium' WHERE id = ?",
                (topic_ids[1],))
            conn.execute(
                "UPDATE topics SET importance = 'low' WHERE id = ?",
                (topic_ids[2],))
        self._add_week_slots()
        exam_id = self._add_exam(calc_id, days=60, lead_days=30,
                                 emergency=True)
        exam_date = date.today() + timedelta(days=60)
        window_start = exam_date - timedelta(days=30)
        with app.connect() as conn:
            sessions = app.get_plan_sessions(conn, exam_id)
        self.assertTrue(sessions)
        for s in sessions:
            self.assertGreaterEqual(
                date.fromisoformat(s["session_date"]), window_start)
        kinds = {}
        for s in sessions:
            kinds[s["kind"]] = kinds.get(s["kind"], 0) + 1
        self.assertEqual(kinds.get("simulation", 0), 0)
        learn_topics = {s["topic_id"] for s in sessions
                        if s["kind"] == "learn"}
        self.assertEqual(learn_topics, {topic_ids[0]})

    # --- Old database: migration adds the column and rebuilds ----

    def test_old_database_gets_lead_and_rebuilt_plan(self):
        # A database from before this feature: an exams table
        # without lead_days, and a plan session 50 days before a
        # 60-day-out exam (learning far too early). Opening it
        # must add the column (default 14) and rebuild the plan
        # inside the window — the too-early session is gone.
        old_schema = app.SCHEMA.replace(
            ",\n    lead_days INTEGER NOT NULL DEFAULT 14", "")
        self.assertNotIn("lead_days", old_schema)
        exam_date = date.today() + timedelta(days=60)
        with app.connect() as conn:
            # Rebuild the exams table the old way: setUp already
            # created it with the new column, so drop it first —
            # otherwise CREATE TABLE IF NOT EXISTS would keep the
            # column and the migration would never run.
            conn.execute("DROP TABLE IF EXISTS exams")
            conn.executescript(old_schema)
            # setUp already seeded "Calculus 2" — reuse it.
            course_id = conn.execute(
                "SELECT id FROM courses WHERE name = 'Calculus 2'"
            ).fetchone()["id"]
            cursor = conn.execute(
                "INSERT INTO topics (course_id, title) VALUES (?, 'A')",
                (course_id,))
            topic_id = cursor.lastrowid
            cursor = conn.execute(
                "INSERT INTO exams (course_id, kind, title, exam_date)"
                " VALUES (?, 'exam', 'Final', ?)",
                (course_id, exam_date.isoformat()))
            exam_id = cursor.lastrowid
            conn.execute(
                "INSERT INTO exam_topics (exam_id, topic_id)"
                " VALUES (?, ?)", (exam_id, topic_id))
            for d in range(7):
                conn.execute(
                    "INSERT INTO free_slots (weekday, start_time, end_time)"
                    " VALUES (?, '09:00', '12:00')", (d,))
            conn.execute(
                "INSERT INTO plan_sessions (exam_id, topic_id,"
                " session_date, start_time, end_time, kind)"
                " VALUES (?, ?, ?, '09:00', '09:50', 'learn')",
                (exam_id, topic_id,
                 (exam_date - timedelta(days=50)).isoformat()))
        app.init_db()
        with app.connect() as conn:
            stored = conn.execute(
                "SELECT lead_days FROM exams WHERE id = ?",
                (exam_id,)).fetchone()["lead_days"]
            self.assertEqual(stored, 14)
            dates = [date.fromisoformat(s["session_date"]) for s in
                     app.get_plan_sessions(conn, exam_id)]
        self.assertTrue(dates)
        self.assertGreaterEqual(
            min(dates), exam_date - timedelta(days=14))

class RedesignTest(unittest.TestCase):
    """UI redesign (2026-11-07): the home page is a daily dashboard
    only — today first, the rest condensed — the settings live on
    their own pages, the exam flow is three short steps, and the
    server listens on every interface so a phone on the same Wi-Fi
    can open it. Daniel asked for all three improvements at once
    ("תשנה גם וגם וגם")."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        app.DB_PATH = os.path.join(self.tmp.name, "test.db")
        app.init_db()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _request(self, method, path, fields=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        body = urlencode(fields or {}, doseq=True) if fields else None
        headers = {"Content-Type": "application/x-www-form-urlencoded"} \
            if body else {}
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read().decode()
        location = resp.getheader("Location")
        status = resp.status
        conn.close()
        return status, location, data

    def _course_id(self, name="Calculus 2"):
        with app.connect() as conn:
            return conn.execute(
                "SELECT id FROM courses WHERE name = ?", (name,)
            ).fetchone()["id"]

    def _add_week_slots(self, start="09:00", end="12:00"):
        with app.connect() as conn:
            conn.execute(
                "INSERT INTO free_slots (weekday, start_time, end_time)"
                " VALUES (?, ?, ?), (?, ?, ?), (?, ?, ?), (?, ?, ?),"
                " (?, ?, ?), (?, ?, ?), (?, ?, ?)",
                tuple(v for d in range(7) for v in (d, start, end)))

    def _add_topics(self, course_id, *titles):
        with app.connect() as conn:
            ids = []
            for title in titles:
                cursor = conn.execute(
                    "INSERT INTO topics (course_id, title) VALUES (?, ?)",
                    (course_id, title),
                )
                ids.append(cursor.lastrowid)
            return ids

    def _add_exam(self, course_id, title="Final", kind="exam",
                  days=14, lead_days=None):
        fields = {
            "course_id": str(course_id), "kind": kind, "title": title,
            "exam_date": (date.today() + timedelta(days=days)).isoformat(),
            "learn_pct": "40", "review_pct": "20",
            "exercise_pct": "20", "simulation_pct": "20"}
        if lead_days is not None:
            fields["lead_days"] = str(lead_days)
        status, _, _ = self._request("POST", "/add-exam", fields)
        self.assertEqual(status, 303)
        with app.connect() as conn:
            return conn.execute(
                "SELECT id FROM exams WHERE title = ?",
                (title,)).fetchone()["id"]

    def _add_session(self, exam_id, topic_id, day, kind="learn",
                     start="09:00", end="09:50"):
        with app.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO plan_sessions (exam_id, topic_id, kind,"
                " session_date, start_time, end_time, done)"
                " VALUES (?, ?, ?, ?, ?, ?, 0)",
                (exam_id, topic_id, kind, day.isoformat(), start, end))
            return cursor.lastrowid

    # --- 1: today-first dashboard --------------------------------------

    def test_dashboard_shows_today_first_and_no_section_forms(self):
        calc_id = self._course_id()
        self._add_topics(calc_id, "Limits")
        self._add_week_slots()
        self._add_exam(calc_id)
        _, _, data = self._request("GET", "/")
        # Today first, then the rest of the week, then the peeks.
        self.assertLess(data.index('id="today"'),
                        data.index('id="future"'))
        self.assertLess(data.index('id="future"'),
                        data.index('id="exams-peek"'))
        # No settings section lives on the dashboard any more.
        for marker in ('id="routine"', 'id="courses"', 'id="slots"',
                       'id="exams"', 'id="topics"', 'id="busy"',
                       'id="assignments"'):
            self.assertNotIn(marker, data)
        for field in ('name="session_minutes"', 'name="syllabus"',
                      'name="estimated_hours"', 'name="lead_days"'):
            self.assertNotIn(field, data)
        # The slim nav links to the five pages.
        for href in ('href="/"', 'href="/exams"', 'href="/courses"',
                     'href="/slots"', 'href="/routine"'):
            self.assertIn(href, data)
        self.assertIn('aria-current="page"', data)

    def test_dashboard_has_timeline_progress_aria_live_and_dark_mode(self):
        calc_id = self._course_id()
        self._add_topics(calc_id, "Limits")
        self._add_week_slots()
        self._add_exam(calc_id)
        _, _, data = self._request("GET", "/")
        self.assertIn('class="timeline"', data)
        self.assertIn('class="tl-time"', data)
        self.assertIn('class="tl-kind"', data)
        self.assertIn('class="progress"', data)
        self.assertIn('aria-live="polite"', data)
        self.assertIn('@media (prefers-color-scheme: dark)', data)
        self.assertIn('class="done-btn"', data)
        self.assertNotIn("<script", data)
        _, _, courses = self._request("GET", "/courses")
        self.assertNotIn("<script", courses)

    def test_old_open_anchor_still_renders_full_page(self):
        calc_id = self._course_id()
        self._add_topics(calc_id, "Limits")
        status, _, data = self._request("GET", "/?open=exams")
        self.assertEqual(status, 200)
        self.assertIn('<details class="card" id="exams" open', data)
        self.assertIn('href="/add-exam"', data)
        status, _, data = self._request("GET", "/?open=topics")
        self.assertEqual(status, 200)
        self.assertIn('<details class="card" id="topics" open', data)
        # ?open=today is the dashboard, not the full page.
        _, _, data = self._request("GET", "/?open=today")
        self.assertIn('id="today"', data)
        self.assertNotIn('name="session_minutes"', data)

    # --- 2: separate pages ---------------------------------------------

    def test_each_section_route_loads_with_its_form(self):
        calc_id = self._course_id()
        self._add_topics(calc_id, "Limits")
        self._add_exam(calc_id)
        markers = {
            "/courses": 'name="name"',
            "/exams": 'href="/add-exam"',
            "/topics": 'name="syllabus"',
            "/assignments": 'name="estimated_hours"',
            "/slots": 'id="slot_weekday"',
            "/routine": 'name="session_minutes"',
            "/busy": 'name="busy_lines"',
        }
        for path, marker in markers.items():
            status, _, data = self._request("GET", path)
            self.assertEqual(status, 200, path)
            self.assertIn(marker, data, path)
            self.assertIn('aria-label="ניווט ראשי"', data, path)
            self.assertIn('aria-live="polite"', data, path)

    def test_field_error_on_section_page(self):
        status, _, data = self._request(
            "POST", "/add-course",
            {"name": "", "next": "/courses?saved=1"})
        self.assertEqual(status, 200)
        self.assertIn('id="name-error"', data)
        self.assertIn("שם הקורס חסר", data)
        with app.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM courses")
                .fetchone()[0], 3)

    # --- 3: stepped exam flow ------------------------------------------

    def test_stepped_flow_end_to_end(self):
        calc_id = self._course_id()
        topic_ids = self._add_topics(calc_id, "Limits", "Derivatives")
        self._add_week_slots()
        exam_date = (date.today() + timedelta(days=14)).isoformat()
        # Step 1: a bad date is rejected with the error under
        # its field, and nothing is saved.
        status, _, data = self._request("POST", "/add-exam-step1", {
            "course_id": str(calc_id), "kind": "quiz", "title": "Quiz",
            "exam_date": (date.today() - timedelta(days=1)).isoformat()})
        self.assertEqual(status, 200)
        self.assertIn('id="exam_date-error"', data)
        self.assertIn("התאריך לא הגיוני", data)
        with app.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM exams")
                .fetchone()[0], 0)
        # Step 1 valid -> step 2.
        status, location, _ = self._request("POST", "/add-exam-step1", {
            "course_id": str(calc_id), "kind": "quiz", "title": "Quiz",
            "exam_date": exam_date})
        self.assertEqual(status, 303)
        self.assertIn("/add-exam?step=2", location)
        _, _, data = self._request("GET", location)
        self.assertIn("שלב 2 מתוך 3", data)
        self.assertIn("Limits", data)
        # Step 2: a quiz with no topics picked is rejected.
        status, _, data = self._request("POST", "/add-exam-step2", {
            "course_id": str(calc_id), "kind": "quiz", "title": "Quiz",
            "exam_date": exam_date})
        self.assertEqual(status, 200)
        self.assertIn("בוחן חייב לכלול לפחות נושא אחד", data)
        with app.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM exams")
                .fetchone()[0], 0)
        # Step 2 with topics -> step 3.
        status, location, _ = self._request("POST", "/add-exam-step2", {
            "course_id": str(calc_id), "kind": "quiz", "title": "Quiz",
            "exam_date": exam_date,
            "topic_id": [str(t) for t in topic_ids]})
        self.assertEqual(status, 303)
        self.assertIn("/add-exam?step=3", location)
        _, _, data = self._request("GET", location)
        self.assertIn("שלב 3 מתוך 3", data)
        self.assertIn('name="lead_days"', data)
        # Step 3 saves, creates the exam and its plan.
        status, location, _ = self._request("POST", "/add-exam-step3", {
            "course_id": str(calc_id), "kind": "quiz", "title": "Quiz",
            "exam_date": exam_date,
            "topic_id": [str(t) for t in topic_ids],
            "learn_pct": "40", "review_pct": "20",
            "exercise_pct": "20", "simulation_pct": "20",
            "lead_days": "14"})
        self.assertEqual(status, 303)
        self.assertIn("/exam?id=", location)
        with app.connect() as conn:
            exam_id = conn.execute(
                "SELECT id FROM exams WHERE title = 'Quiz'"
            ).fetchone()["id"]
            self.assertGreater(
                conn.execute(
                    "SELECT COUNT(*) FROM plan_sessions WHERE exam_id = ?",
                    (exam_id,)).fetchone()[0], 0)

    def test_step_without_draft_redirects_to_step1(self):
        status, location, _ = self._request("GET", "/add-exam?step=2")
        self.assertEqual(status, 303)
        self.assertEqual(location, "/add-exam?step=1")
        status, location, _ = self._request("GET", "/add-exam?step=3")
        self.assertEqual(status, 303)
        self.assertEqual(location, "/add-exam?step=1")

    def test_marking_done_from_dashboard_works(self):
        calc_id = self._course_id()
        topic_id = self._add_topics(calc_id, "Limits")[0]
        exam_id = self._add_exam(calc_id)
        session_id = self._add_session(exam_id, topic_id, date.today())
        status, _, _ = self._request(
            "GET", f"/session-done?id={session_id}")
        self.assertEqual(status, 303)
        _, _, data = self._request("GET", "/")
        self.assertIn("הושלמו היום 1 מתוך 1", data)
        self.assertIn(f"/session-undone?id={session_id}", data)

    # --- 4: phone access + preview cancel ------------------------------

    def test_host_is_all_interfaces(self):
        # 0.0.0.0: a phone on the same home Wi-Fi opens
        # http://<PC-IP>:8000. 127.0.0.1 would only work on the PC.
        self.assertEqual(app.HOST, "0.0.0.0")
        self.assertEqual(app.PORT, 8000)

    def test_strip_saved_keeps_wizard_step(self):
        self.assertEqual(app._strip_saved("/topics?saved=1"), "/topics")
        self.assertEqual(
            app._strip_saved("/?open=topics&saved=1#topics"),
            "/?open=topics#topics")
        self.assertEqual(
            app._strip_saved("/setup?step=2"), "/setup?step=2")
        self.assertEqual(app._strip_saved(""), "")


class WizardFixTest(unittest.TestCase):
    """The wizard actually works: the CSS is styled (not printed
    as text), every save advances to the next step with "נשמר ✓",
    errors stay in the wizard, and every feature is reachable
    from the nav. Live HTTP against a temp DB, like ServerTest."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        app.DB_PATH = os.path.join(self.tmp.name, "test.db")
        app.init_db()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _request(self, method, path, fields=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        body = urlencode(fields or {}, doseq=True) if fields else None
        headers = {"Content-Type": "application/x-www-form-urlencoded"} \
            if body else {}
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read().decode()
        location = resp.getheader("Location")
        status = resp.status
        conn.close()
        return status, location, data

    def _course_id(self, name="Calculus 2"):
        with app.connect() as conn:
            return conn.execute(
                "SELECT id FROM courses WHERE name = ?", (name,)
            ).fetchone()["id"]

    def _assert_styled(self, data):
        # The stylesheet is inside <style>…</style>, never printed
        # as page text: ":root{" must come after "<style>".
        self.assertIn("<style>", data)
        self.assertLess(data.index("<style>"), data.index(":root{"))

    def test_style_tag_on_every_rendered_page(self):
        # The five pages that used to print the raw CSS as text:
        # wizard, exam page, assignment page, both preview pages.
        calc_id = self._course_id()
        with app.connect() as conn:
            conn.execute(
                "INSERT INTO topics (course_id, title) VALUES (?, 'Limits')",
                (calc_id,))
            conn.execute(
                "INSERT INTO exams (course_id, kind, title, exam_date)"
                " VALUES (?, 'exam', 'Final', ?)",
                (calc_id, (date.today() + timedelta(days=14)).isoformat()))
            exam_id = conn.execute(
                "SELECT id FROM exams").fetchone()["id"]
            conn.execute(
                "INSERT INTO assignments (course_id, title, due_date,"
                " estimated_hours) VALUES (?, 'HW', ?, 2)",
                (calc_id, (date.today() + timedelta(days=7)).isoformat()))
            assignment_id = conn.execute(
                "SELECT id FROM assignments").fetchone()["id"]
        for path in ("/setup", f"/exam?id={exam_id}",
                     f"/assignment?id={assignment_id}"):
            status, _, data = self._request("GET", path)
            self.assertEqual(status, 200, path)
            self._assert_styled(data)
        status, _, data = self._request("POST", "/parse-syllabus", {
            "course_id": str(calc_id), "syllabus": "Limits\nDerivatives"})
        self.assertEqual(status, 200)
        self._assert_styled(data)
        status, _, data = self._request("POST", "/parse-busy", {
            "busy_lines": "2026-11-10 18:00-20:00 חברים"})
        self.assertEqual(status, 200)
        self._assert_styled(data)

    def test_wizard_walk_all_six_steps(self):
        # Step 1: add a course — saves and advances to step 2.
        status, location, _ = self._request("POST", "/add-course", {
            "name": "קורס אשף", "teacher": "",
            "next": "/setup?step=2", "wizard_step": "1"})
        self.assertEqual(status, 303)
        self.assertEqual(location, "/setup?step=2&saved=1")
        status, _, data = self._request("GET", "/setup?step=2&saved=1")
        self.assertEqual(status, 200)
        self.assertIn("שלב 2 מתוך 6", data)
        self.assertIn("נשמר ✓", data)
        # Step 2: paste a syllabus, confirm the preview, save —
        # the preview's cancel goes back to step 2, the save
        # advances to step 3.
        calc_id = self._course_id()
        status, _, data = self._request("POST", "/parse-syllabus", {
            "course_id": str(calc_id), "syllabus": "נושא א\nנושא ב\nנושא ג",
            "next": "/setup?step=3", "wizard_step": "2"})
        self.assertEqual(status, 200)
        self.assertIn("נמצאו 3 נושאים", data)
        self.assertIn('href="/setup?step=2"', data)
        status, location, _ = self._request("POST", "/save-topics", {
            "course_id": str(calc_id),
            "topics": "נושא א\nנושא ב\nנושא ג",
            "next": "/setup?step=3", "wizard_step": "2"})
        self.assertEqual(status, 303)
        self.assertEqual(location, "/setup?step=3&saved=1")
        status, _, data = self._request("GET", "/setup?step=3&saved=1")
        self.assertIn("שלב 3 מתוך 6", data)
        self.assertIn("נשמר ✓", data)
        # Step 3: add a free slot — advances to step 4.
        status, location, _ = self._request("POST", "/add-slot", {
            "weekday": "6", "start_time": "09:00", "end_time": "12:00",
            "next": "/setup?step=4", "wizard_step": "3"})
        self.assertEqual(status, 303)
        self.assertEqual(location, "/setup?step=4&saved=1")
        # Step 4: save the routine — advances to step 5.
        status, location, _ = self._request("POST", "/save-prefs", {
            "session_minutes": "50", "break_minutes": "10",
            "homework_pref": "morning", "study_style": "variety",
            "next": "/setup?step=5", "wizard_step": "4"})
        self.assertEqual(status, 303)
        self.assertEqual(location, "/setup?step=5&saved=1")
        # Step 5: add a quiz — advances to the summary.
        with app.connect() as conn:
            topic_ids = [r["id"] for r in conn.execute(
                "SELECT id FROM topics ORDER BY id")]
        status, location, _ = self._request("POST", "/add-exam", {
            "course_id": str(calc_id), "kind": "quiz",
            "title": "בוחן אשף",
            "exam_date": (date.today() + timedelta(days=14)).isoformat(),
            "learn_pct": "40", "review_pct": "20",
            "exercise_pct": "20", "simulation_pct": "20",
            "lead_days": "14", "topic_id": topic_ids,
            "next": "/setup?step=6", "wizard_step": "5"})
        self.assertEqual(status, 303)
        self.assertEqual(location, "/setup?step=6&saved=1")
        # Step 6: the summary counts everything that was saved.
        status, _, data = self._request("GET", "/setup?step=6")
        self.assertEqual(status, 200)
        self.assertIn("שלב 6 מתוך 6", data)
        self.assertIn("4 קורסים", data)
        self.assertIn("3 נושאים", data)
        self.assertIn("1 חלונות פנויים", data)
        self.assertIn("1 מבחנים", data)
        with app.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM exams").fetchone()[0], 1)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0], 3)

    def test_wizard_walk_with_topics_already_existing(self):
        # Topics from before: step 2 lists them, adding one more
        # still advances, and nothing is lost.
        calc_id = self._course_id()
        with app.connect() as conn:
            conn.execute(
                "INSERT INTO topics (course_id, title) VALUES (?, 'Limits')",
                (calc_id,))
        status, _, data = self._request("GET", "/setup?step=2")
        self.assertEqual(status, 200)
        self.assertIn("Limits", data)
        status, location, _ = self._request("POST", "/add-topic", {
            "course_id": str(calc_id), "title": "Derivatives",
            "next": "/setup?step=3", "wizard_step": "2"})
        self.assertEqual(status, 303)
        self.assertEqual(location, "/setup?step=3&saved=1")
        with app.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0], 2)

    def test_wizard_error_stays_in_wizard(self):
        # An empty course name on step 1: the error is shown on
        # step 1, not on the home page.
        status, _, data = self._request("POST", "/add-course", {
            "name": "", "next": "/setup?step=2", "wizard_step": "1"})
        self.assertEqual(status, 200)
        self.assertIn("שלב 1 מתוך 6", data)
        self.assertIn("שם הקורס חסר", data)
        # A bad slot on step 3: same — error on step 3.
        status, _, data = self._request("POST", "/add-slot", {
            "weekday": "7", "start_time": "25:00", "end_time": "26:00",
            "next": "/setup?step=4", "wizard_step": "3"})
        self.assertEqual(status, 200)
        self.assertIn("שלב 3 מתוך 6", data)

    def test_nav_links_every_section_page(self):
        # The slim nav reaches all seven sections now — topics and
        # busy days used to have no link from anywhere.
        _, _, data = self._request("GET", "/")
        for href in ("/exams", "/assignments", "/topics", "/courses",
                     "/slots", "/busy", "/routine"):
            self.assertIn(f'href="{href}"', data)

    def test_topic_importance_returns_to_topics_page(self):
        calc_id = self._course_id()
        with app.connect() as conn:
            conn.execute(
                "INSERT INTO topics (course_id, title) VALUES (?, 'Limits')",
                (calc_id,))
            topic_id = conn.execute(
                "SELECT id FROM topics").fetchone()["id"]
        _, _, data = self._request("GET", "/topics")
        self.assertIn("נתח סילבוס", data)
        self.assertIn("מאוד חשוב", data)
        self.assertIn("סמן כנושא כבד", data)
        # The everyday list links to the edit page now — the
        # direct delete-all link is gone from /topics.
        self.assertIn("עריכת נושאים", data)
        self.assertNotIn('href="/clear-topics', data)
        # The importance buttons on /topics carry next=/topics.
        self.assertIn(
            f"/topic-importance?id={topic_id}&importance=high"
            "&next=%2Ftopics", data)
        status, location, _ = self._request(
            "GET",
            f"/topic-importance?id={topic_id}&importance=high"
            "&next=%2Ftopics")
        self.assertEqual(status, 303)
        self.assertEqual(location, "/topics")


class TopicEditTest(unittest.TestCase):
    """Topic editing behind confirmation: delete-all and
    single-topic delete are two-step (confirm page + POST), the
    everyday list has no direct delete link, and topics can be
    renamed on the edit page."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        app.DB_PATH = os.path.join(self.tmp.name, "test.db")
        app.init_db()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _request(self, method, path, fields=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        body = urlencode(fields or {}, doseq=True) if fields else None
        headers = {"Content-Type": "application/x-www-form-urlencoded"} \
            if body else {}
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read().decode()
        location = resp.getheader("Location")
        status = resp.status
        conn.close()
        return status, location, data

    def _course_id(self, name="Calculus 2"):
        with app.connect() as conn:
            return conn.execute(
                "SELECT id FROM courses WHERE name = ?", (name,)
            ).fetchone()["id"]

    def _add_topics(self, course_id, *titles):
        with app.connect() as conn:
            ids = []
            for title in titles:
                cursor = conn.execute(
                    "INSERT INTO topics (course_id, title) VALUES (?, ?)",
                    (course_id, title),
                )
                ids.append(cursor.lastrowid)
            return ids

    def _add_week_slots(self):
        with app.connect() as conn:
            conn.execute(
                "INSERT INTO free_slots (weekday, start_time, end_time)"
                " VALUES (?, ?, ?), (?, ?, ?), (?, ?, ?), (?, ?, ?),"
                " (?, ?, ?), (?, ?, ?), (?, ?, ?)",
                tuple(v for d in range(7) for v in (d, "09:00", "12:00")))

    def _add_exam_with_plan(self, course_id, topic_ids):
        self._add_week_slots()
        status, _, _ = self._request("POST", "/add-exam", {
            "course_id": str(course_id), "kind": "quiz",
            "title": "Quiz",
            "exam_date": (date.today() + timedelta(days=14)).isoformat(),
            "learn_pct": "40", "review_pct": "20",
            "exercise_pct": "20", "simulation_pct": "20",
            "topic_id": [str(t) for t in topic_ids]})
        self.assertEqual(status, 303)

    def test_topics_page_links_to_edit_not_to_delete(self):
        course_id = self._course_id()
        self._add_topics(course_id, "Limits")
        _, _, data = self._request("GET", "/topics")
        self.assertIn(
            f'href="/edit-topics?course_id={course_id}"', data)
        self.assertNotIn('href="/clear-topics', data)
        self.assertNotIn('href="/delete-topic', data)

    def test_edit_page_lists_topics_with_rename_and_controls(self):
        course_id = self._course_id()
        self._add_topics(course_id, "Limits", "Derivatives")
        status, _, data = self._request(
            "GET", f"/edit-topics?course_id={course_id}")
        self.assertEqual(status, 200)
        self.assertIn("עריכת נושאים", data)
        self.assertIn("Calculus 2", data)
        self.assertIn("Limits", data)
        self.assertIn("Derivatives", data)
        self.assertIn("מאוד חשוב", data)
        self.assertIn("סמן כנושא כבד", data)
        self.assertIn("שמור שם", data)
        # Delete-all is on the edit page, but only as a link to
        # the confirmation page — never a direct delete.
        self.assertIn(f'href="/clear-topics?id={course_id}"', data)

    def test_edit_page_missing_course_redirects(self):
        status, location, _ = self._request(
            "GET", "/edit-topics?course_id=999")
        self.assertEqual(status, 303)
        self.assertEqual(location, "/topics")

    def test_clear_topics_get_is_confirm_only(self):
        course_id = self._course_id()
        topic_ids = self._add_topics(course_id, "Limits", "Derivatives")
        self._add_exam_with_plan(course_id, topic_ids)
        status, _, data = self._request(
            "GET", f"/clear-topics?id={course_id}")
        self.assertEqual(status, 200)
        self.assertIn("Calculus 2", data)
        self.assertIn("2 נושאים", data)
        self.assertIn("אי אפשר לבטל", data)
        with app.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0], 2)
            self.assertGreater(
                conn.execute(
                    "SELECT COUNT(*) FROM plan_sessions").fetchone()[0], 0)

    def test_clear_topics_post_deletes_only_that_course(self):
        calc_id = self._course_id()
        algo_id = self._course_id("Algorithms")
        calc_topics = self._add_topics(calc_id, "Limits", "Derivatives")
        algo_topics = self._add_topics(algo_id, "Graphs")
        self._add_exam_with_plan(calc_id, calc_topics)
        status, location, _ = self._request(
            "POST", "/confirm-clear-topics", {"course_id": str(calc_id)})
        self.assertEqual(status, 303)
        self.assertIn("topics", location)
        with app.connect() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM topics WHERE course_id = ?",
                    (calc_id,)).fetchone()[0], 0)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM topics WHERE course_id = ?",
                    (algo_id,)).fetchone()[0], 1)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM exam_topics").fetchone()[0], 0)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM plan_sessions").fetchone()[0], 0)
            # The course, its exam and the other course survive.
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM exams").fetchone()[0], 1)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM topics WHERE id = ?",
                    (algo_topics[0],)).fetchone()[0], 1)

    def test_delete_single_topic_confirm_flow(self):
        course_id = self._course_id()
        topic_ids = self._add_topics(course_id, "Limits", "Derivatives")
        self._add_exam_with_plan(course_id, topic_ids)
        # GET shows the confirm page and deletes nothing.
        status, _, data = self._request(
            "GET", f"/delete-topic?id={topic_ids[0]}")
        self.assertEqual(status, 200)
        self.assertIn("Limits", data)
        self.assertIn("אי אפשר לבטל", data)
        with app.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0], 2)
        # Cancelling is just not posting: topics survive. The POST
        # deletes only that topic, and the exam survives.
        status, location, _ = self._request(
            "POST", "/confirm-delete-topic",
            {"topic_id": str(topic_ids[0])})
        self.assertEqual(status, 303)
        self.assertIn("edit-topics", location)
        with app.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0], 1)
            self.assertEqual(
                conn.execute(
                    "SELECT title FROM topics").fetchone()[0], "Derivatives")
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM plan_sessions"
                    " WHERE topic_id = ?", (topic_ids[0],)).fetchone()[0], 0)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM exams").fetchone()[0], 1)

    def test_rename_topic_and_empty_rename_rejected(self):
        course_id = self._course_id()
        topic_ids = self._add_topics(course_id, "Limits")
        status, location, _ = self._request(
            "POST", "/save-topic-title",
            {"topic_id": str(topic_ids[0]), "title": "Limits II",
             "next": f"/edit-topics?course_id={course_id}"})
        self.assertEqual(status, 303)
        self.assertIn("edit-topics", location)
        with app.connect() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT title FROM topics WHERE id = ?",
                    (topic_ids[0],)).fetchone()[0], "Limits II")
        # Empty title: Hebrew error on the edit page, nothing saved.
        status, _, data = self._request(
            "POST", "/save-topic-title",
            {"topic_id": str(topic_ids[0]), "title": "   ",
             "next": f"/edit-topics?course_id={course_id}"})
        self.assertEqual(status, 200)
        self.assertIn("שם הנושא חסר", data)
        self.assertIn("עריכת נושאים", data)
        with app.connect() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT title FROM topics WHERE id = ?",
                    (topic_ids[0],)).fetchone()[0], "Limits II")

    def test_importance_from_edit_page_returns_to_edit_page(self):
        course_id = self._course_id()
        topic_ids = self._add_topics(course_id, "Limits")
        back = f"/edit-topics?course_id={course_id}"
        status, location, _ = self._request(
            "GET",
            f"/topic-importance?id={topic_ids[0]}&importance=high"
            f"&next={quote(back)}")
        self.assertEqual(status, 303)
        self.assertEqual(location, back)
        with app.connect() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT importance FROM topics WHERE id = ?",
                    (topic_ids[0],)).fetchone()[0], "high")


if __name__ == "__main__":
    unittest.main()
