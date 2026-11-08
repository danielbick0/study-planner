"""Study Planner — Step 4: importance, prep percentages, today-first
view, and date exceptions.
QA round 2 fixes (12 more): a bad free slot can no longer
crash the whole home page, an exam with no topics gets zero
sessions and a clear message instead of invented reviews,
creating anything without an existing course shows a Hebrew
error instead of crashing, messy syllabus pastes lose their junk
lines in the preview, a same-day exam warns that there is no
time to learn, the today view says the plan may shift after a
done-mark, a 0%-learn exam shows its real per-kind counts, tiny
assignment parts get a 15-minute minimum with a warning, bad
routine answers are rejected with an error instead of silently
wiping the saved routine, every save says "נשמר ✓", a quiz
turned into an exam keeps its narrowed scope with a clear note,
and duplicate/overlapping free slots are rejected or warned about.

A small full-stack study planner in pure Python (standard library only,
no dependencies to install): an SQLite database, a tiny web UI, and the
ability to add/list courses and assignments and mark assignments done.

Run it:
    python3 app.py
Then open http://localhost:8000 in a browser.

Step 1: add + list courses and assignments, SQLite persistence, simple
web UI.
Step 2: syllabus topics per course, weekly free slots, exams/quizzes
with a chosen topic scope, and a generated study plan.
Step 3: routine settings (session/break length, homework
morning/evening preference), breaks between learning sessions, and
assignments scheduled in whole blocks, in hours, per question/part.
Step 4 (this file): optional topic importance (most important topics
are scheduled first), per-exam prep percentages (learn / review /
exercise / simulation), a today-first plan view, and busy blocks —
one-off exceptions for a specific date. Later fixes, same stack:
the database file sits next to app.py (not in the terminal's
folder), every save redirects back to its own section instead of
the top of the page, and a 6-step first-run setup wizard
(/setup) walks through courses, syllabus, free slots, routine,
first exam/assignment, and a summary.
QA fixes (1-9): plans never double-book two exams, exercise and
review never land before a topic is learned, focus mode keeps one
subject per day for every kind of session, busy blocks move
existing sessions, exams and assignments can be edited and
deleted, a new exam gets its plan immediately, review is capped
at 2 sessions a day and each one names its topic, the exam page
groups sessions by date, and the today view marks both "now" and
"next".
Upgrades (Daniel approved all 12, 2026-11-06): the page works on
a phone (viewport), screen-reader landmarks and a skip link, the
session kind is always shown in words, one "needs attention" box
collects the warnings, every exam shows a countdown and progress,
the plan prints cleanly, one-click sample data (with a delete
that removes exactly the sample), the plan exports as .ics for
Google/Apple Calendar, busy hours can be pasted as a list,
reviews follow spaced repetition (1, 3 and 7 days after learning),
a per-exam emergency mode schedules only very-important topics
with no simulation, and a heavy topic gets two learning sessions.
Lead time (2026-11-06): each exam carries how many days before
it learning may start (1–30, default 14) — Daniel does not study
three months ahead for an exam, and a month is the maximum.
Deliberately NOT built: splitting app.py into files, stable
planning that never moves existing sessions, a planner.db backup
(Daniel declined it — the file sits in a convenient folder),
a public version with accounts, real PDF upload, and two-way
Google Calendar sync.
"""

import html
import os
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlencode, urlparse

# The database file sits next to the code file, no matter which folder
# you run it from: DB_PATH is an absolute path built from __file__,
# not the relative name "planner.db" (which used to create the file
# in whatever folder CMD happened to be in, so it kept "disappearing").
DB_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "planner.db")

# Fallback when the user has not answered the routine questions yet:
# 50-minute learning sessions with 10-minute breaks. The README says
# this plainly, and the plan page reminds the user to answer.
DEFAULT_SESSION_MINUTES = 50
DEFAULT_BREAK_MINUTES = 10
SESSION_MINUTES = DEFAULT_SESSION_MINUTES
# The short review session the day before the exam is 30 minutes.
REVIEW_MINUTES = 30
# At most 2 review sessions a day (QA fix 7): without a cap, one
# exam once produced 71 identical 30-minute reviews, 8 in a day.
MAX_REVIEW_PER_DAY = 2
# QA round 3, fix 8: at most 3 review sessions per topic — one
# topic with lots of free time used to collect 22 identical
# reviews (2 a day × 11 days). The time that is left stays
# honestly free; the per-kind counts on the exam page show it.
MAX_REVIEW_PER_TOPIC = 3
# Spaced repetition (upgrade 10): a topic is reviewed 1, 3 and 7
# days after it was learned — the gaps that help memory stick —
# instead of filling the review bucket from the end. A review
# whose day has no free window is skipped, never pretended.
SPACED_REVIEW_OFFSETS = (1, 3, 7)

# Lead time (new feature, 2026-11-06): how many days before the
# exam learning may start. Daniel does not study three months
# ahead for an exam, so each exam carries its own lead: 1–30 days,
# default 14 (his original example was "an exam in two weeks"),
# and 30 is the hard maximum — a month of learning before an exam,
# never more. The planner never schedules a session for that exam
# before exam_date - lead_days.
DEFAULT_LEAD_DAYS = 14
MAX_LEAD_DAYS = 30

# Hebrew names for session kinds, in one place: every session row
# shows its kind in words, never only as a colour (upgrade 3).
KIND_HE = {"learn": "למידה", "review": "חזרה", "exercise": "תרגול",
           "simulation": "סימולציה", "break": "הפסקה",
           "assignment": "שיעורי בית", "busy": "תפוס"}

# One-click sample data (upgrade 7): a clearly marked course, so a
# friend who gets the project sees a full plan at once instead of
# an empty page. The name is the marker — the sample course is
# found and deleted by this exact name, so real courses are never
# touched. Sample free slots carry sample=1 in the database, for
# the same reason: deleting the sample removes exactly its own
# slots, never Daniel's.
SAMPLE_COURSE_NAME = "קורס לדוגמה"
SAMPLE_TOPICS = [
    ("מבוא — נושא לדוגמה", "high"),
    ("נושא לדוגמה 2", "medium"),
    ("נושא לדוגמה 3", "medium"),
    ("נושא לדוגמה 4", "low"),
]
SAMPLE_SLOTS = [
    # Sunday, Monday, Tuesday 09:00–12:00 (weekday uses Python's
    # convention: 6 = Sunday, 0 = Monday, 1 = Tuesday).
    (6, "09:00", "12:00"),
    (0, "09:00", "12:00"),
    (1, "09:00", "12:00"),
]

SEED_COURSES = [
    # Daniel's real Semester courses (Ben-Gurion, 2nd year) — taken from
    # his calendar, so the planner is useful from the first run.
    ("Algorithms", "Mushyov (lecture) / Hozman (tutorial)"),
    ("Systems Programming", "Adler (lecture) / Bitan, Netai, Zlotnikov (tutorials)"),
    ("Calculus 2", "Landsman (lecture) / Luzon (tutorial)"),
]

# weekday: 0 = Monday, 1 = Tuesday, ... 6 = Sunday (same as date.weekday()).
WEEKDAYS_HE = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]

# The week in Israel starts on Sunday, so every weekday list the
# user sees runs Sunday first. The stored values stay Python's
# convention (0 = Monday) — only the display order changes, so a
# slot saved as Monday still means Monday; no migration, no data
# is rewritten.
WEEKDAY_DISPLAY_ORDER = (6, 0, 1, 2, 3, 4, 5)


def weekday_options_html():
    """<option> tags for the weekday dropdown, Sunday first."""
    return "".join(
        f'<option value="{i}">{WEEKDAYS_HE[i]}</option>'
        for i in WEEKDAY_DISPLAY_ORDER
    )


def sunday_first_key(slot):
    """Sort key so a slot list reads Sunday → Saturday."""
    try:
        return (int(slot["weekday"]) + 1) % 7
    except (TypeError, ValueError, KeyError):
        return 7


def weekday_name(value):
    """The Hebrew weekday name, defensively: a bad row stored
    before validation existed (weekday 7, -1, or "x") must never
    crash the page that lists it — it shows "יום לא תקין" instead
    (QA round 2, fix 1)."""
    try:
        index = int(value)
    except (TypeError, ValueError):
        return "יום לא תקין"
    if 0 <= index < len(WEEKDAYS_HE):
        return WEEKDAYS_HE[index]
    return "יום לא תקין"

SCHEMA = """
CREATE TABLE IF NOT EXISTS courses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    teacher TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL REFERENCES courses(id),
    title TEXT NOT NULL,
    due_date TEXT NOT NULL DEFAULT '',
    done INTEGER NOT NULL DEFAULT 0,
    estimated_hours REAL,
    num_parts INTEGER
);
CREATE TABLE IF NOT EXISTS user_prefs (
    id INTEGER PRIMARY KEY,
    session_minutes INTEGER,
    break_minutes INTEGER,
    homework_pref TEXT,
    study_style TEXT
);
CREATE TABLE IF NOT EXISTS assignment_blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    assignment_id INTEGER NOT NULL REFERENCES assignments(id),
    part_number INTEGER NOT NULL,
    part_label TEXT NOT NULL DEFAULT '',
    block_date TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL REFERENCES courses(id),
    title TEXT NOT NULL,
    learned INTEGER NOT NULL DEFAULT 0,
    importance TEXT,
    heavy INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS free_slots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    weekday INTEGER NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    sample INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS exams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL REFERENCES courses(id),
    kind TEXT NOT NULL DEFAULT 'exam',
    title TEXT NOT NULL,
    exam_date TEXT NOT NULL,
    learn_pct INTEGER NOT NULL DEFAULT 40,
    review_pct INTEGER NOT NULL DEFAULT 20,
    exercise_pct INTEGER NOT NULL DEFAULT 20,
    simulation_pct INTEGER NOT NULL DEFAULT 20,
    emergency INTEGER NOT NULL DEFAULT 0,
    lead_days INTEGER NOT NULL DEFAULT 14
);
CREATE TABLE IF NOT EXISTS busy_blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    block_date TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS exam_topics (
    exam_id INTEGER NOT NULL REFERENCES exams(id),
    topic_id INTEGER NOT NULL REFERENCES topics(id),
    PRIMARY KEY (exam_id, topic_id)
);
CREATE TABLE IF NOT EXISTS plan_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exam_id INTEGER NOT NULL REFERENCES exams(id),
    topic_id INTEGER REFERENCES topics(id),
    session_date TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'learn',
    done INTEGER NOT NULL DEFAULT 0
);
"""


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with connect() as conn:
        conn.executescript(SCHEMA)
        # Databases created by Step 1/2 lack the Step 3 columns, so add
        # them if they are missing (new databases already have them).
        columns = [r[1] for r in conn.execute(
            "PRAGMA table_info(assignments)")]
        if "estimated_hours" not in columns:
            conn.execute(
                "ALTER TABLE assignments ADD COLUMN estimated_hours REAL")
        if "num_parts" not in columns:
            conn.execute(
                "ALTER TABLE assignments ADD COLUMN num_parts INTEGER")
        # Databases created before Step 4 lack the importance and
        # percentage columns, so add them if they are missing.
        topic_columns = [r[1] for r in conn.execute(
            "PRAGMA table_info(topics)")]
        if "importance" not in topic_columns:
            conn.execute("ALTER TABLE topics ADD COLUMN importance TEXT")
        exam_columns = [r[1] for r in conn.execute(
            "PRAGMA table_info(exams)")]
        for column, default in (
            ("learn_pct", 40), ("review_pct", 20),
            ("exercise_pct", 20), ("simulation_pct", 20),
        ):
            if column not in exam_columns:
                conn.execute(
                    f"ALTER TABLE exams ADD COLUMN {column} INTEGER"
                    f" NOT NULL DEFAULT {default}")
        # Databases created before the focus/variety question lack
        # the study_style column, so add it if it is missing.
        pref_columns = [r[1] for r in conn.execute(
            "PRAGMA table_info(user_prefs)")]
        if "study_style" not in pref_columns:
            conn.execute(
                "ALTER TABLE user_prefs ADD COLUMN study_style TEXT")
        # Upgrade 12 (heavy topic) and upgrade 11 (emergency
        # mode) add one flag column each; upgrade 7 (sample data)
        # marks its own free slots, so deleting the sample removes
        # exactly those slots. Old databases get the columns added
        # here, new ones already have them from SCHEMA.
        if "heavy" not in topic_columns:
            conn.execute(
                "ALTER TABLE topics ADD COLUMN heavy INTEGER"
                " NOT NULL DEFAULT 0")
        if "emergency" not in exam_columns:
            conn.execute(
                "ALTER TABLE exams ADD COLUMN emergency INTEGER"
                " NOT NULL DEFAULT 0")
        # Lead time (new feature, 2026-11-06): old databases get
        # the column with the default (14 days), and their plans
        # are rebuilt once, right here, so the new rule applies to
        # existing exams too — a plan built months ahead shrinks
        # back into its window. Done sessions are kept (see
        # save_plan), so work already done is never lost. Every
        # later rebuild (creation, edit, done-cascade,
        # /generate-plan) respects the lead through
        # regenerate_exam_plan, so this one-time rebuild is the
        # only catch-up an old database needs.
        if "lead_days" not in exam_columns:
            conn.execute(
                "ALTER TABLE exams ADD COLUMN lead_days INTEGER"
                " NOT NULL DEFAULT 14")
            regenerate_all_exam_plans(conn)
        slot_columns = [r[1] for r in conn.execute(
            "PRAGMA table_info(free_slots)")]
        if "sample" not in slot_columns:
            conn.execute(
                "ALTER TABLE free_slots ADD COLUMN sample INTEGER"
                " NOT NULL DEFAULT 0")
        for name, teacher in SEED_COURSES:
            conn.execute(
                "INSERT OR IGNORE INTO courses (name, teacher) VALUES (?, ?)",
                (name, teacher),
            )


# ---------------------------------------------------------------------------
# Routine (user_prefs): one row, id = 1. Every field may be NULL, which
# means "the user has not answered yet" — the plan page says so, and
# planning falls back to 50-minute sessions and 10-minute breaks.
# ---------------------------------------------------------------------------

def get_prefs(conn):
    """The saved routine row, or None if the user never answered."""
    return conn.execute(
        "SELECT * FROM user_prefs WHERE id = 1"
    ).fetchone()


def save_prefs(conn, session_minutes, break_minutes, homework_pref,
               study_style=None):
    """Save the routine answers. Any of them may be None (unanswered).
    study_style is 'focus' or 'variety'; None means "not answered"
    and behaves as variety (see effective_study_style)."""
    conn.execute(
        """
        INSERT INTO user_prefs (id, session_minutes, break_minutes,
                                homework_pref, study_style)
        VALUES (1, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            session_minutes = excluded.session_minutes,
            break_minutes = excluded.break_minutes,
            homework_pref = excluded.homework_pref,
            study_style = excluded.study_style
        """,
        (session_minutes, break_minutes, homework_pref, study_style),
    )


def validate_routine(session_text, break_text, homework_pref,
                     study_style=None):
    """Validate the routine form before saving anything.

    Returns (values, error): values is a tuple of
    (session_minutes, break_minutes, homework_pref, study_style)
    with None for blank fields ("not answered"), and error is a
    Hebrew message or None. A blank field is fine — it just means
    unanswered. A filled field that is garbage or out of range is
    an error, and NOTHING is saved, so the previous saved routine
    stays exactly as it was (QA round 2, fix 9: the old code
    silently stored NULL for "abc" or a 999-minute break, and the
    user thought it had saved).
    Rules, in plain words: a learning session is 15–180 minutes,
    a break is 0–60 minutes, homework preference is morning /
    evening / any, study style is focus / variety.
    """
    errors = []
    session = None
    if session_text not in (None, ""):
        try:
            session = int(session_text)
        except (ValueError, TypeError):
            session = None
            errors.append("אורך מפגש חייב להיות מספר שלם בין 15 ל-180 דקות")
        else:
            if not 15 <= session <= 180:
                errors.append(
                    "אורך מפגש חייב להיות בין 15 ל-180 דקות")
                session = None
    break_ = None
    if break_text not in (None, ""):
        try:
            break_ = int(break_text)
        except (ValueError, TypeError):
            break_ = None
            errors.append("אורך הפסקה חייב להיות מספר שלם בין 0 ל-60 דקות")
        else:
            if not 0 <= break_ <= 60:
                errors.append(
                    "אורך הפסקה חייב להיות בין 0 ל-60 דקות")
                break_ = None
    pref = homework_pref or None
    if pref is not None and pref not in ("morning", "evening", "any"):
        errors.append("העדפת שיעורי בית חייבת להיות בוקר, ערב או אין העדפה")
        pref = None
    style = study_style or None
    if style is not None and style not in ("focus", "variety"):
        errors.append("סגנון הלמידה חייב להיות פוקוס או ערבוב")
        style = None
    if errors:
        return None, " — ".join(errors) + ". הרוטינה הקודמת נשמרה, כלום לא השתנה."
    return (session, break_, pref, style), None


def effective_session_minutes(conn):
    """Session length from prefs, or the 50-minute fallback."""
    prefs = get_prefs(conn)
    if prefs and prefs["session_minutes"]:
        return prefs["session_minutes"]
    return DEFAULT_SESSION_MINUTES


def effective_break_minutes(conn):
    """Break length from prefs, or the 10-minute fallback."""
    prefs = get_prefs(conn)
    if prefs and prefs["break_minutes"] is not None:
        return prefs["break_minutes"]
    return DEFAULT_BREAK_MINUTES


def effective_homework_pref(conn):
    """'morning', 'evening', or 'any' (the fallback)."""
    prefs = get_prefs(conn)
    if prefs and prefs["homework_pref"] in ("morning", "evening", "any"):
        return prefs["homework_pref"]
    return "any"


def effective_study_style(conn):
    """'focus' (one subject per day) or 'variety' (mix subjects in a
    day). Unanswered means variety — mixing is the default."""
    prefs = get_prefs(conn)
    if prefs and "study_style" in prefs.keys() \
            and prefs["study_style"] == "focus":
        return "focus"
    return "variety"


def validate_date(value, today=None):
    """Is this a real, sensible date? Returns the date, or None.

    One rule, in plain words: it must be a real calendar date (so
    30 February is rejected), in this year or next year (so year
    1212 is rejected), and today or in the future (so a past date
    is rejected). The app knows what year it is — the user never
    has to type a year by hand.
    """
    if today is None:
        today = date.today()
    try:
        parsed = date.fromisoformat((value or "").strip())
    except (ValueError, AttributeError):
        return None
    if parsed.year not in (today.year, today.year + 1):
        return None
    if parsed < today:
        return None
    return parsed


def prefs_are_set(conn):
    """True only when the user answered all three routine questions."""
    prefs = get_prefs(conn)
    return bool(
        prefs
        and prefs["session_minutes"]
        and prefs["break_minutes"] is not None
        and prefs["homework_pref"] in ("morning", "evening", "any")
    )


# ---------------------------------------------------------------------------
# Topic importance (Step 4): 'high' = מאוד חשוב, 'medium' = די חשוב,
# 'low' = לא מאוד חשוב, NULL = not set (treated as medium).
# The planning rule is: sort by importance first (high, then medium,
# then low), then by the topic's original order. When time runs out,
# low-importance topics are the ones left unscheduled first.
# ---------------------------------------------------------------------------

IMPORTANCE_RANK = {"high": 3, "medium": 2, "low": 1}
IMPORTANCE_HE = {"high": "מאוד חשוב", "medium": "די חשוב",
                 "low": "לא מאוד חשוב"}


def importance_rank(topic):
    """3 for high, 2 for medium or not set, 1 for low."""
    value = _topic_value(topic, "importance")
    return IMPORTANCE_RANK.get(value, 2)


def sort_topics_by_importance(topics):
    """Most important first; topics of equal importance keep their
    original order (Python's sort is stable, so this is free)."""
    return sorted(topics, key=lambda t: -importance_rank(t))


def validate_percentages(learn, review, exercise, simulation):
    """Prep percentages are valid only when each is 0–100 and the
    four of them add up to exactly 100."""
    values = (learn, review, exercise, simulation)
    if any(v is None or not isinstance(v, int) or v < 0 or v > 100
           for v in values):
        return False
    return sum(values) == 100


def parse_percentage(text, default):
    """One prep percentage from a form field: blank means the
    default (40/20/20/20), anything unparseable means None, so
    validate_percentages rejects it. Shared by exam creation and
    exam editing, so the two cannot drift apart (QA fix 5)."""
    try:
        return int(text) if text else default
    except (ValueError, TypeError):
        return None


def parse_lead_days(text, default=DEFAULT_LEAD_DAYS):
    """One exam's lead time from a form field: blank means the
    default (14 days), anything unparseable means None, so the
    caller rejects it — same pattern as parse_percentage, so
    creation and editing cannot drift apart."""
    try:
        return int(text) if text else default
    except (ValueError, TypeError):
        return None


def validate_lead_days(value):
    """A lead time is valid only when it is a whole number of days
    from 1 to 30. 0 would mean "never start learning", 31 would
    mean more than a month ahead, and both are rejected outright —
    never silently clamped, so the user sees the mistake."""
    return isinstance(value, int) and 1 <= value <= MAX_LEAD_DAYS


def exam_lead_days(exam):
    """The lead time of one exam row (Row or dict): the stored
    value, or the default when the row predates the column or the
    stored value is out of range — old rows keep working, and a
    bad value stored by hand never breaks the plan."""
    try:
        value = int(_topic_value(exam, "lead_days", DEFAULT_LEAD_DAYS))
    except (ValueError, TypeError):
        return DEFAULT_LEAD_DAYS
    return value if validate_lead_days(value) else DEFAULT_LEAD_DAYS


# ---------------------------------------------------------------------------
# Busy blocks (Step 4): one-off exceptions for a specific date.
# Weekly free slots are the usual pattern; a busy block says "this
# date is less free than usual" (e.g. leave the evening for friends).
# ---------------------------------------------------------------------------

def get_busy_blocks(conn):
    return conn.execute(
        "SELECT * FROM busy_blocks ORDER BY block_date, start_time"
    ).fetchall()


def busy_blocks_as_occupied(blocks):
    """Busy blocks in the shape plan code expects: each item needs
    session_date / start_time / end_time."""
    return [
        {"session_date": b["block_date"], "start_time": b["start_time"],
         "end_time": b["end_time"]}
        for b in blocks
    ]


# ---------------------------------------------------------------------------
# Assignment blocks: whole blocks of assignment work, stored separately
# from exam plan sessions so the two kinds of work never overlap.
# ---------------------------------------------------------------------------

def get_assignment_blocks(conn, assignment_id):
    return conn.execute(
        """
        SELECT * FROM assignment_blocks
        WHERE assignment_id = ? ORDER BY block_date, start_time
        """,
        (assignment_id,),
    ).fetchall()


def get_all_assignment_blocks(conn):
    return conn.execute(
        "SELECT * FROM assignment_blocks ORDER BY block_date, start_time"
    ).fetchall()


def save_assignment_blocks(conn, assignment_id, blocks):
    """Replace an assignment's old blocks with newly scheduled ones."""
    conn.execute(
        "DELETE FROM assignment_blocks WHERE assignment_id = ?",
        (assignment_id,),
    )
    for block in blocks:
        block_date = block["block_date"]
        if hasattr(block_date, "isoformat"):
            block_date = block_date.isoformat()
        conn.execute(
            """
            INSERT INTO assignment_blocks
                (assignment_id, part_number, part_label,
                 block_date, start_time, end_time)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                assignment_id,
                block["part_number"],
                block["part_label"],
                block_date,
                block["start_time"],
                block["end_time"],
            ),
        )


def clear_assignment_blocks(conn, assignment_id):
    conn.execute(
        "DELETE FROM assignment_blocks WHERE assignment_id = ?",
        (assignment_id,),
    )


def _blocks_as_occupied(blocks):
    """Assignment blocks in the shape plan code expects: each item
    needs session_date / start_time / end_time. Blocks store their
    date as block_date, so rename that one key."""
    return [
        {"session_date": b["block_date"], "start_time": b["start_time"],
         "end_time": b["end_time"]}
        for b in blocks
    ]


def reschedule_assignment(conn, assignment_id):
    """(Re)compute one assignment's whole blocks and save them.

    Occupied time = all exam-plan sessions EXCEPT not-done review
    sessions, plus the other assignments' blocks. A review session
    that is not done yet is filler — homework outranks it, so it
    yields its time to the assignment, and the exam plan is rebuilt
    afterwards (see reschedule_everything) so the review moves into
    whatever free time is left, or is honestly skipped. Done
    reviews stay occupied: done work is done. Returns the
    schedule_assignment result (blocks + unscheduled minutes).
    """
    row = conn.execute(
        "SELECT * FROM assignments WHERE id = ?", (assignment_id,)
    ).fetchone()
    if row is None:
        return {"blocks": [], "scheduled_minutes": 0,
                "unscheduled_minutes": 0}
    assignment = dict(row)
    slots = [dict(r) for r in get_free_slots(conn)]
    occupied = [
        {"session_date": s["session_date"], "start_time": s["start_time"],
         "end_time": s["end_time"]}
        for s in conn.execute(
            "SELECT * FROM plan_sessions"
            " WHERE kind != 'review' OR done = 1").fetchall()
    ]
    occupied += _blocks_as_occupied([
        b for b in get_all_assignment_blocks(conn)
        if b["assignment_id"] != assignment_id
    ])
    # Busy blocks (date exceptions) are occupied too: no assignment
    # block may overlap one.
    occupied += busy_blocks_as_occupied(get_busy_blocks(conn))
    result = schedule_assignment(
        assignment, slots, occupied,
        homework_pref=effective_homework_pref(conn),
    )
    save_assignment_blocks(conn, assignment_id, result["blocks"])
    return result


def exams_with_plans(conn):
    """Ids of the exams that already have a plan, earliest exam
    first — so when plans are rebuilt, the earliest exam keeps
    its time and later exams fit around it (QA fix 1)."""
    return [r["id"] for r in conn.execute(
        "SELECT DISTINCT e.id FROM exams e"
        " JOIN plan_sessions ps ON ps.exam_id = e.id"
        " ORDER BY e.exam_date, e.id")]


def regenerate_all_exam_plans(conn):
    """Rebuild every exam's plan, earliest exam first (QA fixes 1
    and 6). Used when a new exam is created: the new exam gets its
    plan immediately, and the other exams are rebuilt in date
    order, so the earliest exam keeps its time."""
    for row in conn.execute(
            "SELECT id FROM exams ORDER BY exam_date, id"):
        regenerate_exam_plan(conn, row["id"])


def regenerate_existing_plans(conn):
    """Rebuild every exam that already has a plan, earliest exam
    first — the second half of the cascade (see
    reschedule_everything): after assignments move, the plans
    reflow into the time that is left, so a review that yielded
    its slot to homework lands elsewhere or is honestly skipped."""
    for exam_id in exams_with_plans(conn):
        regenerate_exam_plan(conn, exam_id)


def reschedule_everything(conn):
    """Reschedule every still-open assignment and regenerate every
    exam that has a plan — the cascade that runs whenever time
    frees up or gets blocked (an assignment marked done, a busy
    block added, an exam or assignment deleted). Open assignments
    are rescheduled first, in due-date order (the most urgent
    first — a sooner-due assignment created later still outranks
    a later-due one created earlier), then exams, so freed time
    is reused right away instead of sitting empty."""
    for row in conn.execute(
            "SELECT id FROM assignments WHERE done = 0"
            " ORDER BY (due_date IS NULL), due_date, id"):
        reschedule_assignment(conn, row["id"])
    regenerate_existing_plans(conn)


def mark_assignment_done(conn, assignment_id):
    """Mark done and clear its blocks: done work needs no time slots.

    Finishing early frees time, so the rest is reshuffled right
    away: every still-open assignment is rescheduled into the freed
    windows first, then every exam that already has a plan is
    regenerated, so its sessions can move into the freed time too.
    (A plan that is regenerated is rebuilt from today; topics
    already learned stay learned, so they never come back.)
    """
    conn.execute(
        "UPDATE assignments SET done = 1 WHERE id = ?", (assignment_id,)
    )
    clear_assignment_blocks(conn, assignment_id)
    reschedule_everything(conn)


def mark_assignment_undone(conn, assignment_id):
    """Undo a done-mark: the assignment is open again and its blocks
    are scheduled again, fitting around the other work (the same
    reschedule used everywhere else). If the due date has passed by
    now, no block fits and the assignment honestly reports its
    minutes as unscheduled instead of silently dropping them. The
    exam plans are rebuilt afterwards, so a review that has to
    yield its slot to the reopened assignment moves elsewhere."""
    conn.execute(
        "UPDATE assignments SET done = 0 WHERE id = ?", (assignment_id,)
    )
    result = reschedule_assignment(conn, assignment_id)
    regenerate_existing_plans(conn)
    return result


def mark_session_undone(conn, session_id):
    """Undo a done-mark on one plan session: the session counts as
    not done again. A learning session also un-learns its topic —
    but only when no other done learning session covers that topic,
    so a topic ticked learned on its own (or by another session)
    is not undone by accident. Un-learning matters because plans
    skip learned topics: without it, the next regeneration would
    delete the undone session instead of bringing it back."""
    conn.execute(
        "UPDATE plan_sessions SET done = 0 WHERE id = ?", (session_id,)
    )
    conn.execute(
        """
        UPDATE topics SET learned = 0 WHERE id IN
        (SELECT topic_id FROM plan_sessions WHERE id = ? AND kind = 'learn')
        AND NOT EXISTS (
            SELECT 1 FROM plan_sessions
            WHERE topic_id = topics.id AND kind = 'learn' AND done = 1)
        """,
        (session_id,),
    )


def delete_exam(conn, exam_id):
    """Delete an exam and everything that belongs to it: its plan
    sessions and its topic scope (QA fix 5)."""
    conn.execute("DELETE FROM plan_sessions WHERE exam_id = ?", (exam_id,))
    conn.execute("DELETE FROM exam_topics WHERE exam_id = ?", (exam_id,))
    conn.execute("DELETE FROM exams WHERE id = ?", (exam_id,))


def delete_assignment(conn, assignment_id):
    """Delete an assignment and its scheduled blocks (QA fix 5)."""
    clear_assignment_blocks(conn, assignment_id)
    conn.execute("DELETE FROM assignments WHERE id = ?", (assignment_id,))


def delete_course(conn, course_id):
    """Delete a course and everything that belongs to it: its
    assignments and their blocks, its topics, and its exams (with
    their plan sessions and topic scope)."""
    assignment_ids = [
        r["id"] for r in conn.execute(
            "SELECT id FROM assignments WHERE course_id = ?", (course_id,)
        )
    ]
    for assignment_id in assignment_ids:
        clear_assignment_blocks(conn, assignment_id)
    exam_ids = [
        r["id"] for r in conn.execute(
            "SELECT id FROM exams WHERE course_id = ?", (course_id,)
        )
    ]
    for exam_id in exam_ids:
        conn.execute("DELETE FROM plan_sessions WHERE exam_id = ?",
                     (exam_id,))
        conn.execute("DELETE FROM exam_topics WHERE exam_id = ?",
                     (exam_id,))
    conn.execute("DELETE FROM assignments WHERE course_id = ?",
                 (course_id,))
    conn.execute("DELETE FROM topics WHERE course_id = ?", (course_id,))
    conn.execute("DELETE FROM exams WHERE course_id = ?", (course_id,))
    conn.execute("DELETE FROM courses WHERE id = ?", (course_id,))


def clear_course_topics(conn, course_id):
    """Delete all topics of one course — the recovery after a bad
    syllabus paste (a whole BGU page once produced 261 junk
    topics): remove them from exam scopes and plan sessions, then
    rebuild the affected plans, so nothing keeps pointing at
    deleted topics. The course itself, its exams and its
    assignments stay; the exams simply have an empty scope until
    the topics are re-pasted."""
    topic_ids = [
        r["id"] for r in conn.execute(
            "SELECT id FROM topics WHERE course_id = ?", (course_id,))
    ]
    if not topic_ids:
        return
    placeholders = ",".join("?" for _ in topic_ids)
    conn.execute(
        f"DELETE FROM plan_sessions WHERE topic_id IN ({placeholders})",
        topic_ids)
    conn.execute(
        f"DELETE FROM exam_topics WHERE topic_id IN ({placeholders})",
        topic_ids)
    conn.execute("DELETE FROM topics WHERE course_id = ?", (course_id,))
    reschedule_everything(conn)


def delete_topic(conn, topic_id):
    """Delete ONE topic — the single-topic version of
    clear_course_topics: remove it from exam scopes and plan
    sessions, then rebuild the affected plans, so nothing keeps
    pointing at the deleted topic. The course, its exams and its
    assignments stay. Returns the course id, or None when the
    topic does not exist."""
    row = conn.execute(
        "SELECT course_id FROM topics WHERE id = ?", (topic_id,)).fetchone()
    if row is None:
        return None
    conn.execute(
        "DELETE FROM plan_sessions WHERE topic_id = ?", (topic_id,))
    conn.execute(
        "DELETE FROM exam_topics WHERE topic_id = ?", (topic_id,))
    conn.execute("DELETE FROM topics WHERE id = ?", (topic_id,))
    reschedule_everything(conn)
    return row["course_id"]


def sample_data_exists(conn):
    """True when the sample course is in the database."""
    return conn.execute(
        "SELECT 1 FROM courses WHERE name = ?",
        (SAMPLE_COURSE_NAME,)).fetchone() is not None


def create_sample_data(conn):
    """One-click sample data (upgrade 7): a clearly marked course
    with a few topics, three free slots and one exam in two weeks,
    with a plan built for it — so a friend who gets the project
    sees a full plan at once instead of an empty page. Returns
    False when the sample already exists (nothing is duplicated).
    The sample exam's plan is built, but only while the sample
    exists: deleting it rebuilds everything, so Daniel's real
    plans come back exactly as they were."""
    if sample_data_exists(conn):
        return False
    cursor = conn.execute(
        "INSERT INTO courses (name, teacher) VALUES (?, ?)",
        (SAMPLE_COURSE_NAME, "דוגמה"),
    )
    course_id = cursor.lastrowid
    for title, importance in SAMPLE_TOPICS:
        conn.execute(
            "INSERT INTO topics (course_id, title, importance)"
            " VALUES (?, ?, ?)",
            (course_id, title, importance),
        )
    for weekday, start, end in SAMPLE_SLOTS:
        conn.execute(
            "INSERT INTO free_slots (weekday, start_time, end_time,"
            " sample) VALUES (?, ?, ?, 1)",
            (weekday, start, end),
        )
    exam_date = (date.today() + timedelta(days=14)).isoformat()
    cursor = conn.execute(
        "INSERT INTO exams (course_id, kind, title, exam_date)"
        " VALUES (?, 'exam', 'מבחן לדוגמה', ?)",
        (course_id, exam_date),
    )
    exam_id = cursor.lastrowid
    for topic_id in conn.execute(
            "SELECT id FROM topics WHERE course_id = ? ORDER BY id",
            (course_id,)).fetchall():
        conn.execute(
            "INSERT OR IGNORE INTO exam_topics (exam_id, topic_id)"
            " VALUES (?, ?)",
            (exam_id, topic_id["id"]),
        )
    regenerate_exam_plan(conn, exam_id)
    return True


def delete_sample_data(conn):
    """Remove exactly the sample data (upgrade 7) and nothing
    else: the sample course (with its topics, exam and plan
    sessions — see delete_course) and the free slots marked
    sample=1. Daniel's real courses, topics and slots are never
    touched — they carry no sample marker and a different name.
    Returns False when there is no sample data to delete."""
    row = conn.execute(
        "SELECT id FROM courses WHERE name = ?",
        (SAMPLE_COURSE_NAME,)).fetchone()
    if row is None:
        return False
    delete_course(conn, row["id"])
    conn.execute("DELETE FROM free_slots WHERE sample = 1")
    reschedule_everything(conn)
    return True


def get_courses(conn):
    return conn.execute("SELECT * FROM courses ORDER BY name").fetchall()


def get_assignments(conn):
    return conn.execute(
        """
        SELECT a.*, c.name AS course_name
        FROM assignments a JOIN courses c ON c.id = a.course_id
        ORDER BY a.done, a.due_date, a.title
        """
    ).fetchall()


def get_topics(conn):
    """All topics, with their course name, in course then id order."""
    return conn.execute(
        """
        SELECT t.*, c.name AS course_name
        FROM topics t JOIN courses c ON c.id = t.course_id
        ORDER BY c.name, t.id
        """
    ).fetchall()


def get_free_slots(conn):
    return conn.execute(
        "SELECT * FROM free_slots ORDER BY weekday, start_time"
    ).fetchall()


def get_exams(conn):
    # QA round 3, fix 9: LEFT JOIN, so an exam whose course no
    # longer exists (only possible through direct database edits)
    # still shows up — as "קורס שנמחק" — instead of silently
    # disappearing from every list.
    return conn.execute(
        """
        SELECT e.*, COALESCE(c.name, 'קורס שנמחק') AS course_name
        FROM exams e LEFT JOIN courses c ON c.id = e.course_id
        ORDER BY e.exam_date
        """
    ).fetchall()


def get_scoped_topics(conn, exam):
    """Topics an exam/quiz covers.

    If the user picked topics, use exactly those. Otherwise an exam
    covers the whole course, and a quiz covers nothing (a quiz must
    have its scope picked — it never covers the whole course).
    """
    chosen = conn.execute(
        """
        SELECT t.* FROM exam_topics et
        JOIN topics t ON t.id = et.topic_id
        WHERE et.exam_id = ? ORDER BY t.id
        """,
        (exam["id"],),
    ).fetchall()
    if chosen:
        return chosen
    if exam["kind"] == "exam":
        return conn.execute(
            "SELECT * FROM topics WHERE course_id = ? ORDER BY id",
            (exam["course_id"],),
        ).fetchall()
    return []


def resolve_exam_scope(conn, kind, course_id, picked):
    """Which topic ids to store for a new exam/quiz.

    Returns a list of topic id strings, or None when the exam must
    NOT be saved: a quiz with no topics picked has no scope at all,
    so saving it would build a plan from nothing. An exam with no
    boxes ticked keeps the old behaviour — scope is the whole course.
    Picked topics are returned exactly as picked, in picked order.
    """
    picked = [str(t) for t in (picked or []) if str(t).strip()]
    if picked:
        return picked
    if kind == "quiz":
        return None
    return [
        str(r["id"]) for r in conn.execute(
            "SELECT id FROM topics WHERE course_id = ? ORDER BY id",
            (course_id,),
        )
    ]


def get_plan_sessions(conn, exam_id):
    return conn.execute(
        """
        SELECT ps.*, t.title AS topic_title
        FROM plan_sessions ps LEFT JOIN topics t ON t.id = ps.topic_id
        WHERE ps.exam_id = ?
        ORDER BY ps.session_date, ps.start_time
        """,
        (exam_id,),
    ).fetchall()


# ---------------------------------------------------------------------------
# Syllabus parsing: one line = one topic.
# ---------------------------------------------------------------------------

# Lines in a pasted syllabus that are clearly not topics (QA
# round 2, fix 4): a real paste produced 12 "topics", 7 of them
# junk — the course title, the lecturer, office hours, grading,
# a book, a midterm date. These are dropped in the preview, which
# is still shown for approval/editing, so nothing is silently
# lost: the user sees exactly what will be saved and can add
# anything back. "Week N: <topic>" keeps only the text after the
# colon — the week number is not a topic.
JUNK_SYLLABUS_MARKERS = (
    "סילבוס", "מרצה", "grading", "office hours", "שעות קבלה",
    "ספר:", "lecturer", "instructor", "ציונים",
    # Admin headings of a Ben-Gurion course page (the live bug:
    # Daniel pasted a whole BGU page and almost every line became
    # a topic). These are headings, not things to study.
    "נגישות", "פרטי הקורס", "מבנה הקורס", "קורסים דומים",
    "צוות הקורס", "תיאור הקורס", "תפוקות למידה", "אופן הערכה",
    "דרישות הקורס", "ביבליוגרפיה", "מקורות לשיעור",
    "מספר הקורס", "שנה אקדמית", "שנה לועזית", "רמת תואר",
    "ציון עובר", "נקודות זכות", "ects", "בחירת קבוצה",
    "אופן לימוד", "מקום לימוד", "זמני לימוד", "דילוג לתוכן",
    "מוסד", "תשפ",
)
# A line that is exactly one of these words is a table heading or
# a page label, never a topic.
_EXACT_JUNK_LINES = {
    "תאריך", "נושא", "מקורות לשיעור", "כיתה", "שעור", "תרגיל",
    "היברידי", "pdf", "הכל",
}
# A staff line starts with a title (both apostrophe spellings
# occur in pasted pages: ' and ׳); a time, a bare number, a course
# code, an email, a room, a group, or a weekday on its own is
# admin data, not a topic. Real topics may contain numbers
# ("R^n"), so these rules only fire when the whole line is that
# shape — nothing else is dropped on their account.
_TIME_ONLY_RE = re.compile(r"^\d{1,2}:\d{2}\s*([-–]\s*\d{1,2}:\d{2})?$")
_NUMBER_ONLY_RE = re.compile(r"^[\d.\-–]+$")
_COURSE_CODE_RE = re.compile(r"^\d{2,4}\.\d+\.\d+")
_ROOM_RE = re.compile(r"^\[\d+\]")
_WEEKDAY_LINE_RE = re.compile(
    r"^יום (ראשון|שני|שלישי|רביעי|חמישי|שישי|שבת)$")
_STAFF_TITLES = ("פרופ׳", "פרופ'", "ד״ר", 'ד"ר', "מר ", "גב׳ ",
                 "גב' ")
# A source line in the weekly table: only a book reference, e.g.
# "Multivariable Calculus with Applications Lax-Terrell. פרק 4"
# or a bare "פרק 6" / "פרקים 4,6".
_CHAPTER_ONLY_RE = re.compile(r"^פרק(ים)?\s*[\d,\s\-–]+$")
_DATE_RE = re.compile(r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}"
                      r"|\d{4}-\d{2}-\d{2}")
_WEEK_RE = re.compile(r"(?i)^week\s+\d+\s*:\s*(.*)$")


def _is_junk_syllabus_line(line):
    """True when a syllabus line is clearly not a topic: it names
    the course/lecturer/book, gives grading or office hours, or
    contains a date (an exam date is not something to study)."""
    lowered = line.lower()
    if any(marker in lowered for marker in JUNK_SYLLABUS_MARKERS):
        return True
    if lowered in _EXACT_JUNK_LINES:
        return True
    if "@" in line:
        return True
    if _TIME_ONLY_RE.match(line):
        return True
    if _NUMBER_ONLY_RE.match(line):
        return True
    if _COURSE_CODE_RE.match(line):
        return True
    if _ROOM_RE.match(line):
        return True
    if _WEEKDAY_LINE_RE.match(line):
        return True
    if line.startswith(_STAFF_TITLES):
        return True
    if line.startswith(("חדר ", "קבוצה ")):
        return True
    if "lax-terrell" in lowered:
        return True
    if _CHAPTER_ONLY_RE.match(line):
        return True
    return bool(_DATE_RE.search(line))


def _topics_section_lines(text):
    """Lines of the weekly-topics section, when the paste has one.

    A Ben-Gurion course page has a "נושאי הקורס ..." heading and
    a "ביבליוגרפיה" heading after it; everything outside that
    range is course admin, staff, grading and bibliography — not
    topics. When there is no such heading, the whole text is the
    list, so a simple pasted topic list still works exactly as
    before."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if "נושאי הקורס" in line:
            start = i + 1
            break
    if start is None:
        return lines
    end = len(lines)
    for j in range(start, len(lines)):
        if "ביבליוגרפיה" in lines[j]:
            end = j
            break
    return lines[start:end]


def parse_syllabus(text):
    """Turn pasted syllabus text into a list of topic titles.

    Rule (simple and honest): when the paste has a weekly-topics
    section, parse only that section (see
    _topics_section_lines); otherwise split the whole text into
    lines, drop empty lines, drop lines that are clearly not
    topics (see _is_junk_syllabus_line), and treat each remaining
    line as one topic. Leading numbering or a bullet ("1.", "2)",
    "- ") is stripped, and a "Week N: <topic>" line keeps only
    the text after the colon. Nothing is guessed or invented.
    """
    topics = []
    for line in _topics_section_lines(text):
        title = _clean_topic_line(line)
        if not title:
            continue
        if _is_junk_syllabus_line(title):
            continue
        week = _WEEK_RE.match(title)
        if week:
            title = week.group(1).strip()
            if not title:
                continue
        topics.append(title)
    return topics


def _clean_topic_line(line):
    line = line.strip()
    # Strip a leading bullet: "- ", "* ", "• ", "– ".
    while line and line[0] in "-*•–":
        line = line[1:].strip()
    # Strip leading numbering: "1.", "2)", "3 - ".
    digits = 0
    while digits < len(line) and line[digits].isdigit():
        digits += 1
    if digits and digits < len(line) and line[digits] in ".)":
        line = line[digits + 1:].strip()
    elif digits and line[digits:digits + 3].strip().startswith("-"):
        line = line[digits:].lstrip("- ").strip()
    return line


# ---------------------------------------------------------------------------
# Plan generation: a pure function, no database, easy to test.
# ---------------------------------------------------------------------------

def _to_minutes(time_text):
    hours, minutes = time_text.split(":")
    return int(hours) * 60 + int(minutes)


def _to_time(total_minutes):
    return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def _topic_value(topic, key, default=None):
    # Works for both plain dicts (tests) and sqlite3.Row objects (app).
    try:
        return topic[key]
    except (KeyError, IndexError, TypeError):
        return default


def _occupied_by_date(occupied):
    """Group occupied intervals by date: {date: [(start, end), ...]}.

    `occupied` is a list of dicts/rows with session_date (a date or an
    ISO string), start_time and end_time, e.g. assignment blocks.
    """
    by_date = {}
    for item in occupied or []:
        # QA round 3, fix 1: one corrupt row (a bad time or date
        # stored before validation existed) is skipped, never
        # crashes the plan — the page that lists it is the page
        # that would crash, so there is no way to fix it from
        # the UI otherwise.
        try:
            item_date = item["session_date"]
            if isinstance(item_date, str):
                item_date = date.fromisoformat(item_date)
            start = _to_minutes(item["start_time"])
            end = _to_minutes(item["end_time"])
        except (ValueError, AttributeError, TypeError, KeyError):
            continue
        by_date.setdefault(item_date, []).append((start, end))
    for intervals in by_date.values():
        intervals.sort()
    return by_date


def _free_minutes(windows, occupied_by_date):
    """Total free minutes inside the windows, after removing
    occupied intervals (clamped to each window, overlaps merged)."""
    total = 0
    for window in windows:
        busy = sorted(
            (max(start, window["start"]), min(end, window["end"]))
            for start, end in occupied_by_date.get(window["date"], [])
        )
        busy = [(s, e) for s, e in busy if e > s]
        cursor = window["start"]
        for start, end in busy:
            if start > cursor:
                total += start - cursor
            cursor = max(cursor, end)
        if cursor < window["end"]:
            total += window["end"] - cursor
    return total


def _next_free_start(cursor, duration, occupied_today, window_end):
    """Earliest start at or after `cursor` where a block of `duration`
    minutes fits before `window_end` without overlapping an occupied
    interval. Returns None if it does not fit in this window."""
    while cursor + duration <= window_end:
        clash = None
        for start, end in occupied_today:
            if start < cursor + duration and cursor < end:
                clash = end
                break
        if clash is None:
            return cursor
        cursor = clash
    return None


def _overlaps(start, end, occupied_today):
    return any(s < end and start < e for s, e in occupied_today)


def generate_plan(topics, free_slots, exam_date, session_minutes=None,
                  break_minutes=None, occupied=None, learn_pct=40,
                  review_pct=20, exercise_pct=20, simulation_pct=20,
                  busy_blocks=None, study_style="variety",
                  focus_blocked_dates=None, emergency=False,
                  lead_days=DEFAULT_LEAD_DAYS):
    """Build a study plan for one exam/quiz.

    How it works, in plain words:
    1. Take the scoped topics in order, skipping ones already learned,
       sorted by importance first (high, then medium, then low — a
       topic with no importance set counts as medium), then by their
       original order.
    2. Expand the weekly free slots into real windows for each day
       from the plan start up to the day before the exam. The plan
       start is the later of today and exam_date - lead_days: nobody
       studies three months ahead for an exam, and 30 days is the
       hard maximum (see lead_days). Busy blocks and occupied
       intervals (e.g. assignment blocks) are removed from those
       windows: no session may overlap one.
    3. Count the total free minutes left, and split them into four
       buckets by the prep percentages: learn, review, exercise and
       simulation. Each bucket is a time budget — a kind of session
       may use up to its bucket, never more.
    4. Fill the buckets: learning sessions first, in time order
       (one topic per session, breaks between learning sessions
       only — a heavy topic gets two learning sessions instead of
       one, upgrade 12); then one simulation session near the exam
       (up to its bucket, at most 120 minutes); then review
       sessions, by spaced repetition (upgrade 10): a topic is
       reviewed 1, 3 and 7 days after it was learned, when a free
       window fits on that day — at most 2 per day and at most 3
       per topic, each one attached to a specific topic; then
       exercise sessions, one per topic. A session must fit
       entirely inside one free window and must not overlap an
       occupied interval. Exercise and review for a topic are
       never placed before that topic's first learning session —
       you cannot practise what you have not learned yet.
       In emergency mode (upgrade 11) only "very important"
       topics are scheduled at all, there is no simulation, and
       the simulation bucket's minutes move to learning, so the
       remaining work is compressed into the available time.
    5. Topics that did not fit are returned in "unscheduled" — they
       are never silently dropped. If learn_pct is 0 and topics are
       still unlearned, the reason is exactly "learning share is 0%"
       — never "not enough free time", because the learn bucket was
       zero by choice, not because time ran out.
    6. Focus vs variety (the routine's study-style question):
       in "variety" mode every day is used as it comes. In "focus"
       mode, NO session of any kind is placed on a day listed in
       focus_blocked_dates — those are days where another exam
       already has a session, so one day stays one subject for
       learning, review, exercise and simulation alike.

    Session/break lengths come from the user's routine when set, and
    fall back to 50 minutes / 10 minutes when not (None means
    "not set", so the defaults above apply).
    """
    if session_minutes is None:
        session_minutes = DEFAULT_SESSION_MINUTES
    if break_minutes is None:
        break_minutes = DEFAULT_BREAK_MINUTES
    # Focus-blocked days may arrive as ISO strings (from the DB);
    # normalise them to dates, and ignore anything unparseable.
    focus_blocked = set()
    for blocked in (focus_blocked_dates or []):
        try:
            focus_blocked.add(
                blocked if isinstance(blocked, date)
                else date.fromisoformat(str(blocked)))
        except ValueError:
            continue
    if isinstance(exam_date, str):
        exam_date = date.fromisoformat(exam_date)
    today = date.today()
    last_day = exam_date - timedelta(days=1)

    # Lead time (new feature): learning for this exam starts at
    # most `lead_days` before it — never three months ahead. The
    # effective plan start is the later of today and
    # exam_date - lead_days, so a near exam (its window already
    # open) starts today, and a far exam waits. Every kind of
    # session is placed only inside the windows built below, so
    # reviews (+1/+3/+7 days from a learn date that is itself
    # inside the window), exercise and the simulation all stay
    # inside the window naturally — nothing is scheduled before
    # the window opens.
    if not validate_lead_days(lead_days):
        lead_days = DEFAULT_LEAD_DAYS
    start_day = max(today, exam_date - timedelta(days=lead_days))

    # Emergency mode (upgrade 11): only "very important" topics
    # are scheduled at all. The filter applies to the whole plan
    # — learning, exercise and review all derive from `topics` —
    # and the exam page says so plainly, so the missing topics
    # are a choice, not a silent drop.
    if emergency:
        topics = [t for t in topics
                  if _topic_value(t, "importance") == "high"]

    # Unlearned topics, most important first (see importance_rank).
    remaining = sort_topics_by_importance(
        [t for t in topics if not _topic_value(t, "learned", 0)]
    )

    def _result(sessions, unscheduled):
        """The plan, plus the exact unscheduled reason and the
        per-kind session counts the plan page shows."""
        reason = None
        if unscheduled:
            if learn_pct == 0:
                reason = "learning share is 0%"
            else:
                reason = "not enough free time"
        counts = {"learn": 0, "review": 0, "exercise": 0,
                  "simulation": 0, "break": 0}
        for session in sessions:
            counts[session["kind"]] = counts.get(session["kind"], 0) + 1
        return {"sessions": sessions, "unscheduled": unscheduled,
                "unscheduled_reason": reason, "session_counts": counts,
                "empty_scope": False}

    # No days left to study in: nothing can be scheduled.
    if last_day < today:
        return _result([], remaining)

    # QA round 2, fix 2: no topics in scope at all means no plan —
    # not 22 invented review sessions and a simulation. An exam on
    # a course with no topics gets zero sessions, and the page
    # shows "אין נושאים בקורס..." instead.
    if not topics:
        result = _result([], [])
        result["empty_scope"] = True
        return result

    # Busy blocks are occupied time, just like assignment blocks.
    all_occupied = list(occupied or []) + \
        busy_blocks_as_occupied(busy_blocks or [])
    occupied_by_date = _occupied_by_date(all_occupied)

    # Expand weekly slots into concrete windows, in time order —
    # from the plan start (today, or exam_date - lead_days when
    # the exam is far away), never from before the window opens.
    windows = []  # each window: {"date", "start", "end", "cursor"}
    day = start_day
    while day <= last_day:
        for slot in free_slots:
            if slot["weekday"] == day.weekday():
                # Defensive: a bad slot stored before validation
                # existed (e.g. "25:00") is skipped, never crashes
                # the plan (QA round 2, fix 1).
                try:
                    start = _to_minutes(slot["start_time"])
                    end = _to_minutes(slot["end_time"])
                except (ValueError, AttributeError):
                    continue
                if end > start:
                    windows.append(
                        {"date": day, "start": start, "end": end, "cursor": start}
                    )
        day += timedelta(days=1)
    windows.sort(key=lambda w: (w["date"], w["start"]))
    # Focus mode blocks whole days, for every kind of session:
    # a day where another exam already has a session is not used
    # at all (QA fix 3). In variety mode every window is usable.
    usable_windows = windows
    if study_style == "focus":
        usable_windows = [w for w in windows
                          if w["date"] not in focus_blocked]

    # Total free minutes, then the four time budgets (buckets).
    total_free = _free_minutes(windows, occupied_by_date)
    buckets = {
        "learn": total_free * learn_pct // 100,
        "review": total_free * review_pct // 100,
        "exercise": total_free * exercise_pct // 100,
        "simulation": total_free * simulation_pct // 100,
    }
    if emergency:
        # No simulation in emergency mode: its minutes move to
        # learning, so the remaining work is compressed into the
        # available time (upgrade 11).
        buckets["learn"] += buckets["simulation"]
        buckets["simulation"] = 0

    # Sessions placed in this call, per date: later kinds must not
    # overlap them, so they count as occupied too.
    placed_by_date = {}

    def _occupied_today(day):
        return occupied_by_date.get(day, []) + placed_by_date.get(day, [])

    def _place(duration, window_list):
        """Earliest fitting slot of `duration` minutes in the given
        windows (in their order), or None. Records the placement so
        later sessions cannot overlap it."""
        for window in window_list:
            start = _next_free_start(
                window["start"], duration,
                _occupied_today(window["date"]), window["end"],
            )
            if start is not None:
                placed_by_date.setdefault(window["date"], []).append(
                    (start, start + duration))
                return window["date"], start, start + duration
        return None

    sessions = []
    learn_minutes = 0
    # Topics that got at least one learning session in this plan,
    # and how many learning sessions each heavy topic got: a heavy
    # topic that got its first session but not its second is
    # learned, not unscheduled (upgrade 12).
    learned_ids = set()
    heavy_learns = {}
    # Topics in scope, most important first: exercise sessions use
    # this same list (one practice session per topic).
    exercise_topics = list(remaining)
    for window in usable_windows:
        occupied_today = _occupied_today(window["date"])
        while remaining and learn_minutes + session_minutes <= buckets["learn"]:
            start = _next_free_start(
                window["cursor"], session_minutes, occupied_today, window["end"]
            )
            if start is None:
                break
            topic = remaining.pop(0)
            end = start + session_minutes
            sessions.append(
                {
                    "topic_id": _topic_value(topic, "id"),
                    "topic_title": _topic_value(topic, "title", ""),
                    "session_date": window["date"],
                    "start_time": _to_time(start),
                    "end_time": _to_time(end),
                    "kind": "learn",
                }
            )
            placed_by_date.setdefault(window["date"], []).append((start, end))
            learn_minutes += session_minutes
            topic_id = _topic_value(topic, "id")
            learned_ids.add(topic_id)
            heavy_learns[topic_id] = heavy_learns.get(topic_id, 0) + 1
            if (_topic_value(topic, "heavy", 0)
                    and heavy_learns[topic_id] < 2):
                # A heavy topic gets a second learning session:
                # back to the front of the queue (upgrade 12).
                remaining.insert(0, topic)
            window["cursor"] = end
            if not remaining:
                break
            # A break only when another learning session fits after it
            # in this same window (and neither the break nor that
            # session overlaps occupied time). Otherwise the next loop
            # round places the next session directly, with no break.
            if (
                break_minutes > 0
                and window["cursor"] + break_minutes + session_minutes
                <= window["end"]
                and learn_minutes + session_minutes <= buckets["learn"]
                and not _overlaps(window["cursor"],
                                  window["cursor"] + break_minutes,
                                  occupied_today)
                and not _overlaps(window["cursor"] + break_minutes,
                                  window["cursor"] + break_minutes
                                  + session_minutes,
                                  occupied_today)
            ):
                sessions.append(
                    {
                        "topic_id": None,
                        "topic_title": None,
                        "session_date": window["date"],
                        "start_time": _to_time(window["cursor"]),
                        "end_time": _to_time(window["cursor"] + break_minutes),
                        "kind": "break",
                    }
                )
                placed_by_date.setdefault(window["date"], []).append(
                    (window["cursor"], window["cursor"] + break_minutes))
                window["cursor"] += break_minutes

    # First learning date per topic (learning sessions are placed
    # in time order, so the first one found is the earliest).
    # Exercise and review for a topic may only land on or after
    # this date (QA fix 2).
    first_learn_date = {}
    for session in sessions:
        if session["kind"] == "learn" and session["topic_id"] is not None:
            first_learn_date.setdefault(session["topic_id"],
                                        session["session_date"])

    def _earliest_practice_date(topic):
        """Earliest date an exercise or review session for this
        topic may land on: its first learning session, or today
        when the topic was already learned before this plan. None
        when the topic has no learning session in this plan and
        was not learned before — there is nothing to practise yet,
        so no exercise or review is placed for it at all."""
        if _topic_value(topic, "learned", 0):
            return today
        return first_learn_date.get(_topic_value(topic, "id"))

    # Simulation: one long session near the exam, up to its bucket
    # and at most 120 minutes. Reserve it before review/exercise
    # fill the last windows, so it really lands close to the exam.
    simulation_minutes = min(buckets["simulation"], 120)
    if simulation_minutes > 0:
        placed = _place(simulation_minutes, list(reversed(usable_windows)))
        if placed is not None:
            sim_date, sim_start, sim_end = placed
            sessions.append(
                {
                    "topic_id": None,
                    "topic_title": None,
                    "session_date": sim_date,
                    "start_time": _to_time(sim_start),
                    "end_time": _to_time(sim_end),
                    "kind": "simulation",
                }
            )

    # Review, by spaced repetition (upgrade 10): a topic is
    # reviewed 1, 3 and 7 days after it was learned — the gaps
    # that help memory stick — when a free window fits on that
    # day. A review date with no fitting window is skipped
    # honestly, never pretended. The caps stay (QA fix 7, QA
    # round 3 fix 8): at most 2 review sessions a day, at most 3
    # per topic, and each one is attached to a specific topic,
    # so the row says what it reviews. A topic is only reviewed
    # on or after its first learning session (QA fix 2); a topic
    # with no learning session is skipped. A topic already
    # learned before this plan counts as learned today, so its
    # reviews land 1, 3 and 7 days from today.
    review_topics = list(exercise_topics)
    if not review_topics:
        review_topics = sort_topics_by_importance(list(topics))
    review_minutes = 0
    review_per_day = {}
    review_per_topic = {}
    review_targets = []  # (target date, importance order, topic)
    for order, topic in enumerate(review_topics):
        learn_date = _earliest_practice_date(topic)
        if learn_date is None:
            continue
        for offset in SPACED_REVIEW_OFFSETS:
            target = learn_date + timedelta(days=offset)
            # A review before the plan start is not scheduled —
            # the window rule covers reviews too, not just learning.
            if start_day <= target <= last_day:
                review_targets.append((target, order, topic))
    review_targets.sort(key=lambda pair: (pair[0], pair[1]))
    for target, _order, topic in review_targets:
        if review_minutes + REVIEW_MINUTES > buckets["review"]:
            break
        topic_id = _topic_value(topic, "id")
        if review_per_topic.get(topic_id, 0) >= MAX_REVIEW_PER_TOPIC:
            continue
        if review_per_day.get(target, 0) >= MAX_REVIEW_PER_DAY:
            continue
        placed = _place(
            REVIEW_MINUTES,
            [w for w in usable_windows if w["date"] == target])
        if placed is None:
            continue
        review_date, review_start, review_end = placed
        review_per_day[review_date] = \
            review_per_day.get(review_date, 0) + 1
        review_per_topic[topic_id] = \
            review_per_topic.get(topic_id, 0) + 1
        sessions.append(
            {
                "topic_id": topic_id,
                "topic_title": _topic_value(topic, "title", ""),
                "session_date": review_date,
                "start_time": _to_time(review_start),
                "end_time": _to_time(review_end),
                "kind": "review",
            }
        )
        review_minutes += REVIEW_MINUTES

    # Exercise: one session per topic (the topics in scope, most
    # important first), labelled as practice, up to its bucket.
    # Only on or after the topic's first learning session (QA
    # fix 2): a topic that was never learned gets no exercise
    # session. A topic whose exercise does not fit is skipped,
    # not fatal — the next topic may have an earlier learning
    # date and still fit.
    exercise_minutes = 0
    for topic in exercise_topics:
        if exercise_minutes + session_minutes > buckets["exercise"]:
            break
        min_date = _earliest_practice_date(topic)
        if min_date is None:
            continue
        allowed = [w for w in usable_windows if w["date"] >= min_date]
        placed = _place(session_minutes, allowed)
        if placed is None:
            continue
        ex_date, ex_start, ex_end = placed
        sessions.append(
            {
                "topic_id": _topic_value(topic, "id"),
                "topic_title": _topic_value(topic, "title", ""),
                "session_date": ex_date,
                "start_time": _to_time(ex_start),
                "end_time": _to_time(ex_end),
                "kind": "exercise",
            }
        )
        exercise_minutes += session_minutes

    sessions.sort(key=lambda s: (s["session_date"], s["start_time"]))
    # A heavy topic that got its first learning session but not
    # its second is learned, not unscheduled — only topics with
    # no learning session at all are reported as unscheduled
    # (upgrade 12).
    unscheduled = [t for t in remaining
                   if _topic_value(t, "id") not in learned_ids]
    return _result(sessions, unscheduled)


def save_plan(conn, exam_id, plan):
    """Replace an exam's old plan with the newly generated one.

    Sessions already marked done are never deleted: the work is
    done, and deleting it would pretend it never happened (QA
    fix 1). They stay in the plan, and the new plan is built
    around them (see regenerate_exam_plan)."""
    conn.execute(
        "DELETE FROM plan_sessions WHERE exam_id = ? AND done = 0",
        (exam_id,),
    )
    for session in plan["sessions"]:
        conn.execute(
            """
            INSERT INTO plan_sessions
                (exam_id, topic_id, session_date, start_time, end_time, kind)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                exam_id,
                session["topic_id"],
                session["session_date"].isoformat(),
                session["start_time"],
                session["end_time"],
                session["kind"],
            ),
        )


def regenerate_exam_plan(conn, exam_id, exam=None):
    """Rebuild one exam's plan from today, with the current routine.

    Shared by the /generate-plan link, the exam page, and the
    done-cascade: one place builds a plan, so they cannot drift
    apart. Learning never overlaps assignment blocks or busy
    blocks. It also never overlaps another exam's sessions (QA
    fix 1): the other exams' plan sessions count as occupied time,
    whatever kind they are. Sessions of this exam that are already
    marked done are kept (see save_plan), so they count as
    occupied too and the new plan is built around them. In focus
    mode the plan also avoids days where another exam already has
    any session (see generate_plan).
    Returns the plan dict, or None when the exam does not exist.
    """
    if exam is None:
        exam = conn.execute(
            "SELECT * FROM exams WHERE id = ?", (exam_id,)).fetchone()
    if exam is None:
        return None
    scoped = [dict(r) for r in get_scoped_topics(conn, exam)]
    slots = [dict(r) for r in get_free_slots(conn)]
    occupied = _blocks_as_occupied(get_all_assignment_blocks(conn))
    occupied += busy_blocks_as_occupied(get_busy_blocks(conn))
    # QA fix 1: the other exams' sessions are occupied time too —
    # a new plan must never land on top of another exam's session,
    # whatever kind it is (learn, review, exercise, simulation or
    # break). This exam's own done sessions are kept (see
    # save_plan), so they are occupied too: the new plan is built
    # around work that is already done.
    occupied += [
        {"session_date": s["session_date"],
         "start_time": s["start_time"], "end_time": s["end_time"]}
        for s in conn.execute(
            "SELECT session_date, start_time, end_time FROM plan_sessions"
            " WHERE exam_id != ?",
            (exam_id,)).fetchall()
    ]
    occupied += [
        {"session_date": s["session_date"],
         "start_time": s["start_time"], "end_time": s["end_time"]}
        for s in conn.execute(
            "SELECT session_date, start_time, end_time FROM plan_sessions"
            " WHERE exam_id = ? AND done = 1",
            (exam_id,)).fetchall()
    ]
    style = effective_study_style(conn)
    focus_blocked = set()
    if style == "focus":
        # QA fix 3: in focus mode a day is blocked when another
        # exam has ANY session there, not just a learning session —
        # one day stays one subject for every kind of session.
        focus_blocked = {
            r["session_date"] for r in conn.execute(
                "SELECT session_date FROM plan_sessions"
                " WHERE exam_id != ?",
                (exam_id,)).fetchall()
        }
    plan = generate_plan(
        scoped, slots, exam["exam_date"],
        session_minutes=effective_session_minutes(conn),
        break_minutes=effective_break_minutes(conn),
        occupied=occupied,
        learn_pct=exam["learn_pct"],
        review_pct=exam["review_pct"],
        exercise_pct=exam["exercise_pct"],
        simulation_pct=exam["simulation_pct"],
        study_style=style,
        focus_blocked_dates=focus_blocked,
        # Emergency mode (upgrade 11) is stored on the exam, so
        # every rebuild — creation, edit, done-cascade — honours
        # it the same way.
        emergency=bool(_topic_value(exam, "emergency", 0)),
        # Lead time (new feature) is stored on the exam the same
        # way: every rebuild honours the exam's own window.
        lead_days=exam_lead_days(exam),
    )
    save_plan(conn, exam_id, plan)
    return plan


def _item_date(item):
    """The date of a plan session or an assignment block: stored
    under "session_date", "block_date", or "date", as a date or an
    ISO string."""
    for key in ("session_date", "block_date", "date"):
        try:
            value = item[key]
        except (KeyError, IndexError, TypeError):
            continue
        if value is None:
            continue
        if isinstance(value, str):
            return date.fromisoformat(value)
        return value
    return None


def _item_done(item):
    """Done flag of a plan session or assignment block: works for
    both plain dicts and sqlite3.Row objects."""
    try:
        return bool(item["done"])
    except (KeyError, IndexError, TypeError):
        return False


def missed_items(items, day=None):
    """Work planned before `day` (today by default) that is still
    not done — plan sessions and assignment blocks alike. Missed
    work is never silently dropped: the today view shows it until
    it is done or rescheduled."""
    if day is None:
        day = date.today()
    if isinstance(day, str):
        day = date.fromisoformat(day)
    missed = [item for item in items
              if _item_date(item) is not None
              and _item_date(item) < day and not _item_done(item)]
    missed.sort(key=lambda item: (_item_date(item), item["start_time"]))
    return missed


def todays_items(items, day=None):
    """Today's items only, in time order. A pure function, easy to
    test: `items` are plan sessions and assignment blocks (dicts or
    rows), `day` defaults to today."""
    if day is None:
        day = date.today()
    if isinstance(day, str):
        day = date.fromisoformat(day)
    todays = [item for item in items if _item_date(item) == day]
    todays.sort(key=lambda item: item["start_time"])
    return todays


def annotate_today_items(items, now_minutes):
    """Mark today's items with "now", "missed" and "next", by the clock.

    The item happening right now (not done yet) gets "עכשיו"; an
    item whose time already ended today and is still not done gets
    "⏰ פספסת" — missed work is flagged the same day, not only
    from tomorrow; the first not-yet-started, not-done item after
    now gets "הבא בתור". Three separate flags — the old code used
    one flag for both "now" and "next", so once "עכשיו" was used,
    "הבא" never appeared at all. A done item is never "now",
    "missed" or "next": it is already finished. Breaks are not
    work and are never missed; busy blocks are blocked time, not
    work either. Pure function, easy to test with a fixed clock
    (QA fix 9). Returns the items with "marker" and
    "marker_class" added."""
    now_used = False
    next_used = False
    annotated = []
    for item in items:
        # QA round 3, fix 1: a corrupt time row cannot be placed
        # on the clock — it keeps no marker instead of crashing
        # the whole today view.
        try:
            start = _to_minutes(item["start_time"])
            end = _to_minutes(item["end_time"])
        except (ValueError, AttributeError, TypeError, KeyError):
            annotated.append({**item, "marker": "", "marker_class": ""})
            continue
        marker = ""
        marker_class = ""
        if (not _item_done(item) and not now_used
                and start <= now_minutes < end):
            marker = " 🔵 עכשיו"
            marker_class = " now"
            now_used = True
        elif (not _item_done(item)
              and item.get("kind") not in ("break", "busy")
              and end <= now_minutes):
            marker = " ⏰ פספסת"
            marker_class = " missed"
        elif (not _item_done(item) and not next_used
              and start > now_minutes):
            marker = " ➡️ הבא בתור"
            marker_class = " next"
            next_used = True
        annotated.append({**item, "marker": marker,
                          "marker_class": marker_class})
    return annotated


def exam_progress(conn, exam, today=None):
    """Countdown + progress for one exam (upgrade 5): how many
    days are left, and how many of its scoped topics are already
    learned. Display only — it is computed from data already in
    the database, no new logic. days_left is None when the exam
    date itself cannot be read."""
    today = today or date.today()
    try:
        exam_day = date.fromisoformat(exam["exam_date"])
    except (ValueError, TypeError):
        return {"days_left": None, "learned": 0, "total": 0}
    scoped = get_scoped_topics(conn, exam)
    learned = sum(1 for t in scoped if _topic_value(t, "learned", 0))
    return {"days_left": (exam_day - today).days,
            "learned": learned, "total": len(scoped)}


def exam_progress_text(progress):
    """One line: "עוד 9 ימים — נלמדו 3 מתוך 8 נושאים"."""
    days = progress["days_left"]
    if days is None:
        when = "תאריך לא ידוע"
    elif days > 0:
        when = f"עוד {days} ימים"
    elif days == 0:
        when = "המבחן היום"
    else:
        when = f"המבחן היה לפני {-days} ימים"
    return (f"{when} — נלמדו {progress['learned']} מתוך "
            f"{progress['total']} נושאים")


def assignment_schedule_summary(assignment, blocks):
    """How much of an assignment is scheduled, in minutes:
    (total minutes, scheduled minutes, unscheduled minutes). A
    corrupt block row is skipped, never counted — one bad time
    must not crash the home page (QA round 3, fix 1). Used both
    by the assignment row itself and by the "needs attention"
    box, so the two can never disagree."""
    total = int(round((assignment["estimated_hours"] or 0) * 60)) \
        if assignment["estimated_hours"] else 0
    scheduled = 0
    for b in blocks:
        try:
            scheduled += (_to_minutes(b["end_time"])
                          - _to_minutes(b["start_time"]))
        except (ValueError, AttributeError, TypeError, KeyError):
            continue
    return total, scheduled, total - scheduled


def unscheduled_reason(learn_pct, unscheduled):
    """The exact reason topics were left unscheduled: if the learn
    share is 0%, that is the reason — never "not enough free time"."""
    if not unscheduled:
        return None
    if learn_pct == 0:
        return "learning share is 0%"
    return "not enough free time"


def schedule_assignment(assignment, free_slots, occupied_sessions,
                        start_date=None, homework_pref="any"):
    """Schedule one assignment into whole blocks. Pure function.

    How it works, in plain words:
    1. Total minutes = estimated_hours * 60. If num_parts is set,
       split into that many equal parts (one contiguous block per
       part, labelled "חלק 1", "חלק 2", ...); otherwise the whole
       assignment is one block.
    2. Expand the weekly free slots into real windows from
       `start_date` (today by default) up to and including the due
       date. Windows are tried in time order, except that
       homework_pref moves matching windows first: 'morning' prefers
       windows starting before 12:00, 'evening' prefers windows
       starting at/after 16:00.
    3. Each part gets ONE whole contiguous block in a single window,
       never cut into smaller chunks and never with breaks inside,
       and never overlapping an occupied session (exam sessions or
       other assignment blocks).
    4. A part is split ONLY when no single free window fits it, and
       then only at free-window boundaries (one piece per window).
    5. Returns the blocks plus how many minutes could not be
       scheduled — reported honestly, never silently dropped.

    `assignment` needs estimated_hours, num_parts (or None) and
    due_date (an ISO string or a date). `occupied_sessions` are
    dicts/rows with session_date, start_time, end_time.
    """
    if start_date is None:
        start_date = date.today()
    if isinstance(start_date, str):
        start_date = date.fromisoformat(start_date)
    due_date = assignment.get("due_date") if hasattr(assignment, "get") \
        else assignment["due_date"]
    if isinstance(due_date, str):
        due_date = date.fromisoformat(due_date) if due_date else None
    estimated_hours = assignment.get("estimated_hours") \
        if hasattr(assignment, "get") else assignment["estimated_hours"]
    if not estimated_hours or due_date is None or due_date < start_date:
        total = int(round((estimated_hours or 0) * 60))
        return {"blocks": [], "scheduled_minutes": 0,
                "unscheduled_minutes": total}

    total_minutes = int(round(estimated_hours * 60))
    num_parts = assignment.get("num_parts") \
        if hasattr(assignment, "get") else assignment["num_parts"]
    if not num_parts or num_parts < 1:
        num_parts = 1
    # Equal parts: the first `remainder` parts get one extra minute.
    base, remainder = divmod(total_minutes, num_parts)
    part_sizes = [
        base + (1 if i < remainder else 0) for i in range(num_parts)
    ]
    # QA round 2, fix 8: a part is never shorter than 15 minutes —
    # 1 hour split into 10 parts used to make ten useless 6-minute
    # blocks. When the parts do not fit in the estimate at 15
    # minutes each, every part is still 15 minutes and the result
    # carries a warning saying so, instead of silently creating
    # micro-blocks.
    warning = None
    if part_sizes and min(part_sizes) < MIN_PART_MINUTES:
        if num_parts * MIN_PART_MINUTES > total_minutes:
            warning = (
                f"שימו לב: {num_parts} חלקים × 15 דקות מינימום לחלק "
                f"= {num_parts * MIN_PART_MINUTES} דקות, יותר מההערכה "
                f"של {total_minutes} דקות — כל חלק הוארך ל-15 דקות.")
        part_sizes = [max(size, MIN_PART_MINUTES) for size in part_sizes]

    # Expand weekly slots into concrete windows, in preference order.
    windows = []
    day = start_date
    while day <= due_date:
        for slot in free_slots:
            if slot["weekday"] == day.weekday():
                # Defensive, same as generate_plan: a bad slot
                # stored before validation existed is skipped,
                # never crashes the scheduling (QA round 2, fix 1).
                try:
                    start = _to_minutes(slot["start_time"])
                    end = _to_minutes(slot["end_time"])
                except (ValueError, AttributeError):
                    continue
                if end > start:
                    windows.append({"date": day, "start": start, "end": end})
        day += timedelta(days=1)

    def _rank(window):
        if homework_pref == "morning":
            return 0 if window["start"] < 12 * 60 else 1
        if homework_pref == "evening":
            return 0 if window["start"] >= 16 * 60 else 1
        return 0

    windows.sort(key=lambda w: (_rank(w), w["date"], w["start"]))

    occupied_by_date = _occupied_by_date(occupied_sessions)
    # Blocks placed in this call also count as occupied, so two parts
    # of the same assignment never overlap each other.
    placed_by_date = {}

    def _free_gaps(window):
        """Free intervals inside one window, in time order."""
        busy = sorted(occupied_by_date.get(window["date"], [])
                      + placed_by_date.get(window["date"], []))
        gaps = []
        cursor = window["start"]
        for start, end in busy:
            if start > cursor:
                gaps.append((cursor, min(start, window["end"])))
            cursor = max(cursor, end)
        if cursor < window["end"]:
            gaps.append((cursor, window["end"]))
        return [(s, e) for s, e in gaps if e > s]

    def _place_whole(part_number, part_label, minutes):
        """One contiguous block for a part, or None if no window fits."""
        for window in windows:
            for gap_start, gap_end in _free_gaps(window):
                if gap_end - gap_start >= minutes:
                    block = {
                        "assignment_id": assignment.get("id")
                        if hasattr(assignment, "get")
                        else assignment["id"],
                        "part_number": part_number,
                        "part_label": part_label,
                        "block_date": window["date"],
                        "start_time": _to_time(gap_start),
                        "end_time": _to_time(gap_start + minutes),
                    }
                    placed_by_date.setdefault(window["date"], []).append(
                        (gap_start, gap_start + minutes))
                    return block
        return None

    def _place_split(part_number, part_label, minutes):
        """Split a part across windows, one piece per free gap, only
        used when no single window fits the whole part."""
        pieces = []
        left = minutes
        for window in windows:
            if left <= 0:
                break
            for gap_start, gap_end in _free_gaps(window):
                if left <= 0:
                    break
                take = min(left, gap_end - gap_start)
                if take <= 0:
                    continue
                pieces.append({
                    "assignment_id": assignment.get("id")
                    if hasattr(assignment, "get") else assignment["id"],
                    "part_number": part_number,
                    "part_label": part_label,
                    "block_date": window["date"],
                    "start_time": _to_time(gap_start),
                    "end_time": _to_time(gap_start + take),
                })
                placed_by_date.setdefault(window["date"], []).append(
                    (gap_start, gap_start + take))
                left -= take
        return pieces

    blocks = []
    for i, minutes in enumerate(part_sizes, start=1):
        label = f"חלק {i}"
        block = _place_whole(i, label, minutes)
        if block is not None:
            blocks.append(block)
        else:
            blocks.extend(_place_split(i, label, minutes))

    scheduled = sum(
        _to_minutes(b["end_time"]) - _to_minutes(b["start_time"])
        for b in blocks
    )
    return {"blocks": blocks, "scheduled_minutes": scheduled,
            "unscheduled_minutes": max(0, total_minutes - scheduled),
            "warning": warning}


# A part of an assignment is never shorter than this: below it a
# "block" is too short to sit down and work in (QA round 2, fix 8).
MIN_PART_MINUTES = 15


def assignment_part_warning(assignment):
    """The warning for an assignment whose parts are too short for
    its estimate, or None when the parts fit fine: the same rule
    as schedule_assignment, so the page and the scheduler cannot
    disagree about it."""
    hours = assignment.get("estimated_hours") \
        if hasattr(assignment, "get") else assignment["estimated_hours"]
    parts = assignment.get("num_parts") \
        if hasattr(assignment, "get") else assignment["num_parts"]
    if not hours or not parts or parts < 1:
        return None
    total = int(round(hours * 60))
    if parts * MIN_PART_MINUTES > total:
        return (
            f"שימו לב: {parts} חלקים × 15 דקות מינימום לחלק "
            f"= {parts * MIN_PART_MINUTES} דקות, יותר מההערכה "
            f"של {total} דקות — כל חלק הוארך ל-15 דקות.")
    return None


def ics_escape(text):
    """Escape text for an .ics file (RFC 5545): backslash,
    semicolon, comma and newlines. A topic title with a comma
    must not break the calendar file."""
    return (str(text).replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\n", "\\n"))


def _ics_datetime(day, time_text):
    """A date + "HH:MM" into ICS floating local time, e.g.
    20261106T090000. Floating means "this wall-clock time in
    the calendar's own timezone" — no timezone database is
    needed, and importing the file into Google or Apple Calendar
    puts the event at the right local hour."""
    return (str(day).replace("-", "") + "T"
            + str(time_text).replace(":", "") + "00")


def collect_plan_events(conn, exam_id=None):
    """The plan as calendar events (upgrade 8): plan sessions
    and assignment blocks that are not done yet — done work needs
    no reminder. With an exam id, only that exam's sessions;
    without one, the whole plan, assignments included. The exam
    itself is an all-day event on its date, and busy blocks are
    events too — otherwise the imported calendar shows when to
    study but not when the exam is, and blocked time looks free."""
    # The id arrives as text from the URL ("/export-exam.ics?
    # exam_id=3") but as an int from code — normalise it, so the
    # per-exam export actually matches its own exam.
    if exam_id is not None:
        try:
            exam_id = int(exam_id)
        except (TypeError, ValueError):
            pass
    events = []
    if exam_id is None:
        rows = conn.execute(
            "SELECT ps.*, e.title AS exam_title,"
            " t.title AS topic_title FROM plan_sessions ps"
            " JOIN exams e ON e.id = ps.exam_id"
            " LEFT JOIN topics t ON t.id = ps.topic_id"
            " WHERE ps.done = 0"
            " ORDER BY ps.session_date, ps.start_time").fetchall()
    else:
        rows = conn.execute(
            "SELECT ps.*, e.title AS exam_title,"
            " t.title AS topic_title FROM plan_sessions ps"
            " JOIN exams e ON e.id = ps.exam_id"
            " LEFT JOIN topics t ON t.id = ps.topic_id"
            " WHERE ps.done = 0 AND ps.exam_id = ?"
            " ORDER BY ps.session_date, ps.start_time",
            (exam_id,)).fetchall()
    for s in rows:
        label = KIND_HE.get(s["kind"], s["kind"])
        if s["kind"] in ("learn", "exercise", "review") \
                and s["topic_title"]:
            label += f": {s['topic_title']}"
        events.append({
            "uid": f"session-{s['id']}@study-planner",
            "date": s["session_date"], "start": s["start_time"],
            "end": s["end_time"],
            "summary": f"{label} — {s['exam_title']}",
        })
    if exam_id is None:
        for b in conn.execute(
                "SELECT ab.*, a.title AS assignment_title"
                " FROM assignment_blocks ab"
                " JOIN assignments a ON a.id = ab.assignment_id"
                " WHERE a.done = 0"
                " ORDER BY ab.block_date, ab.start_time").fetchall():
            events.append({
                "uid": f"assignment-{b['id']}@study-planner",
                "date": b["block_date"], "start": b["start_time"],
                "end": b["end_time"],
                "summary": f"שיעורי בית: {b['assignment_title']}"
                           f" — {b['part_label']}",
            })
        # Busy blocks, today onwards: blocked time is visible in
        # the imported calendar too, with its reason.
        for b in conn.execute(
                "SELECT * FROM busy_blocks WHERE block_date >= ?"
                " ORDER BY block_date, start_time",
                (date.today().isoformat(),)).fetchall():
            events.append({
                "uid": f"busy-{b['id']}@study-planner",
                "date": b["block_date"], "start": b["start_time"],
                "end": b["end_time"],
                "summary": (f"תפוס: {b['reason']}"
                            if b["reason"] else "תפוס"),
            })
    # The exam itself, as an all-day event on its date — no
    # invented hour, just the day. Past exams are left out.
    for e in get_exams(conn):
        if exam_id is not None and e["id"] != exam_id:
            continue
        if not e["exam_date"]:
            continue
        if e["exam_date"] < date.today().isoformat():
            continue
        kind_he = "בוחן" if e["kind"] == "quiz" else "מבחן"
        events.append({
            "uid": f"exam-{e['id']}@study-planner",
            "date": e["exam_date"], "all_day": True,
            "summary": f"{kind_he}: {e['title']} — {e['course_name']}",
        })
    return events


def build_ics(events):
    """The events as a valid .ics file (upgrade 8): plain text,
    standard library only, no network, no passwords. Every event
    carries UID, DTSTAMP, DTSTART, DTEND and SUMMARY, so Google
    and Apple Calendar both import it. An all-day event (the exam
    itself) uses VALUE=DATE with the next day as DTEND — the
    standard way to say "this whole day"."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0",
             "PRODID:-//Study Planner//HE",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH"]
    for event in events:
        if event.get("all_day"):
            day = event["date"]
            if isinstance(day, str):
                day = date.fromisoformat(day)
            nxt = day + timedelta(days=1)
            lines += ["BEGIN:VEVENT",
                      f"UID:{ics_escape(event['uid'])}",
                      f"DTSTAMP:{stamp}",
                      "DTSTART;VALUE=DATE:" + day.strftime("%Y%m%d"),
                      "DTEND;VALUE=DATE:" + nxt.strftime("%Y%m%d"),
                      f"SUMMARY:{ics_escape(event['summary'])}",
                      "END:VEVENT"]
            continue
        lines += ["BEGIN:VEVENT",
                  f"UID:{ics_escape(event['uid'])}",
                  f"DTSTAMP:{stamp}",
                  "DTSTART:" + _ics_datetime(event["date"],
                                             event["start"]),
                  "DTEND:" + _ics_datetime(event["date"],
                                           event["end"]),
                  f"SUMMARY:{ics_escape(event['summary'])}",
                  "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def is_valid_time(text):
    """A real clock time: HH:MM with hours 00–23 and minutes
    00–59. "25:00" and "24:00" are not times, even though
    splitting on ":" would happily give a number."""
    try:
        hours_text, minutes_text = (text or "").split(":")
        hours, minutes = int(hours_text), int(minutes_text)
    except (ValueError, AttributeError):
        return False
    return 0 <= hours <= 23 and 0 <= minutes <= 59


def is_valid_slot(start_time, end_time):
    """A free slot is valid only if both times are real clock
    times and it ends after it starts."""
    if not is_valid_time(start_time) or not is_valid_time(end_time):
        return False
    try:
        return _to_minutes(end_time) > _to_minutes(start_time)
    except (ValueError, AttributeError):
        return False


BUSY_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})-(\d{2}:\d{2})"
    r"(?:\s+(.*))?$")


def parse_busy_lines(text, today=None):
    """Parse a pasted list of busy hours (upgrade 9) into
    (blocks, errors). One line per block, in a fixed format:

        2026-11-10 18:00-20:00 חברים

    Date, then start–end times, then an optional reason. Blank
    lines are ignored. A bad line is reported with its line
    number and is never saved — the good lines still are. Dates
    use the same rule as the busy form: real and not in the past.
    """
    blocks = []
    errors = []
    for number, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        match = BUSY_LINE_RE.match(line)
        if match is None:
            errors.append(
                f"שורה {number}: הפורמט לא מובן — צריך תאריך, "
                "שעות וסיבה, למשל 2026-11-10 18:00-20:00 חברים")
            continue
        block_date, start, end, reason = match.groups()
        if validate_date(block_date, today=today) is None:
            errors.append(
                f"שורה {number}: התאריך {block_date} לא הגיוני "
                "או בעבר")
            continue
        if not is_valid_slot(start, end):
            errors.append(
                f"שורה {number}: השעות {start}–{end} לא תקינות")
            continue
        blocks.append({"block_date": block_date, "start_time": start,
                       "end_time": end, "reason": (reason or "").strip()})
    return blocks, errors


def serialize_busy_lines(blocks):
    """The parsed blocks back into the same one-line-per-block
    format, so the confirm page can carry them to the save step
    in a hidden field without re-typing anything."""
    return "\n".join(
        f"{b['block_date']} {b['start_time']}-{b['end_time']}"
        + (f" {b['reason']}" if b["reason"] else "")
        for b in blocks
    )


def validate_slot(weekday, start_time, end_time):
    """Validate a free slot before saving it. Returns None when
    it is fine, or a Hebrew error message when it is not.

    QA round 2, fix 1: the old code checked only end > start, so
    weekday 7, -1 or "x" and times like 25:00 were stored, and
    then the home page crashed on WEEKDAYS_HE[7] — unfixable from
    the UI, because the page that lists the slots (and its delete
    link) was the page that crashed. A bad slot is now rejected
    at save time, with an error, and nothing is saved.
    """
    try:
        day = int(weekday)
    except (TypeError, ValueError):
        return "יום בשבוע חייב להיות מספר בין 0 (שני) ל-6 (ראשון)."
    if day < 0 or day > 6:
        return "יום בשבוע חייב להיות בין 0 (שני) ל-6 (ראשון)."
    if not is_valid_time(start_time) or not is_valid_time(end_time):
        return "השעות חייבות להיות שעות אמיתיות, בין 00:00 ל-23:59."
    if _to_minutes(end_time) <= _to_minutes(start_time):
        return "שעת הסיום חייבת להיות אחרי שעת ההתחלה."
    return None


def course_exists(conn, course_id):
    """True only when course_id names a course that really exists
    (QA round 2, fix 3: an empty or made-up id used to raise an
    unhandled database error and drop the connection)."""
    try:
        course_id = int(course_id)
    except (TypeError, ValueError):
        return False
    return conn.execute(
        "SELECT 1 FROM courses WHERE id = ?", (course_id,)
    ).fetchone() is not None


def slot_is_duplicate(conn, weekday, start_time, end_time):
    """True when this exact slot is already stored (QA round 2,
    fix 12)."""
    return conn.execute(
        "SELECT 1 FROM free_slots WHERE weekday = ? AND start_time = ?"
        " AND end_time = ?",
        (weekday, start_time, end_time),
    ).fetchone() is not None


def slot_overlaps(conn, weekday, start_time, end_time):
    """True when another slot on the same weekday overlaps this
    one (QA round 2, fix 12). The planner merges overlapping
    windows when it computes free time, so an overlap is only a
    warning, never a rejection."""
    try:
        start = _to_minutes(start_time)
        end = _to_minutes(end_time)
    except (ValueError, AttributeError):
        return False
    for row in conn.execute(
            "SELECT start_time, end_time FROM free_slots WHERE weekday = ?",
            (weekday,)):
        try:
            other_start = _to_minutes(row["start_time"])
            other_end = _to_minutes(row["end_time"])
        except (ValueError, AttributeError):
            continue
        if start < other_end and other_start < end:
            return True
    return False


def exam_topic_checkboxes(courses, topics):
    """Checkboxes for picking an exam/quiz scope, grouped by
    course inside a collapsed <details> per course, each labelled
    with the topic title and its current importance. A quiz for
    one course used to show every other course's checkboxes too,
    so Daniel opens only his course's list and sees only its
    topics. If there are no topics at all, say so plainly
    instead of showing an empty space — the user cannot pick what
    does not exist yet, and must add topics first (via the syllabus).
    Shared by the main page and the setup wizard (step 5)."""
    esc = html.escape
    if not topics:
        return (
            '<p class="notice">עדיין אין נושאים — קודם מוסיפים נושאים '
            'לקורס בסעיף <a href="#topics">נושאים (מסילבוס)</a>, ורק '
            'אז אפשר לבחור אותם לבוחן.</p>'
        )
    checkbox_groups = []
    for course in courses:
        course_topics = [
            t for t in topics if t["course_id"] == course["id"]
        ]
        if not course_topics:
            continue
        rows = "".join(
            f'<label><input type="checkbox" name="topic_id" '
            f'value="{t["id"]}"> {esc(t["title"])} — '
            f'{IMPORTANCE_HE.get(t["importance"] if "importance" in t.keys() else None, "לא נקבעה חשיבות")}'
            f'</label><br>'
            for t in course_topics
        )
        count = len(course_topics)
        noun = "נושא" if count == 1 else "נושאים"
        checkbox_groups.append(
            f'<details class="course-topics">'
            f"<summary>{esc(course['name'])} — {count} {noun}</summary>"
            f"{rows}</details>"
        )
    return "".join(checkbox_groups)


# Section ids on the main page, and the anchor each save redirects
# back to (Fix 2: stay in the same section instead of jumping to
# the top). "today" is a <section>, the rest are <details>.
SECTION_IDS = ("routine", "courses", "assignments", "topics", "slots",
               "exams", "today", "future", "busy", "summaries")

# Which section each POST save belongs to (Fix 2).
POST_SECTIONS = {
    "/save-prefs": "routine",
    "/add-course": "courses",
    "/add-assignment": "assignments",
    "/save-assignment": "assignments",
    "/add-topic": "topics",
    "/save-topics": "topics",
    "/add-slot": "slots",
    "/add-busy": "busy",
    "/save-busy": "busy",
    "/add-exam": "exams",
    "/save-exam": "exams",
}


# Warning codes carried in the redirect URL (?warn=...), shown as
# a notice on the page the user lands on (QA round 2, fixes 5, 7
# and 12: a save can succeed and still be worth a word — a
# same-day exam, a 0% learning split, an overlapping slot).
WARN_TEXTS = {
    "sameday": "המבחן היום — אין זמן ללמוד לפניו, רק חזרה. כדאי "
               "להזיז את התאריך אם רוצים זמן למידה.",
    "zerolearn": "שימו לב: עם 0% למידה לא יהיו גם מפגשי חזרה "
                 "ותרגול — שניהם דורשים נושאים שנלמדו. רק "
                 "סימולציה תתוכנן.",
    "slot_overlap": "שימו לב: החלון החדש חופף לחלון קיים באותו "
                    "יום — התכנון ממזג חלונות חופפים, אז לא נוצרה "
                    "כפילות זמן, אבל כדאי לבדוק את הרשימה.",
    # QA round 2, fix 6: marking work done rebuilds the plan from
    # today, so the other sessions' times may move. One line says
    # so, right where the tick was pressed — the plan itself is
    # rebuilt the same way as before, only the surprise is gone.
    "rescheduled": "התוכנית עודכנה — שעות המפגשים הבאים עלולות לזוז.",
}


def redirect_location(section=None, saved=False, warn=None):
    """Where a save redirects to: the same section, open, with its
    anchor — so the page lands where the user just worked instead
    of jumping back to the top. No section means plain "/" (top).
    saved=True adds ?saved=1, so the page can say "נשמר ✓" (QA
    round 2, fix 10); warn adds ?warn=<code> for a warning notice
    (see WARN_TEXTS)."""
    if section in SECTION_IDS:
        query = f"?open={section}"
        if saved:
            query += "&saved=1"
        if warn:
            query += f"&warn={warn}"
        return f"/{query}#{section}"
    return "/"


def _with_saved(url):
    """Append ?saved=1 (or &saved=1) to a redirect target that does
    not have it yet, so the page it lands on can say "נשמר ✓".
    Used when a form's "next" is a wizard step: the wizard page
    itself shows the confirmation, so a successful save is visible
    instead of the page silently reloading."""
    if "saved=" in url:
        return url
    return url + ("&saved=1" if "?" in url else "?saved=1")


def warn_notices(warn):
    """Warning texts for a comma-separated warn code string, or an
    empty list when there is nothing to warn about."""
    return [WARN_TEXTS[code] for code in (warn or "").split(",")
            if code in WARN_TEXTS]

def setup_is_complete(conn):
    """Setup is done once the user has added at least one topic:
    topics are the first thing the wizard produces, and without
    them there is no quiz scope and no plan to build."""
    return conn.execute(
        "SELECT COUNT(*) FROM topics").fetchone()[0] > 0


# ---------------------------------------------------------------------------
# Web UI (Hebrew, RTL). The server structure is unchanged from Step 1.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Shared look + shell (UI redesign, 2026-11-07). One stylesheet and one
# page shell for every page, so the dashboard, the section pages, the
# exam page and the wizard all look the same. CSS only, no JavaScript.
# ---------------------------------------------------------------------------

HOST = "0.0.0.0"
PORT = 8000

# Shared stylesheet for every page: one place defines the look.
# Palette: warm off-white page, white cards, dark-green buttons.
# Dark mode follows the phone/computer setting (prefers-color-scheme),
# so it needs no toggle and no JavaScript either.
SHARED_CSS = """
:root{--bg:#f7f3ea;--card:#ffffff;--ink:#23281f;--muted:#6b6f66;
--green:#1f5c3d;--green-dark:#16452e;--border:#e3ddd0;--radius:14px;
--red:#8f2b2b;--red-bg:#fdf0f0;--red-border:#f0c9c9;
--amber-bg:#fdf6e3;--amber-border:#e8d9a8}
html{font-size:18px}
body{font-family:system-ui,-apple-system,"Segoe UI","Helvetica Neue",Arial,sans-serif;
background:var(--bg);color:var(--ink);line-height:1.7;
max-width:860px;margin:2em auto;padding:0 1.2em}
h1{font-size:1.7rem;margin:.2em 0} h2{font-size:1.3rem;color:var(--green);margin:0 0 .6em}
h3{font-size:1.05rem;margin:1em 0 .4em}
a{color:var(--green)}
.card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);
padding:1.2em 1.4em;margin:1.2em 0;box-shadow:0 1px 3px rgba(0,0,0,.06)}
.card-quiet{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);
padding:1em 1.2em;margin:1em 0}
.topnav{display:flex;flex-wrap:wrap;gap:.5em;padding:.6em 0;position:sticky;top:0;
background:var(--bg);z-index:1}
.topnav a{display:inline-block;background:var(--card);border:1px solid var(--border);
border-radius:999px;padding:.45em 1em;text-decoration:none;font-size:.95rem}
.topnav a:hover{background:#e6f2e9}
.topnav a[aria-current="page"]{background:var(--green);color:#fff;border-color:var(--green)}
details.card summary{cursor:pointer}
details.card summary h2{display:inline}
details.card summary::after{content:" \▾";color:var(--muted)}
details.card[open] summary::after{content:" \▴";color:var(--muted)}
.field{margin:.9em 0}
.field>label{display:block;font-weight:600;margin-bottom:.3em}
.field input,.field select,.field textarea{width:100%;box-sizing:border-box}
.help{font-size:.9rem;color:var(--muted);margin:.2em 0 0}
.field-error{color:var(--red);font-weight:600;margin:.3em 0 0}
input[aria-invalid="true"],select[aria-invalid="true"],textarea[aria-invalid="true"]{border:2px solid var(--red)}
.today-hero{border:2px solid var(--green);border-radius:16px}
.today-hero h2{margin-top:0}
.notice{background:var(--amber-bg);border:1px solid var(--amber-border);border-radius:10px;padding:.6em .9em}
.setup-banner{background:#e6f2e9;border:2px solid var(--green);border-radius:10px;padding:.7em 1em;font-weight:600}
.error{color:var(--red);background:var(--red-bg);border:1px solid var(--red-border);border-radius:10px;padding:.6em .9em}
ul{padding-inline-start:1.2em} li{margin:.45em 0}
.timeline{list-style:none;padding-inline-start:0}
.timeline>li{margin:.5em 0}
.session{border-inline-start:4px solid #999;background:#faf9f6;border-radius:8px;padding:.5em .7em;list-style:none}
.session.learn{border-color:#1d6a8a;background:#eaf3f7}
.session.review{border-color:#8a6d1d;background:#faf3dd}
.session.exercise{border-color:#4a7c59;background:#eaf3ec}
.session.simulation{border-color:#7c4a6e;background:#f6ecf3}
.session.break{border-color:#9a9a8a;background:#f2f2ee;color:var(--muted)}
.session.assignment{border-color:#b45309;background:#fdf0dd}
.session.busy{border-color:#9a9a8a;background:#f2f2ee;color:var(--muted)}
.session.now{outline:2px solid var(--green);font-weight:600}
.session.next{background:#e6f2e9}
.session.missed{background:var(--red-bg);border-color:var(--red)}
.tl-item{display:grid;grid-template-columns:6.5em 1fr;gap:.6em;align-items:start}
.tl-time{font-weight:700;white-space:nowrap}
.tl-kind{display:inline-block;font-size:.85rem;font-weight:700;border:1px solid var(--border);
border-radius:999px;padding:.05em .6em;margin-inline-end:.4em;background:var(--card)}
.tl-break{opacity:.75}
.done-btn{display:inline-block;background:var(--green);color:#fff;border-radius:10px;
padding:.5em 1.1em;text-decoration:none;font-weight:700;min-height:44px;line-height:1.4;margin-top:.4em}
.done-btn:hover{background:var(--green-dark)}
.undo-link{display:inline-block;margin-top:.4em}
.progress{background:#e8e2d4;border-radius:999px;height:.7em;overflow:hidden;margin:.4em 0 .8em}
.progress>div{background:var(--green);height:100%}
input,select,textarea,button{min-height:44px;padding:.6em .8em;margin:.2em 0;font:inherit;font-size:1rem}
input,select,textarea{border:1px solid #cfc8b8;border-radius:8px;background:#fff}
input:focus,select:focus,textarea:focus{outline:2px solid var(--green);outline-offset:1px}
:focus-visible{outline:3px solid var(--green);outline-offset:2px}
button{background:var(--green);color:#fff;border:none;border-radius:10px;
padding:.7em 1.3em;cursor:pointer;font-size:1rem}
button:hover{background:var(--green-dark)}
.imp-btn{display:inline-block;background:var(--card);border:1px solid var(--green);
border-radius:999px;padding:.25em .8em;text-decoration:none;font-size:.9rem;margin:.1em}
.imp-btn.imp-current{background:var(--green);color:#fff}
.imp-btn.imp-current b{color:#fff}
textarea{width:100%;height:6em}
small ul{font-size:.9em;color:var(--muted)}
.step-badge{display:inline-block;background:var(--green);color:#fff;border-radius:999px;
padding:.1em .7em;font-size:.9rem;font-weight:700;margin-inline-end:.4em}
@media (prefers-color-scheme: dark){:root{--bg:#1d201c;--card:#262a25;--ink:#eef0e9;--muted:#b6bcb0;
--green:#7fc7a0;--green-dark:#a5d9bc;--border:#3c423a;--red:#f0a8a8;--red-bg:#3a2323;--red-border:#6b3333;
--amber-bg:#3a3220;--amber-border:#6b5f33}
.session.learn{background:#1e323c;border-color:#5aa3c2}
.session.review{background:#38331f;border-color:#c2a95a}
.session.exercise{background:#22382a;border-color:#7fc7a0}
.session.simulation{background:#382a33;border-color:#c27ba8}
.session.break{background:#2a2d28}
.session.assignment{background:#3a2c1c;border-color:#d99a4a}
.session.busy{background:#2a2d28}
.session.next{background:#22382a}
.session.missed{background:#3a2323;border-color:#f0a8a8}
.session{background:#2a2d28}
.topnav a:hover{background:#22382a}
.progress{background:#3c423a}
input,select,textarea{background:#2a2d28;color:var(--ink);border-color:#4a5048}}
@media print{.topnav,form,button,.imp-btn,.setup-banner,.skip-link{display:none} .done-btn{display:none} body{max-width:none} .card{border:none;box-shadow:none}}
"""

# The slim top nav: five destinations only. Today is the dashboard;
# everything else lives on its own page, so the home page is never a
# long list of settings again. Assignments have their own page too
# (/assignments), linked from the dashboard and the exams page.
NAV_ITEMS = (
    ("/", "היום"),
    ("/exams", "מבחנים"),
    ("/assignments", "מטלות"),
    ("/topics", "נושאים"),
    ("/courses", "קורסים"),
    ("/slots", "זמן פנוי"),
    ("/busy", "ימים חריגים"),
    ("/routine", "הרוטינה"),
)


def nav_html(active="/"):
    """Slim top navigation. The current page is marked, so a screen
    reader (and the eye) knows where it is."""
    links = "".join(
        '<a href="' + href + '"'
        + (' aria-current="page"' if href == active else "")
        + ">" + label + "</a>"
        for href, label in NAV_ITEMS
    )
    return (
        '<nav class="topnav" aria-label="ניווט ראשי">' + links + "</nav>"
    )


def status_html(error=None, saved=False, warn=""):
    """The saved/error/warning region. aria-live="polite" so a screen
    reader announces "נשמר ✓" or an error when it appears, without
    the user having to hunt for it."""
    parts = []
    if error:
        parts.append(
            '<p class="error"><b>שגיאה:</b> ' + html.escape(error) + "</p>")
    if saved:
        parts.append('<p class="notice">נשמר ✓</p>')
    parts.extend(
        '<p class="notice">' + html.escape(text) + "</p>"
        for text in warn_notices(warn)
    )
    inner = "".join(parts)
    return (
        '<div aria-live="polite" role="status" id="status">'
        + inner + "</div>"
    )


def field_error_html(field_errors, name):
    """The error for one field, rendered right under that field —
    not only at the top of the page. Empty when the field is fine."""
    if not field_errors or name not in field_errors:
        return ""
    return (
        '<p class="field-error" id="' + name + '-error">'
        + html.escape(field_errors[name]) + "</p>"
    )


def field_attrs(field_errors, name):
    """aria-invalid + describedby for a field that has an error, so
    a screen reader reads the error with the field."""
    if field_errors and name in field_errors:
        return ' aria-invalid="true" aria-describedby="' + name + '-error"'
    return ""


def shell_html(title, body, active="/"):
    """A whole page: head (viewport, shared CSS), skip link, slim
    nav, and <main>. Every page goes through here except the old
    full planner page, which keeps its own shell for now."""
    return f"""<!doctype html>
<html lang="he" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} — מתכנן לימודים</title>
<style>{SHARED_CSS}</style>
</head><body>
<a class="skip-link" href="#today">דלג לתוכנית היום</a>
<h1>📚 מתכנן לימודים</h1>
{nav_html(active)}
<main role="main">
{body}
</main>
</body></html>"""



def collect_page_data(courses, assignments, topics=None,
                      free_slots=None, exams=None, sessions_by_exam=None,
                      unscheduled_by_exam=None, prefs=None,
                      assignment_blocks_by_assignment=None, busy_blocks=None,
                      session_counts_by_exam=None,
                      unscheduled_reason_by_exam=None, open_section=None,
                      empty_scope_by_exam=None, exam_progress_by_exam=None,
                      topic_back=None):
    """Everything the pages need, computed once: the lists, the
    forms' options, today's items, the coming days, the attention
    box and the per-exam summaries. The dashboard, the full planner
    page and every section page all read this one dict, so they can
    never disagree with each other. topic_back is the page the
    topic buttons (importance, heavy, done) return to when they
    are shown on their own page (/topics): without it they fall
    back to the full planner page with the topics section open."""
    esc = html.escape
    topics = topics or []
    free_slots = free_slots or []
    exams = exams or []
    sessions_by_exam = sessions_by_exam or {}
    unscheduled_by_exam = unscheduled_by_exam or {}
    assignment_blocks_by_assignment = assignment_blocks_by_assignment or {}
    busy_blocks = busy_blocks or []
    session_counts_by_exam = session_counts_by_exam or {}
    unscheduled_reason_by_exam = unscheduled_reason_by_exam or {}
    empty_scope_by_exam = empty_scope_by_exam or {}
    exam_progress_by_exam = exam_progress_by_exam or {}

    course_options = "".join(
        f'<option value="{c["id"]}">{esc(c["name"])}</option>' for c in courses
    )
    course_rows = "".join(
        f"<li><b>{esc(c['name'])}</b> — {esc(c['teacher'])}"
        f' <a href="/delete-course?id={c["id"]}">מחק קורס</a></li>'
        for c in courses
    )
    # One-click sample data (upgrade 7): a friend who gets the
    # project sees a full plan at once instead of an empty page.
    # The link flips to "delete" while the sample exists, and
    # deleting removes exactly the sample — never real data.
    has_sample = any(c["name"] == SAMPLE_COURSE_NAME for c in courses)
    sample_data_html = (
        '<p><a href="/delete-sample-data">מחק נתוני דוגמה</a> — '
        "מסיר את קורס הדוגמה, הנושאים, המבחן והחלונות שלו בלבד.</p>"
        if has_sample else
        '<p><a href="/add-sample-data">מלא נתוני דוגמה</a> — קורס '
        "לדוגמה עם נושאים, חלונות ומבחן, כדי לראות מיד תוכנית מלאה.</p>"
    )
    assignment_rows = ""
    for a in assignments:
        blocks = assignment_blocks_by_assignment.get(a["id"], [])
        block_rows = "".join(
            f"<li>{esc(b['part_label'])}: {esc(b['block_date'])}"
            f" {esc(b['start_time'])}–{esc(b['end_time'])}</li>"
            for b in blocks
        )
        # One helper for both the row and the "needs attention"
        # box, so the two can never disagree about what is
        # unscheduled (upgrade 4).
        total, scheduled, unscheduled = \
            assignment_schedule_summary(a, blocks)
        # QA round 2, fix 8: when the parts are too short for the
        # estimate, say so on the assignment itself.
        part_warning = assignment_part_warning(a)
        assignment_rows += (
            "<li>"
            + ("✅ " if a["done"] else "⬜ ")
            + f"<b>{esc(a['title'])}</b> — {esc(a['course_name'])}"
            + (f" — due {esc(a['due_date'])}" if a["due_date"] else "")
            + (f" — {a['estimated_hours']} שעות" if a["estimated_hours"] else "")
            + (f" — {a['num_parts']} חלקים" if a["num_parts"] else "")
            + (f' <a href="/undone?id={a["id"]}" class="undo-link">בטל סימון</a>'
               if a["done"] else f' <a href="/done?id={a["id"]}" class="done-btn">סמן כבוצע ✓</a>')
            + (f' <a href="/schedule-assignment?id={a["id"]}">תזמן מחדש</a>'
               if not a["done"] and a["estimated_hours"] and a["due_date"] else "")
            + f' <a href="/assignment?id={a["id"]}">ערוך</a>'
            + f' <a href="/delete-assignment?id={a["id"]}">מחק</a>'
            + (f"<br>לא הצלחתי לתזמן {unscheduled} דקות — אין מספיק זמן פנוי לפני התאריך."
               if unscheduled > 0 and not a["done"] and a["due_date"] else "")
            # QA round 3, fix 6: an assignment that was saved
            # before the due-date/hours rule (or edited to clear
            # them) says plainly why it has no blocks, instead of
            # showing nothing at all.
            + (f"<br>לא שובצה — אין תאריך הגשה."
               if not a["due_date"] and not a["done"] else "")
            + (f"<br>לא שובצה — אין הערכת שעות."
               if a["due_date"] and not a["estimated_hours"]
               and not a["done"] else "")
            + (f"<br>{esc(part_warning)}" if part_warning else "")
            + (f"<ul>{block_rows}</ul>" if block_rows else "")
            + "</li>"
        )
    assignment_rows = assignment_rows or \
        "<li>אין מטלות עדיין — הוסף את הראשונה למטה.</li>"

    # Topics, grouped under each course, each with its importance.
    # Importance is shown as real buttons, not small text: the current
    # choice is marked, so it is impossible to miss. When the rows
    # are shown on the standalone /topics page, every button carries
    # next=/topics, so the click returns to that page instead of
    # dropping the user onto the full planner page.
    topic_back_q = (f"&next={quote(topic_back, safe='')}"
                    if topic_back else "")
    topic_rows = (
        "<p>נושאים חשובים יותר מתוכננים קודם, ופחות חשובים נשארים "
        "בחוץ קודם כשאין מספיק זמן. לחצו על כפתור כדי לשנות חשיבות.</p>"
    )
    for course in courses:
        course_topics = [t for t in topics if t["course_id"] == course["id"]]
        items = ""
        for t in course_topics:
            importance = t["importance"] if "importance" in t.keys() \
                else None
            importance_label = IMPORTANCE_HE.get(importance, "לא נקבעה חשיבות")
            buttons = " ".join(
                f'<a class="imp-btn{" imp-current" if value == importance else ""}"'
                f' href="/topic-importance?id={t["id"]}&importance={value}{topic_back_q}">'
                f'{"<b>" if value == importance else ""}{label}'
                f'{"</b>" if value == importance else ""}</a>'
                for value, label in
                (("low", "לא מאוד חשוב"), ("medium", "די חשוב"),
                 ("high", "מאוד חשוב"))
            )
            # Heavy topic (upgrade 12): a hard topic gets two
            # learning sessions instead of one. The toggle sits
            # next to the importance buttons, where the topic is
            # already being weighed.
            heavy = t["heavy"] if "heavy" in t.keys() else 0
            heavy_link = (
                f' <a href="/topic-heavy?id={t["id"]}&heavy=0{topic_back_q}">'
                "נושא כבד ✓ (בטל)</a>"
                if heavy else
                f' <a href="/topic-heavy?id={t["id"]}&heavy=1{topic_back_q}">'
                "סמן כנושא כבד</a>")
            items += (
                "<li>"
                + ("✅ " if t["learned"] else "⬜ ")
                + f"<b>{esc(t['title'])}</b> — חשיבות: {importance_label} "
                f"{buttons}{heavy_link}"
                + (f' <a href="/topic-undone?id={t["id"]}{topic_back_q}" class="undo-link">בטל סימון</a>'
                   if t["learned"]
                   else f' <a href="/topic-done?id={t["id"]}{topic_back_q}">סמן כנלמד</a>')
                + "</li>"
            )
        items = items or "<li>אין נושאים עדיין — הדבק סילבוס למטה.</li>"
        topic_rows += f"<h3>{esc(course['name'])}</h3><ul>{items}</ul>"
        if course_topics:
            # Topic editing lives on its own page (/edit-topics):
            # rename, delete one topic, importance and heavy flag.
            # Delete-all is NOT here any more — one careless click
            # on the everyday list used to wipe the whole course,
            # so it now lives on the edit page, behind a
            # confirmation page.
            topic_rows += (
                f'<p><a href="/edit-topics?course_id={course["id"]}">'
                "עריכת נושאים</a></p>"
            )

    # weekday_name, not WEEKDAYS_HE[...]: a bad row stored before
    # validation existed must show "יום לא תקין" instead of
    # crashing the whole page (QA round 2, fix 1).
    slot_rows = "".join(
        f"<li>{weekday_name(s['weekday'])} {esc(s['start_time'])}–{esc(s['end_time'])}"
        f' <a href="/delete-slot?id={s["id"]}">מחק</a></li>'
        for s in sorted(free_slots, key=sunday_first_key)
    ) or "<li>אין חלונות פנויים עדיין.</li>"
    weekday_options = weekday_options_html()


    # Existing exams, each linking to its dedicated page, where
    # scope and importance are chosen together. Countdown +
    # progress (upgrade 5) sit right next to each exam, so the
    # state of every exam is visible in one glance.
    exam_rows = ""
    for e in exams:
        progress = exam_progress_by_exam.get(e["id"])
        progress_text = (f" — {esc(exam_progress_text(progress))}"
                         if progress else "")
        emergency_tag = (" — 🚨 מצב חירום"
                         if _topic_value(e, "emergency", 0) else "")
        exam_rows += (
            f"<li><b>{esc(e['title'])}</b> — "
            f"{'בוחן' if e['kind'] == 'quiz' else 'מבחן'} "
            f"{esc(e['course_name'])} — {esc(e['exam_date'])}"
            f"{progress_text}{emergency_tag}"
            f' <a href="/exam?id={e["id"]}">פתח דף מבחן</a>'
            f' <a href="/delete-exam?id={e["id"]}">מחק מבחן</a></li>'
        )
    exam_rows = exam_rows or "<li>אין מבחנים עדיין.</li>"

    # --- Today-first plan view (Step 4) --------------------------------
    # One list of everything planned: exam sessions from every exam
    # plus assignment blocks. The top section is today only; below
    # it, a compact peek at the coming days.
    KIND_HE = {"learn": "למידה", "review": "חזרה", "exercise": "תרגול",
               "simulation": "סימולציה", "break": "הפסקה",
               "assignment": "שיעורי בית", "busy": "תפוס"}
    assignment_titles = {a["id"]: a["title"] for a in assignments}
    assignment_done = {a["id"]: bool(a["done"]) for a in assignments}
    all_items = []
    for exam in exams:
        for s in sessions_by_exam.get(exam["id"], []):
            label = KIND_HE.get(s["kind"], s["kind"])
            if s["kind"] in ("learn", "exercise", "review") \
                    and s["topic_title"]:
                label += f": {s['topic_title']}"
            all_items.append({
                "date": s["session_date"], "start_time": s["start_time"],
                "end_time": s["end_time"], "kind": s["kind"],
                "label": label, "done": bool(s["done"]),
                "exam_title": exam["title"],
                "session_id": s["id"], "exam_id": exam["id"],
            })
    for assignment_id, blocks in assignment_blocks_by_assignment.items():
        for b in blocks:
            all_items.append({
                "date": b["block_date"], "start_time": b["start_time"],
                "end_time": b["end_time"], "kind": "assignment",
                "label": f"שיעורי בית: {assignment_titles.get(assignment_id, '')}"
                         f" — {b['part_label']}",
                "done": assignment_done.get(assignment_id, False),
                "exam_title": "", "assignment_id": assignment_id,
            })
    # Blocked time shows in the calendar too: otherwise the day
    # looks free and empty, with no sign of why nothing was
    # planned there. "done" here only means "not work" — a busy
    # block is never ticked, never missed, and never counted in
    # today's done/total.
    for b in busy_blocks:
        all_items.append({
            "date": b["block_date"], "start_time": b["start_time"],
            "end_time": b["end_time"], "kind": "busy",
            "label": (f"תפוס: {b['reason']}" if b["reason"] else "תפוס"),
            "done": True,
            "exam_title": "",
        })

    today_date = date.today()
    today_items = todays_items(all_items, today_date)
    today_done = sum(1 for item in today_items
                     if item["done"] and item["kind"] != "busy")
    now_minutes = datetime.now().hour * 60 + datetime.now().minute
    # "next" sends a done-tick back here (URL-encoded, so the
    # anchor survives as part of the redirect target). It carries
    # warn=rescheduled, so the today view says the plan was
    # updated and the next sessions' times may move (QA round 2,
    # fix 6).
    today_next = quote("/?open=today&warn=rescheduled#today", safe="")
    today_rows = ""
    for item in annotate_today_items(today_items, now_minutes):
        marker = item["marker"]
        item_class = f"session {item['kind']}{item['marker_class']}"
        # A tick right where the work is: an assignment block done
        # marks the whole assignment done (and reshuffles the rest);
        # a session done marks just that session (and its topic
        # learned, for learning sessions). Breaks cannot be ticked.
        # Done is reversible: a done item shows "בטל סימון" instead,
        # so a mistaken click can be undone right where it happened.
        done_link = ""
        if not item["done"]:
            if item["kind"] == "assignment" and item.get("assignment_id"):
                done_link = (
                    f' <a href="/done?id={item["assignment_id"]}'
                    f'&next={today_next}" class="done-btn">סמן כבוצע ✓</a>')
            elif item["kind"] != "break" and item.get("session_id"):
                done_link = (
                    f' <a href="/session-done?id={item["session_id"]}'
                    f'&next={today_next}" class="done-btn">סמן כבוצע ✓</a>')
        elif item["kind"] == "assignment" and item.get("assignment_id"):
            done_link = (
                f' <a href="/undone?id={item["assignment_id"]}'
                f'&next={today_next}" class="undo-link">בטל סימון</a>')
        elif item["kind"] != "break" and item.get("session_id"):
            done_link = (
                f' <a href="/session-undone?id={item["session_id"]}'
                f'&next={today_next}" class="undo-link">בטל סימון</a>')
        kind_text = KIND_HE.get(item["kind"], item["kind"])
        label_text = item["label"]
        if ": " in label_text:
            label_text = label_text.split(": ", 1)[1]
        today_rows += (
            f'<li class="{item_class}"><div class="tl-item">'
            f'<div class="tl-time">{esc(item["start_time"])}–'
            f"{esc(item['end_time'])}</div>"
            f'<div class="tl-body"><span class="tl-kind">'
            f"{esc(kind_text)}</span> "
            + ("" if item["kind"] == "busy"
               else ("✅ " if item["done"] else "⬜ "))
            + f"<b>{esc(label_text)}</b>{marker}"
            + (f" ({esc(item['exam_title'])})" if item["exam_title"] else "")
            + done_link
            + "</div></div></li>"
        )
    today_rows = today_rows or "<li>אין שום דבר מתוכנן להיום.</li>"
    # Missed work: planned before today and still not done. Shown
    # here until it is ticked done or rescheduled — never dropped.
    missed_rows = ""
    for item in missed_items(all_items, today_date):
        # A break is not work: it is never ticked and never missed.
        if item["kind"] == "break":
            continue
        if item["kind"] == "assignment" and item.get("assignment_id"):
            actions = (
                f' <a href="/done?id={item["assignment_id"]}'
                f'&next={today_next}" class="done-btn">סמן כבוצע ✓</a>'
                f' <a href="/schedule-assignment?id={item["assignment_id"]}">'
                f"תזמן מחדש</a>")
        elif item.get("session_id"):
            actions = (
                f' <a href="/session-done?id={item["session_id"]}'
                f'&next={today_next}" class="done-btn">סמן כבוצע ✓</a>'
                f' <a href="/generate-plan?exam_id={item["exam_id"]}">'
                f"תזמן מחדש</a>")
        else:
            actions = ""
        missed_kind = KIND_HE.get(item["kind"], item["kind"])
        missed_label = item["label"]
        if ": " in missed_label:
            missed_label = missed_label.split(": ", 1)[1]
        missed_rows += (
            f'<li class="session {esc(item["kind"])}">'
            f'<div class="tl-item"><div class="tl-time">'
            f"{esc(_item_date(item).isoformat())}<br>"
            f"{esc(item['start_time'])}–{esc(item['end_time'])}</div>"
            f'<div class="tl-body"><span class="tl-kind">'
            f"{esc(missed_kind)}</span> "
            f"<b>{esc(missed_label)}</b>"
            + (f" ({esc(item['exam_title'])})" if item["exam_title"] else "")
            + actions
            + "</div></div></li>"
        )
    missed_section = (
        "<h2>פספסת — עדיין לא בוצע</h2>"
        "<p>זה מה שהיה מתוכנן לימים קודמים ועדיין לא סומן כבוצע. "
        "סמן כבוצע, או תזמן מחדש — התוכנית תיבנה מהיום והלאה.</p>"
        f"<ul class=\"timeline\">{missed_rows}</ul>"
        if missed_rows else ""
    )
    today_total = sum(1 for item in today_items
                      if item["kind"] != "busy")
    today_pct = round(100 * today_done / today_total) if today_total else 0
    today_section = (
        f"<h2>מה עושים היום</h2>"
        f"<p>הושלמו היום {today_done} מתוך {today_total}.</p>"
        f'<div class="progress" role="img" aria-label="הושלמו {today_done} מתוך {today_total}"><div style="width:{today_pct}%"></div></div>'
        f"<ul class=\"timeline\">{today_rows}</ul>"
        f"<p><a href=\"/export-plan.ics\">הורד את כל התוכנית כקובץ "
        "יומן (.ics) — ליומן גוגל / אפל</a></p>"
        f"{missed_section}"
    )

    # "בהמשך": the coming days, grouped by date — one line per day
    # summarising it, with the details in smaller text underneath.
    # The <h2> heading lives in the <summary> in page(), not here.
    future_items = [i for i in all_items if _item_date(i) > today_date]
    future_items.sort(key=lambda i: (_item_date(i), i["start_time"]))
    future_by_date = {}
    for item in future_items:
        future_by_date.setdefault(_item_date(item), []).append(item)
    future_section = ""
    if not future_by_date:
        future_section += "<p>אין תכנון לימים הבאים עדיין.</p>"
    for day in sorted(future_by_date):
        items = future_by_date[day]
        counts = {}
        for item in items:
            counts[item["kind"]] = counts.get(item["kind"], 0) + 1
        summary = ", ".join(
            f"{count} {KIND_HE.get(kind, kind)}"
            for kind, count in sorted(counts.items()))
        detail_rows = []
        for i in items:
            f_kind = KIND_HE.get(i["kind"], i["kind"])
            f_label = i["label"]
            if ": " in f_label:
                f_label = f_label.split(": ", 1)[1]
            detail_rows.append(
                f"<li class=\"session {esc(i['kind'])}\">"
                f"<div class=\"tl-item\"><div class=\"tl-time\">"
                f"{esc(i['start_time'])}–{esc(i['end_time'])}</div>"
                f"<div class=\"tl-body\"><span class=\"tl-kind\">"
                f"{esc(f_kind)}</span> <b>{esc(f_label)}</b>"
                f"</div></div></li>"
            )
        details = "".join(detail_rows)
        future_section += (
            f"<p><b>{WEEKDAYS_HE[day.weekday()]} {day.isoformat()}</b>: "
            f"{len(items)} פריטים ({summary}), "
            f"{esc(items[0]['start_time'])}–{esc(items[-1]['end_time'])}</p>"
            f"<small><ul class=\"timeline\">{details}</ul></small>"
        )

    # Per-exam summaries: session counts per kind, and the exact
    # reason any topics were left unscheduled.
    exam_sections = ""
    for exam in exams:
        counts = session_counts_by_exam.get(exam["id"], {})
        # QA round 2, fix 7: all four kinds are always shown, with
        # their real counts — including zeros. With 0% learning the
        # old text hid the fact that review and exercise were also
        # zero, while the split still advertised 40% and 30%.
        counts_text = ", ".join(
            f"{KIND_HE.get(kind, kind)} "
            f"{counts.get(kind, 0)}"
            for kind in ("learn", "review", "exercise", "simulation")
        )
        # QA round 2, fix 2: an exam with no topics at all has no
        # plan — say so plainly instead of showing an empty list.
        empty_scope_html = (
            "<p><b>אין נושאים בקורס, הוסף נושאים מהסילבוס קודם</b> — "
            "למבחן בלי נושאים אין מה לתכנן.</p>"
            if empty_scope_by_exam.get(exam["id"]) else ""
        )
        # QA round 2, fix 5: an exam dated today has no learning
        # day before it, so its plan is empty — say why.
        try:
            exam_day = date.fromisoformat(exam["exam_date"])
        except (ValueError, TypeError):
            exam_day = None
        too_close_html = (
            "<p><b>אין מפגשים — המבחן קרוב מדי:</b> המבחן היום, "
            "ואין ימי למידה לפניו. רק חזרה אפשרית.</p>"
            if exam_day is not None and exam_day <= today_date
            and not empty_scope_by_exam.get(exam["id"]) else ""
        )
        # QA round 2, fix 7: with 0% learning there is also no
        # review or exercise, because both need learned topics.
        zero_learn_html = (
            "<p>עם 0% למידה לא יהיו גם מפגשי חזרה ותרגול — שניהם "
            "דורשים נושאים שנלמדו. רק סימולציה תתוכנן.</p>"
            if exam["learn_pct"] == 0
            and not empty_scope_by_exam.get(exam["id"]) else ""
        )
        unscheduled = unscheduled_by_exam.get(exam["id"], [])
        reason = unscheduled_reason_by_exam.get(exam["id"])
        reason_text = {
            "learning share is 0%":
                "אחוז הלמידה הוא 0% — לא הוקצו מפגשי למידה בכלל, "
                "לכן הנושאים לא נכנסו. כדי לתקן: פתח את "
                f'<a href="/exam?id={exam["id"]}">דף המבחן</a>'
                " וערוך את אחוז הלמידה ליותר מ-0 — לא צריך "
                "להוסיף את המבחן מחדש.",
            "not enough free time":
                "אין מספיק זמן פנוי לפני התאריך.",
        }.get(reason, "")
        unscheduled_html = (
            "<p><b>לא נכנסו לתוכנית:</b> "
            + ", ".join(esc(t["title"]) for t in unscheduled)
            + (f" — {reason_text}" if reason_text else "")
            + "</p>"
            if unscheduled else ""
        )
        kind = "בוחן" if exam["kind"] == "quiz" else "מבחן"
        progress = exam_progress_by_exam.get(exam["id"])
        progress_html = (
            f"<p><b>{esc(exam_progress_text(progress))}</b></p>"
            if progress else ""
        )
        exam_sections += (
            f"<h3>{esc(exam['title'])} — {kind} {esc(exam['course_name'])}"
            f" — {esc(exam['exam_date'])}</h3>"
            f"{progress_html}"
            f"<p>חלוקת הכנה: למידה {exam['learn_pct']}%, חזרה "
            f"{exam['review_pct']}%, תרגול {exam['exercise_pct']}%, "
            f"סימולציה {exam['simulation_pct']}%</p>"
            f"<p>מפגשים לפי סוג: {esc(counts_text)}</p>"
            f"{empty_scope_html}"
            f"{too_close_html}"
            f"{zero_learn_html}"
            f'<p><a href="/exam?id={exam["id"]}">פתח דף מבחן — נושאים וחשיבות</a> · '
            f'<a href="/generate-plan?exam_id={exam["id"]}">צור / רענן תוכנית</a> · '
            f'<a href="/export-exam.ics?exam_id={exam["id"]}">הורד כקובץ יומן (.ics)</a></p>'
            f"{unscheduled_html}"
        )

    # --- "Needs attention" box (upgrade 4) ---------------------
    # The honest warnings that already exist scattered around the
    # page, collected in one place at the top: one read, no
    # hunting. It only renders when something actually needs
    # attention.
    attention_items = []
    if not (prefs and prefs["session_minutes"]
            and prefs["break_minutes"] is not None
            and prefs["homework_pref"] in ("morning", "evening", "any")):
        attention_items.append(
            "עוד לא ענית על שאלות הרוטינה — התוכנית משתמשת "
            "בברירת המחדל (50 דקות למידה, 10 דקות הפסקה).")
    for exam in exams:
        if empty_scope_by_exam.get(exam["id"]):
            attention_items.append(
                f"למבחן {exam['title']} אין נושאים — הוסף נושאים "
                "מהסילבוס קודם.")
        elif not sessions_by_exam.get(exam["id"]):
            attention_items.append(
                f"למבחן {exam['title']} אין מפגשים מתוכננים.")
    for a in assignments:
        if a["done"]:
            continue
        blocks = assignment_blocks_by_assignment.get(a["id"], [])
        _total, _scheduled, unscheduled = \
            assignment_schedule_summary(a, blocks)
        if not a["due_date"]:
            attention_items.append(
                f"למטלה {a['title']} אין תאריך הגשה — היא לא שובצה.")
        elif not a["estimated_hours"]:
            attention_items.append(
                f"למטלה {a['title']} אין הערכת שעות — היא לא שובצה.")
        elif unscheduled > 0:
            attention_items.append(
                f"למטלה {a['title']} לא שובצו {unscheduled} דקות — "
                "אין מספיק זמן פנוי לפני התאריך.")
    attention_html = (
        '<div class="card attention" role="region" '
        'aria-label="דורש תשומת לב"><h2>⚠️ דורש תשומת לב</h2><ul>'
        + "".join(f"<li>{esc(item)}</li>" for item in attention_items)
        + "</ul></div>"
        if attention_items else ""
    )

    # Busy blocks (date exceptions).
    busy_rows = "".join(
        f"<li>{esc(b['block_date'])} {esc(b['start_time'])}–{esc(b['end_time'])}"
        + (f" — {esc(b['reason'])}" if b["reason"] else "")
        + f' <a href="/delete-busy?id={b["id"]}">מחק</a></li>'
        for b in busy_blocks
    ) or "<li>אין ימים חריגים עדיין.</li>"

    # Routine section starts OPEN when it still needs an answer,
    # so the reminder is visible; every other section starts closed.
    # Fix 2: the section named by ?open=... also renders open, so a
    # redirect back to it shows the section the user just used.
    routine_open = " open" if open_section == "routine" or not (
        prefs and prefs["session_minutes"]
        and prefs["break_minutes"] is not None
        and prefs["homework_pref"] in ("morning", "evening", "any")) else ""
    open_flag = {sid: (" open" if open_section == sid else "")
                 for sid in SECTION_IDS}
    today = date.today().isoformat()
    # Sensible date defaults: the app knows what year it is, so the
    # user never has to type a year by hand — an exam in two weeks,
    # an assignment due in one week, a busy block today.
    exam_default = (date.today() + timedelta(days=14)).isoformat()
    assignment_default = (date.today() + timedelta(days=7)).isoformat()
    study_style = (prefs["study_style"]
                   if prefs and "study_style" in prefs.keys() else None)
    # QA round 2, fix 3: with no courses at all, the add forms
    # for assignments, topics and exams are hidden — an empty
    # course select used to crash the request when submitted —
    # and a pointer says to add a course first.
    no_courses_notice = (
        '<p class="notice">אין קורסים עדיין — '
        '<a href="#courses">הוסף קורס קודם</a>, ורק אז אפשר '
        'להוסיף מטלות, נושאים ומבחנים.</p>'
        if not courses else ""
    )
    return {
        "courses": courses,
        "assignments": assignments,
        "topics": topics,
        "free_slots": free_slots,
        "exams": exams,
        "prefs": prefs,
        "course_options": course_options,
        "course_rows": course_rows,
        "sample_data_html": sample_data_html,
        "assignment_rows": assignment_rows,
        "topic_rows": topic_rows,
        "slot_rows": slot_rows,
        "weekday_options": weekday_options,
        "exam_rows": exam_rows,
        "today_section": today_section,
        "future_section": future_section,
        "exam_sections": exam_sections,
        "attention_html": attention_html,
        "busy_rows": busy_rows,
        "routine_open": routine_open,
        "open_flag": open_flag,
        "today": today,
        "exam_default": exam_default,
        "assignment_default": assignment_default,
        "study_style": study_style,
        "no_courses_notice": no_courses_notice,
        "today_done": today_done,
        "today_total": today_total,
        "today_pct": today_pct,
        "sessions_by_exam": sessions_by_exam,
        "unscheduled_by_exam": unscheduled_by_exam,
        "session_counts_by_exam": session_counts_by_exam,
        "unscheduled_reason_by_exam": unscheduled_reason_by_exam,
        "empty_scope_by_exam": empty_scope_by_exam,
        "exam_progress_by_exam": exam_progress_by_exam,
        "assignment_blocks_by_assignment": assignment_blocks_by_assignment,
        "busy_blocks": busy_blocks,
    }


# ---------------------------------------------------------------------------
# UI redesign (2026-11-07): today-first dashboard, separate pages,
# stepped exam flow. All rendering goes through the fragments below,
# so the dashboard, the full planner page and each section page show
# the same lists and the same forms — one source of truth.
# ---------------------------------------------------------------------------

SECTION_ROUTES = {
    "/courses": "courses",
    "/exams": "exams",
    "/topics": "topics",
    "/assignments": "assignments",
    "/slots": "slots",
    "/routine": "routine",
    "/busy": "busy",
}

SECTION_TITLES = {
    "courses": "קורסים",
    "exams": "מבחנים",
    "topics": "נושאים (מסילבוס)",
    "assignments": "מטלות",
    "slots": "חלונות פנויים (שבועיים)",
    "routine": "הרוטינה שלי",
    "busy": "ימים חריגים",
}


def _wrap_section(section_id, title, inner, standalone, open_attr=""):
    """One section, two shapes: on the full planner page it is a
    collapsed <details> (open when it was just used); on its own
    page it is an open <section> — one thing on the screen."""
    if standalone:
        return (
            f'<section class="card" id="{section_id}">'
            f"<h2>{title}</h2>{inner}</section>"
        )
    return (
        f'<details class="card" id="{section_id}"{open_attr}>'
        f"<summary><h2>{title}</h2></summary>{inner}</details>"
    )


def _next_field(next_url):
    """Hidden 'next' field: after a save, go back to the page the
    form lives on (the section page), with ?saved=1 in the URL."""
    if not next_url:
        return ""
    return (
        '<input type="hidden" name="next" value="'
        + html.escape(next_url, quote=True) + '">'
    )


# --- Section fragments: each returns one section's HTML. ---------------

def fragment_routine(d, standalone=False, field_errors=None,
                     next_url=None):
    prefs = d["prefs"]
    study_style = d["study_style"]
    fe = field_errors or {}
    form = f"""
<form method="post" action="/save-prefs">
{_next_field(next_url)}
<div class="field"><label for="session_minutes">אורך מפגש למידה (דקות)</label>
<input id="session_minutes" name="session_minutes" type="number" min="15" max="180" value="{prefs["session_minutes"] if prefs and prefs["session_minutes"] else ""}" placeholder="למשל 50"{field_attrs(fe, "session_minutes")}>
<p class="help">כמה זמן רצוף אתה לומד לפני הפסקה. 15 עד 180 דקות.</p>
{field_error_html(fe, "session_minutes")}</div>
<div class="field"><label for="break_minutes">אורך הפסקה (דקות)</label>
<input id="break_minutes" name="break_minutes" type="number" min="0" max="60" value="{prefs["break_minutes"] if prefs and prefs["break_minutes"] is not None else ""}" placeholder="למשל 10"{field_attrs(fe, "break_minutes")}>
<p class="help">הפסקה בין מפגשי למידה. 0 עד 60 דקות.</p>
{field_error_html(fe, "break_minutes")}</div>
<div class="field"><label for="homework_pref">שיעורי בית עדיף</label>
<select id="homework_pref" name="homework_pref"{field_attrs(fe, "homework_pref")}>
<option value=""{" selected" if not prefs or not prefs["homework_pref"] else ""}>— לא נבחר —</option>
<option value="any"{" selected" if prefs and prefs["homework_pref"] == "any" else ""}>אין העדפה</option>
<option value="morning"{" selected" if prefs and prefs["homework_pref"] == "morning" else ""}>בבוקר</option>
<option value="evening"{" selected" if prefs and prefs["homework_pref"] == "evening" else ""}>בערב</option>
</select>
<p class="help">מתי שיעורי בית יושבצו קודם — בבוקר, בערב, או בלי העדפה.</p>
{field_error_html(fe, "homework_pref")}</div>
<div class="field"><label for="study_style">איך אתה מעדיף ללמוד ביום אחד</label>
<select id="study_style" name="study_style"{field_attrs(fe, "study_style")}>
<option value="variety"{" selected" if study_style in (None, "variety") else ""}>מערבבים מקצועות באותו יום (בבוקר מקצוע אחד, אחר כך מקצוע אחר)</option>
<option value="focus"{" selected" if study_style == "focus" else ""}>מתמקדים במקצוע אחד ביום (בוסט למקצוע בודד)</option>
</select>
<p class="help">מיקוד = מקצוע אחד ביום. ערבוב = כמה מקצועות באותו יום.</p>
{field_error_html(fe, "study_style")}</div>
<button>שמור רוטינה</button></form>"""
    return _wrap_section("routine", "הרוטינה שלי", form, standalone,
                         d["routine_open"])


def fragment_courses(d, standalone=False, field_errors=None,
                     next_url=None):
    fe = field_errors or {}
    form = f"""
<form method="post" action="/add-course">
{_next_field(next_url)}
<div class="field"><label for="course_name">שם הקורס</label>
<input id="course_name" name="name" placeholder="שם הקורס" required{field_attrs(fe, "name")}>
<p class="help">שם הקורס כפי שהוא מופיע בסילבוס.</p>
{field_error_html(fe, "name")}</div>
<div class="field"><label for="course_teacher">מרצה (לא חובה)</label>
<input id="course_teacher" name="teacher" placeholder="מרצה (לא חובה)">
<p class="help">לא חובה — רק כדי שתזהה את הקורס.</p></div>
<button>הוסף קורס</button></form>"""
    inner = f"<ul>{d['course_rows']}</ul>{d['sample_data_html']}{form}"
    return _wrap_section("courses", "קורסים", inner, standalone,
                         d["open_flag"]["courses"])


def fragment_assignments(d, standalone=False, field_errors=None,
                         next_url=None):
    fe = field_errors or {}
    if d["courses"]:
        form = f"""
<form method="post" action="/add-assignment">
{_next_field(next_url)}
<div class="field"><label for="assignment_course">קורס</label>
<select id="assignment_course" name="course_id"{field_attrs(fe, "course_id")}>{d["course_options"]}</select>
{field_error_html(fe, "course_id")}</div>
<div class="field"><label for="assignment_title">שם המטלה</label>
<input id="assignment_title" name="title" placeholder="שם המטלה" required{field_attrs(fe, "title")}>
<p class="help">שם קצר שתזהה, למשל "תרגיל 3".</p>
{field_error_html(fe, "title")}</div>
<div class="field"><label for="assignment_due">תאריך הגשה</label>
<input id="assignment_due" name="due_date" type="date" value="{d["assignment_default"]}"{field_attrs(fe, "due_date")}>
<p class="help">התאריך האחרון להגשה — המטלה תשובץ רק לפניו.</p>
{field_error_html(fe, "due_date")}</div>
<div class="field"><label for="assignment_hours">זמן משוער (שעות)</label>
<input id="assignment_hours" name="estimated_hours" type="number" min="0.5" step="0.5" placeholder="שעות (למשל 4)"{field_attrs(fe, "estimated_hours")}>
<p class="help">כמה שעות בסך הכול אתה מעריך שהמטלה תיקח.</p>
{field_error_html(fe, "estimated_hours")}</div>
<div class="field"><label for="assignment_parts">מספר שאלות / חלקים</label>
<input id="assignment_parts" name="num_parts" type="number" min="1" step="1" placeholder="מספר שאלות/חלקים">
<p class="help">כל חלק מקבל בלוק רצוף משלו. לא חובה.</p></div>
<button>הוסף מטלה</button></form>"""
    else:
        form = d["no_courses_notice"]
    inner = f"<ul>{d['assignment_rows']}</ul>{form}"
    return _wrap_section("assignments", "מטלות", inner, standalone,
                         d["open_flag"]["assignments"])


def fragment_topics(d, standalone=False, field_errors=None,
                    next_url=None):
    fe = field_errors or {}
    if d["courses"]:
        syllabus_form = f"""
<form method="post" action="/parse-syllabus">
{_next_field(next_url)}
<div class="field"><label for="syllabus_course">קורס</label>
<select id="syllabus_course" name="course_id"{field_attrs(fe, "course_id")}>{d["course_options"]}</select>
{field_error_html(fe, "course_id")}</div>
<div class="field"><label for="syllabus_text">טקסט הסילבוס — כל שורה תהפוך לנושא אחד</label>
<textarea id="syllabus_text" name="syllabus" placeholder="הדבק כאן את טקסט הסילבוס"{field_attrs(fe, "syllabus")}></textarea>
<p class="help">הדבק את הסילבוס, תאשר את הרשימה במסך הבא, ורק אז היא נשמרת.</p>
{field_error_html(fe, "syllabus")}</div>
<button>נתח סילבוס</button></form>"""
        topic_form = f"""
<form method="post" action="/add-topic">
{_next_field(next_url)}
<div class="field"><label for="topic_course">קורס</label>
<select id="topic_course" name="course_id"{field_attrs(fe, "course_id")}>{d["course_options"]}</select>
{field_error_html(fe, "course_id")}</div>
<div class="field"><label for="topic_title">נושא בודד</label>
<input id="topic_title" name="title" placeholder="נושא בודד" required{field_attrs(fe, "title")}>
<p class="help">נושא אחד שלא הגיע מהסילבוס.</p>
{field_error_html(fe, "title")}</div>
<button>הוסף נושא</button></form>"""
    else:
        syllabus_form = d["no_courses_notice"]
        topic_form = ""
    inner = f"{d['topic_rows']}{syllabus_form}{topic_form}"
    return _wrap_section("topics", "נושאים (מסילבוס)", inner, standalone,
                         d["open_flag"]["topics"])


def fragment_slots(d, standalone=False, field_errors=None,
                   next_url=None):
    fe = field_errors or {}
    form = f"""
<form method="post" action="/add-slot">
{_next_field(next_url)}
<div class="field"><label for="slot_weekday">יום בשבוע</label>
<select id="slot_weekday" name="weekday"{field_attrs(fe, "weekday")}>{d["weekday_options"]}</select>
{field_error_html(fe, "weekday")}</div>
<div class="field"><label for="slot_start">שעת התחלה</label>
<input id="slot_start" name="start_time" type="time" required{field_attrs(fe, "start_time")}>
{field_error_html(fe, "start_time")}</div>
<div class="field"><label for="slot_end">שעת סיום</label>
<input id="slot_end" name="end_time" type="time" required{field_attrs(fe, "end_time")}>
<p class="help">חלון קבוע שחוזר כל שבוע, למשל שני 09:00–12:00. התוכנית תיבנה רק בתוך החלונות האלה.</p>
{field_error_html(fe, "end_time")}</div>
<button>הוסף חלון פנוי</button></form>"""
    inner = f"<ul>{d['slot_rows']}</ul>{form}"
    return _wrap_section("slots", "חלונות פנויים (שבועיים)", inner,
                         standalone, d["open_flag"]["slots"])


def fragment_exams(d, standalone=False):
    # The old giant exam form is gone: adding an exam is now a
    # 3-step flow (/add-exam), one short screen per step. The list
    # stays here, with a link to each exam's own page.
    if not d["topics"]:
        empty = (
            '<p class="notice">עדיין אין נושאים — קודם מוסיפים '
            'נושאים לקורס ב<a href="/topics">נושאים (מסילבוס)</a>, '
            'ורק אז אפשר לבחור אותם לבוחן.</p>'
        )
    else:
        empty = ""
    inner = (
        f"<ul>{d['exam_rows']}</ul>"
        "<p>לכל מבחן יש דף משלו — שם בוחרים אילו נושאים בחומר "
        "וכמה כל נושא חשוב, ורואים את התוכנית. לחץ על "
        "״פתח דף מבחן״ ליד המבחן.</p>"
        f"{empty}"
        '<p><a class="done-btn" href="/add-exam">＋ הוסף מבחן / בוחן — '
        "3 שלבים קצרים</a></p>"
        '<p><a href="/assignments">מטלות ←</a></p>'
    )
    return _wrap_section("exams", "מבחנים ובחנים", inner, standalone,
                         d["open_flag"]["exams"])


def fragment_busy(d, standalone=False, field_errors=None,
                  next_url=None):
    fe = field_errors or {}
    form = f"""
<form method="post" action="/add-busy">
{_next_field(next_url)}
<div class="field"><label for="busy_date">תאריך</label>
<input id="busy_date" name="block_date" type="date" required value="{d["today"]}"{field_attrs(fe, "block_date")}>
{field_error_html(fe, "block_date")}</div>
<div class="field"><label for="busy_start">שעת התחלה</label>
<input id="busy_start" name="start_time" type="time" required{field_attrs(fe, "start_time")}>
{field_error_html(fe, "start_time")}</div>
<div class="field"><label for="busy_end">שעת סיום</label>
<input id="busy_end" name="end_time" type="time" required{field_attrs(fe, "end_time")}>
<p class="help">חריגה לתאריך אחד — למשל להשאיר את הערב פנוי לחברים — והתכנון לא ישבץ בה שום דבר.</p>
{field_error_html(fe, "end_time")}</div>
<div class="field"><label for="busy_reason">סיבה</label>
<input id="busy_reason" name="reason" placeholder="סיבה (למשל: ערב עם חברים)"></div>
<button>הוסף יום חריג</button></form>
<form method="post" action="/parse-busy">
{_next_field(next_url)}
<div class="field"><label for="busy_paste">הדבקת שעות תפוסות — שורה לכל חסימה</label>
<textarea id="busy_paste" name="busy_lines" placeholder="2026-11-10 18:00-20:00 חברים"></textarea></div>
<p>כל שורה: תאריך, שעות התחלה-סיום, וסיבה (לא חובה). לדוגמה: 2026-11-10 18:00-20:00 חברים. קודם תראה כמה שורות נמצאו, ורק אז תאשר.</p>
<button>נתח שעות תפוסות</button></form>"""
    inner = (
        "<p>החלונות השבועיים הם הדפוס הרגיל. כאן מוסיפים חריגה "
        "לתאריך אחד — למשל להשאיר את הערב פנוי לחברים — והתכנון "
        "לא ישבץ בה שום דבר.</p>"
        f"<ul>{d['busy_rows']}</ul>{form}"
    )
    return _wrap_section(
        "busy", "ימים חריגים (פחות פנויים מתאריך מסוים)", inner,
        standalone, d["open_flag"]["busy"])


def fragment_summaries(d, standalone=False):
    inner = d["exam_sections"]
    return _wrap_section("summaries", "סיכומי מבחנים ותוכניות", inner,
                         standalone, d["open_flag"]["summaries"])


def fragment_today(d):
    """Today first: the hero card, right at the top of the page."""
    return (
        f'<section class="card today-hero" id="today">'
        f"{d['today_section']}</section>"
    )


def fragment_future(d, standalone=False):
    inner = d["future_section"]
    if standalone:
        return (
            '<section class="card-quiet" id="future">'
            f"<h2>בהמשך</h2>{inner}</section>"
        )
    return (
        f'<details class="card" id="future"{d["open_flag"]["future"]}>'
        f"<summary><h2>בהמשך</h2></summary>{inner}</details>"
    )


def _dashboard_exams_html(d):
    """Compact exam list for the dashboard: title, date, countdown
    and progress, and the honest empty-scope line when an exam has
    no topics — the same words as the full summaries."""
    if not d["exams"]:
        return "<p>אין מבחנים עדיין.</p>"
    rows = ""
    for e in d["exams"]:
        progress = d["exam_progress_by_exam"].get(e["id"])
        progress_text = (
            f" — {html.escape(exam_progress_text(progress))}"
            if progress else ""
        )
        empty = (
            "<br><b>אין נושאים בקורס, הוסף נושאים מהסילבוס קודם</b>"
            if d["empty_scope_by_exam"].get(e["id"]) else ""
        )
        rows += (
            f"<li><b>{html.escape(e['title'])}</b> — "
            f"{'בוחן' if e['kind'] == 'quiz' else 'מבחן'} "
            f"{html.escape(e['course_name'])} — "
            f"{html.escape(e['exam_date'])}{progress_text}{empty}"
            f' <a href="/exam?id={e["id"]}">פתח דף מבחן</a></li>'
        )
    return f"<ul>{rows}</ul>"


def _dashboard_assignments_html(d):
    pending = [a for a in d["assignments"] if not a["done"]]
    if not pending:
        return "<p>אין מטלות פתוחות.</p>"
    rows = "".join(
        f"<li><b>{html.escape(a['title'])}</b>"
        + (f" — עד {html.escape(a['due_date'])}"
           if a["due_date"] else "")
        + f' <a href="/assignment?id={a["id"]}">פתח</a></li>'
        for a in pending
    )
    return f"<ul>{rows}</ul>"


def dashboard_page(d, error=None, saved=False, warn=""):
    """The home page: a daily dashboard only. Today first, then the
    rest of the week condensed, then a compact look at exams and
    assignments — with a quick add-assignment form, because
    homework is daily. Every other settings form has its own
    page, linked from the slim nav."""
    prefs = d["prefs"]
    routine_missing = not (
        prefs and prefs["session_minutes"]
        and prefs["break_minutes"] is not None
        and prefs["homework_pref"] in ("morning", "evening", "any"))
    banner = (
        "<p class=\"setup-banner\">עדיין לא הוספת נושאים — התחל עם "
        "<a href=\"/setup\">אשף ההתחלה</a>, צעד אחרי צעד: קורסים, "
        "סילבוס, חלונות, רוטינה ומבחן ראשון.</p>"
        if not d["topics"] else ""
    )
    reminder = (
        "<p class=\"notice\"><b>עוד לא ענית על שאלות הרוטינה</b> — "
        "בינתיים התוכנית משתמשת בברירת המחדל: מפגשי למידה של 50 "
        "דקות והפסקה של 10 דקות.</p>"
        if routine_missing else ""
    )
    body = f"""
{status_html(error, saved, warn)}
{banner}
{reminder}
{d["attention_html"]}
{fragment_today(d)}
{fragment_future(d, standalone=True)}
<section class="card-quiet" id="exams-peek"><h2>מבחנים</h2>
{_dashboard_exams_html(d)}
<p><a class="done-btn" href="/add-exam">＋ הוסף מבחן / בוחן</a> · <a href="/exams">כל המבחנים ←</a></p></section>
<section class="card-quiet" id="assignments-peek"><h2>מטלות פתוחות</h2>
{_dashboard_assignments_html(d)}
{quick_add_assignment_html(d)}
<p><a href="/assignments">כל המטלות ←</a></p></section>
<p><a href="/setup">🧭 אשף ההתחלה</a> · <a href="/export-plan.ics">הורד את כל התוכנית כקובץ יומן (.ics)</a></p>
"""
    return shell_html("היום", body, active="/")


def quick_add_assignment_html(d):
    """A compact add-assignment form right on the dashboard:
    homework is a daily thing — adding one should not need a
    trip to another page, the way adding an exam (once a
    semester) can. Same fields and same server-side validation
    as the full form on /assignments; on save it comes back
    here with "נשמר ✓" (hidden next=/?saved=1). Hidden when
    there are no courses yet — an empty course select used to
    crash the request (QA round 2, fix 3)."""
    if not d["courses"]:
        return ('<p>אין קורסים עדיין — '
                '<a href="/courses">הוסף קורס קודם</a>, ורק אז אפשר '
                'להוסיף מטלות.</p>')
    return (
        '<form method="post" action="/add-assignment">'
        '<input type="hidden" name="next" value="/?saved=1">'
        '<label>מטלה חדשה '
        '<input name="title" required></label> '
        '<label>קורס '
        f'<select name="course_id">{d["course_options"]}</select>'
        '</label> '
        '<label>הגשה '
        f'<input type="date" name="due_date" value="{d["assignment_default"]}">'
        '</label> '
        '<label>שעות '
        '<input name="estimated_hours" inputmode="decimal" size="4">'
        '</label> '
        '<label>חלקים '
        '<input name="num_parts" value="1" size="3">'
        '</label> '
        '<button type="submit">הוסף מטלה</button></form>'
    )


def section_page(d, section, error=None, saved=False, warn="",
                 field_errors=None):
    """One section on its own page: the form and the list, nothing
    else. The nav marks the page when it is one of the nav items."""
    next_url = f"/{section}?saved=1" if section != "exams" else None
    # Exams have no form any more (the stepped flow), so no next.
    fragments = {
        "routine": lambda: fragment_routine(
            d, standalone=True, field_errors=field_errors,
            next_url=f"/routine?saved=1"),
        "courses": lambda: fragment_courses(
            d, standalone=True, field_errors=field_errors,
            next_url="/courses?saved=1"),
        "assignments": lambda: fragment_assignments(
            d, standalone=True, field_errors=field_errors,
            next_url="/assignments?saved=1"),
        "topics": lambda: fragment_topics(
            d, standalone=True, field_errors=field_errors,
            next_url="/topics?saved=1"),
        "slots": lambda: fragment_slots(
            d, standalone=True, field_errors=field_errors,
            next_url="/slots?saved=1"),
        "exams": lambda: fragment_exams(d, standalone=True)
                        + fragment_summaries(d, standalone=True),
        "busy": lambda: fragment_busy(
            d, standalone=True, field_errors=field_errors,
            next_url="/busy?saved=1"),
    }
    fragment = fragments[section]()
    active = f"/{section}" if section in (
        "exams", "assignments", "topics", "courses", "slots", "busy",
        "routine") else ""
    body = f"""
{status_html(error, saved, warn)}
{fragment}
<p><a href="/">← חזרה להיום</a></p>
"""
    return shell_html(SECTION_TITLES[section], body, active=active)


def page(courses=None, assignments=None, topics=None, free_slots=None, exams=None,
         sessions_by_exam=None, unscheduled_by_exam=None, prefs=None,
         assignment_blocks_by_assignment=None, busy_blocks=None,
         error=None, session_counts_by_exam=None,
         unscheduled_reason_by_exam=None, open_section=None,
         saved=False, warn="", empty_scope_by_exam=None,
         exam_progress_by_exam=None, field_errors=None, data=None):
    """The full planner page: today first, then the rest of the
    week, then every section. Reached from ?open=<section> (after a
    save, or an error) — the plain home page is the dashboard."""
    d = data if data is not None else collect_page_data(
        courses, assignments, topics, free_slots, exams,
        sessions_by_exam, unscheduled_by_exam, prefs,
        assignment_blocks_by_assignment, busy_blocks,
        session_counts_by_exam, unscheduled_reason_by_exam,
        open_section, empty_scope_by_exam, exam_progress_by_exam)
    prefs = d["prefs"]
    routine_missing = not (
        prefs and prefs["session_minutes"]
        and prefs["break_minutes"] is not None
        and prefs["homework_pref"] in ("morning", "evening", "any"))
    banner = (
        "<p class=\"setup-banner\">עדיין לא הוספת נושאים — התחל עם "
        "<a href=\"/setup\">אשף ההתחלה</a>, צעד אחרי צעד: קורסים, "
        "סילבוס, חלונות, רוטינה ומבחן ראשון.</p>"
        if not d["topics"] else ""
    )
    reminder = (
        "<p class=\"notice\"><b>עוד לא ענית על שאלות הרוטינה</b> — "
        "בינתיים התוכנית משתמשת בברירת המחדל: מפגשי למידה של 50 "
        "דקות והפסקה של 10 דקות.</p>"
        if routine_missing else ""
    )
    return f"""<!doctype html>
<html lang="he" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>מתכנן לימודים</title>
<style>{SHARED_CSS}</style>
</head><body>
<a class="skip-link" href="#today">דלג לתוכנית היום</a>
<h1>📚 מתכנן לימודים</h1>
<p>היום {d["today"]}.</p>
{nav_html("")}
<main role="main">
{status_html(error, saved, warn)}
{banner}
{reminder}
{d["attention_html"]}
{fragment_today(d)}
{fragment_future(d)}
{fragment_routine(d, field_errors=field_errors if open_section == "routine" else None)}
{fragment_courses(d, field_errors=field_errors if open_section == "courses" else None)}
{fragment_assignments(d, field_errors=field_errors if open_section == "assignments" else None)}
{fragment_topics(d, field_errors=field_errors if open_section == "topics" else None)}
{fragment_slots(d, field_errors=field_errors if open_section == "slots" else None)}
{fragment_exams(d)}
{fragment_busy(d, field_errors=field_errors if open_section == "busy" else None)}
{fragment_summaries(d)}
</main>
</body></html>"""



# ---------------------------------------------------------------------------
# Stepped exam flow (/add-exam): adding an exam is three short screens
# instead of one long form — details, then topics, then prep split.
# The draft travels as plain hidden fields / query params between the
# steps (no server-side session, nothing to log in to), and the final
# step creates the exam with the same validation as the old form.
# ---------------------------------------------------------------------------

def _exam_draft_from_query(query):
    """The draft between flow steps: course, kind, title, date and
    the picked topic ids, from the query (GET) or the posted fields.
    Missing values stay blank — the step pages fill their defaults."""
    get = lambda k: query.get(k, [""])[0]
    return {
        "course_id": get("course_id"),
        "kind": get("kind") or "exam",
        "title": get("title"),
        "exam_date": get("exam_date"),
        "topic_id": [t for t in query.get("topic_id", []) if t.strip()],
    }


def _exam_draft_is_usable(conn, draft):
    """Step 2/3 need a real draft: a course that exists, a title and
    a real date. Otherwise the flow starts again at step 1."""
    return bool(
        draft["title"]
        and course_exists(conn, draft["course_id"])
        and validate_date(draft["exam_date"]) is not None
        and draft["kind"] in ("exam", "quiz")
    )


def _draft_query(draft):
    """The draft as a query string, so a step can link back to the
    previous one without losing what was already typed."""
    from urllib.parse import urlencode as _urlencode
    pairs = [(k, draft[k]) for k in
             ("course_id", "kind", "title", "exam_date")]
    pairs += [("topic_id", t) for t in draft["topic_id"]]
    return _urlencode(pairs, doseq=True)


def _draft_hidden_fields(draft, with_topics=True):
    """The draft as hidden inputs, so a POST carries it to the
    next step."""
    fields = "".join(
        '<input type="hidden" name="' + k + '" value="'
        + html.escape(str(draft[k]), quote=True) + '">'
        for k in ("course_id", "kind", "title", "exam_date")
    )
    if with_topics:
        fields += "".join(
            '<input type="hidden" name="topic_id" value="'
            + html.escape(str(t), quote=True) + '">'
            for t in draft["topic_id"]
        )
    return fields


def create_exam_from_fields(conn, course_id, kind, title, exam_date,
                            learn_raw, review_raw, exercise_raw,
                            simulation_raw, lead_raw, emergency, picked):
    """Validate and create an exam, shared by the old single-form
    POST (/add-exam — the wizard and the tests still use it) and by
    step 3 of the stepped flow. Returns (exam_id, message,
    field_errors): exam_id is None when validation fails, and then
    message is the Hebrew error and field_errors names the field it
    belongs to, so the error can sit right under that field. The
    checks run in the same order as the old form did — title, date,
    percentages, course, kind, lead time, scope — so an exam that
    was rejected before is rejected the same way now."""
    if not title:
        message = "שם המבחן חסר — המבחן לא נשמר."
        return None, message, {"title": message}
    if validate_date(exam_date) is None:
        message = ("התאריך לא הגיוני — בחר תאריך אמיתי מהיום והלאה. "
                   "המבחן לא נשמר.")
        return None, message, {"exam_date": message}
    pcts = (
        parse_percentage(learn_raw, 40),
        parse_percentage(review_raw, 20),
        parse_percentage(exercise_raw, 20),
        parse_percentage(simulation_raw, 20),
    )
    if not validate_percentages(*pcts):
        message = ("אחוזי ההכנה חייבים להסתכם ב-100 — המבחן לא נשמר.")
        return None, message, {"learn_pct": message}
    if not course_exists(conn, course_id):
        message = "בחר קורס קיים — המבחן לא נשמר."
        return None, message, {"course_id": message}
    kind = kind or "exam"
    if kind not in ("exam", "quiz"):
        message = "סוג המבחן לא תקין — המבחן לא נשמר."
        return None, message, {"kind": message}
    lead_days = parse_lead_days(lead_raw)
    if not validate_lead_days(lead_days):
        message = ("מספר הימים לא תקין — הלמידה מתחילה 1 עד 30 ימים "
                   "לפני המבחן. המבחן לא נשמר.")
        return None, message, {"lead_days": message}
    scope = resolve_exam_scope(conn, kind, course_id, picked)
    if scope is None:
        message = ("בוחן חייב לכלול לפחות נושא אחד — סמן נושאים "
                   "לפני השמירה.")
        return None, message, {"topic_id": message}
    cursor = conn.execute(
        "INSERT INTO exams"
        " (course_id, kind, title, exam_date,"
        "  learn_pct, review_pct, exercise_pct, simulation_pct,"
        "  emergency, lead_days)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (course_id, kind, title, exam_date, *pcts, emergency, lead_days),
    )
    exam_id = cursor.lastrowid
    for topic_id in scope:
        conn.execute(
            "INSERT OR IGNORE INTO exam_topics (exam_id, topic_id)"
            " VALUES (?, ?)",
            (exam_id, topic_id),
        )
    # The new exam gets its plan immediately, and every exam is
    # rebuilt in exam-date order, so the earliest exam keeps its
    # time (QA fixes 1 and 6).
    regenerate_all_exam_plans(conn)
    return exam_id, None, {}


def exam_flow_step1_html(conn, draft, field_errors=None, error=None):
    """Step 1 of 3: which course, exam or quiz, name, date."""
    esc = html.escape
    fe = field_errors or {}
    courses = get_courses(conn)
    options = "".join(
        f'<option value="{c["id"]}"'
        f'{" selected" if str(c["id"]) == str(draft["course_id"]) else ""}>'
        f"{esc(c['name'])}</option>"
        for c in courses
    )
    exam_default = (date.today() + timedelta(days=14)).isoformat()
    body = f"""
{status_html(error)}
<h2><span class="step-badge">שלב 1 מתוך 3</span> פרטי המבחן</h2>
<p>שלושה שלבים קצרים: פרטים, נושאים, אחוזי הכנה. אפשר לחזור אחורה בכל שלב.</p>
<form method="post" action="/add-exam-step1">
<div class="field"><label for="f_exam_course">קורס</label>
<select id="f_exam_course" name="course_id"{field_attrs(fe, "course_id")}>{options}</select>
<p class="help">באיזה קורס המבחן.</p>
{field_error_html(fe, "course_id")}</div>
<div class="field"><label for="f_exam_kind">סוג</label>
<select id="f_exam_kind" name="kind"{field_attrs(fe, "kind")}>
<option value="exam"{" selected" if draft["kind"] != "quiz" else ""}>מבחן</option>
<option value="quiz"{" selected" if draft["kind"] == "quiz" else ""}>בוחן</option>
</select>
<p class="help">בוחן = חלק מהחומר, ובשלב הבא חובה לבחור נושאים. מבחן = כל הקורס כברירת מחדל.</p>
{field_error_html(fe, "kind")}</div>
<div class="field"><label for="f_exam_title">שם המבחן / הבוחן</label>
<input id="f_exam_title" name="title" value="{esc(draft["title"])}" placeholder="שם המבחן / הבוחן" required{field_attrs(fe, "title")}>
<p class="help">שם שתזהה, למשל "מבחן אמצע חדו״א 2".</p>
{field_error_html(fe, "title")}</div>
<div class="field"><label for="f_exam_date">תאריך</label>
<input id="f_exam_date" name="exam_date" type="date" required value="{esc(draft["exam_date"]) or exam_default}"{field_attrs(fe, "exam_date")}>
<p class="help">תאריך אמיתי מהיום והלאה. ברירת המחדל: בעוד שבועיים.</p>
{field_error_html(fe, "exam_date")}</div>
<button>המשך לשלב 2 ←</button></form>
<p><a href="/exams">ביטול — חזרה למבחנים</a></p>
"""
    return shell_html("הוספת מבחן — שלב 1", body, active="/exams")


def _flow_topic_rows(conn, draft):
    """Step 2's topic picker: checkboxes grouped by course in a
    collapsed <details> per course (same helper shape as the old
    form), each line with its importance label and radios to change
    it. The radios only change importance when one is picked."""
    esc = html.escape
    courses = get_courses(conn)
    topics = get_topics(conn)
    if not topics:
        return (
            '<p class="notice">עדיין אין נושאים — קודם מוסיפים '
            'נושאים לקורס ב<a href="/topics">נושאים (מסילבוס)</a>, '
            'ורק אז אפשר לבחור אותם לבוחן.</p>'
        )
    groups = []
    for course in courses:
        course_topics = [t for t in topics
                         if t["course_id"] == course["id"]]
        if not course_topics:
            continue
        rows = ""
        for t in course_topics:
            importance = t["importance"] if "importance" in t.keys() \
                else None
            label = IMPORTANCE_HE.get(importance, "לא נקבעה חשיבות")
            checked = (" checked"
                       if str(t["id"]) in [str(x) for x in draft["topic_id"]]
                       else "")
            radios = " ".join(
                f'<label><input type="radio" name="importance_{t["id"]}" '
                f'value="{value}"'
                f'{" checked" if value == importance else ""}> '
                f"{radio_label}</label>"
                for value, radio_label in
                (("low", "לא מאוד"), ("medium", "די חשוב"),
                 ("high", "מאוד חשוב"))
            )
            rows += (
                f'<label><input type="checkbox" name="topic_id" '
                f'value="{t["id"]}"{checked}> '
                f'{esc(t["title"])} — {label}</label> {radios}<br>'
            )
        count = len(course_topics)
        noun = "נושא" if count == 1 else "נושאים"
        groups.append(
            f'<details class="course-topics">'
            f"<summary>{esc(course['name'])} — {count} {noun}</summary>"
            f"{rows}</details>"
        )
    return "".join(groups)


def exam_flow_step2_html(conn, draft, field_errors=None, error=None):
    """Step 2 of 3: which topics are in scope, and how important
    each one is."""
    fe = field_errors or {}
    body = f"""
{status_html(error)}
<h2><span class="step-badge">שלב 2 מתוך 3</span> נושאים וחשיבות</h2>
<p>סמן ב-V את הנושאים שנכנסים {"לבוחן" if draft["kind"] == "quiz" else "למבחן"}. לבוחן חובה לסמן לפחות נושא אחד. למבחן אפשר להשאיר ריק — ברירת המחדל היא כל הקורס. נושאים חשובים יותר מתוכננים קודם.</p>
<form method="post" action="/add-exam-step2">
{_draft_hidden_fields(draft, with_topics=False)}
{_flow_topic_rows(conn, draft)}
{field_error_html(fe, "topic_id")}
<p class="help">חשיבות: בחר ליד כל נושא כמה הוא חשוב. מי שלא בוחרים לו חשיבות נשאר כמו שהיה.</p>
<button>המשך לשלב 3 ←</button></form>
<p><a href="/add-exam?step=1&amp;{_draft_query(draft)}">→ חזרה לשלב 1</a> · <a href="/exams">ביטול</a></p>
"""
    return shell_html("הוספת מבחן — שלב 2", body, active="/exams")


def exam_flow_step3_html(conn, draft, field_errors=None, error=None):
    """Step 3 of 3: how the prep time splits, when learning starts,
    and emergency mode. Saving creates the exam and its plan."""
    fe = field_errors or {}
    body = f"""
{status_html(error)}
<h2><span class="step-badge">שלב 3 מתוך 3</span> חלוקת הכנה</h2>
<p>איך מתחלק זמן ההכנה בין למידה, חזרה, תרגול וסימולציה — ומתי מתחילים ללמוד.</p>
<form method="post" action="/add-exam-step3">
{_draft_hidden_fields(draft)}
<div class="field"><label for="f_learn_pct">למידה (%)</label>
<input id="f_learn_pct" name="learn_pct" type="number" min="0" max="100" value="40"{field_attrs(fe, "learn_pct")}>
<p class="help">אחוז זמן ההכנה ללמידת נושאים חדשים. עם 0% לא יהיו גם חזרה ותרגול.</p>
{field_error_html(fe, "learn_pct")}</div>
<div class="field"><label for="f_review_pct">חזרה (%)</label>
<input id="f_review_pct" name="review_pct" type="number" min="0" max="100" value="20"{field_attrs(fe, "review_pct")}>
<p class="help">חזרה על נושאים שכבר נלמדו, 1, 3 ו-7 ימים אחרי הלמידה.</p>
{field_error_html(fe, "review_pct")}</div>
<div class="field"><label for="f_exercise_pct">תרגול (%)</label>
<input id="f_exercise_pct" name="exercise_pct" type="number" min="0" max="100" value="20"{field_attrs(fe, "exercise_pct")}>
<p class="help">פתרון תרגילים ושיעורי בית.</p>
{field_error_html(fe, "exercise_pct")}</div>
<div class="field"><label for="f_simulation_pct">סימולציה (%)</label>
<input id="f_simulation_pct" name="simulation_pct" type="number" min="0" max="100" value="20"{field_attrs(fe, "simulation_pct")}>
<p class="help">מבחני דמה מלאים לקראת המבחן.</p>
{field_error_html(fe, "simulation_pct")}</div>
<p class="help">ארבעת האחוזים חייבים להסתכם ב-100.</p>
<div class="field"><label for="f_lead_days">להתחיל ללמוד כמה ימים לפני המבחן</label>
<input id="f_lead_days" name="lead_days" type="number" min="1" max="30" value="14"{field_attrs(fe, "lead_days")}>
<p class="help">1 עד 30 ימים, ברירת המחדל 14 — אף פעם לא יותר מחודש מראש.</p>
{field_error_html(fe, "lead_days")}</div>
<div class="field"><label><input type="checkbox" name="emergency" value="1"> מצב חירום — רק נושאים מאוד חשובים, בלי סימולציה</label>
<p class="help">כשאין זמן: התוכנית מתמקדת רק בנושאים הכי חשובים.</p></div>
<button>שמור מבחן וצור תוכנית</button></form>
<p><a href="/add-exam?step=2&amp;{_draft_query(draft)}">→ חזרה לשלב 2</a> · <a href="/exams">ביטול</a></p>
"""
    return shell_html("הוספת מבחן — שלב 3", body, active="/exams")




def page_from_data(d, error=None, open_section=None, saved=False,
                   warn="", field_errors=None):
    """The full planner page from an already-collected data dict
    (see Handler._home_data). Same HTML as page()."""
    return page(data=d, error=error, open_section=open_section,
                saved=saved, warn=warn, field_errors=field_errors)


def _strip_saved(url):
    """Drop a stale ?saved=1 from a return URL. The section pages
    carry next=/topics?saved=1 (and friends) in their forms, so a
    preview page reached from there would otherwise cancel back to
    a page that still claims "נשמר ✓" from an earlier save. A
    wizard step (next=/setup?step=2) has no saved=1 and passes
    through untouched."""
    if not url:
        return url
    return (url.replace("?saved=1&", "?")
               .replace("&saved=1", "")
               .replace("?saved=1", ""))


def _syllabus_preview_page(course_name, course_id, parsed_topics,
                           next_url=None, cancel_url=None):
    esc = html.escape
    lines = "\n".join(parsed_topics)
    cancel = cancel_url or (_strip_saved(next_url) if next_url
                            else "/?open=topics#topics")
    next_field = (f'<input type="hidden" name="next" value="{esc(next_url)}">'
                  if next_url else "")
    # Inside the wizard, the confirm form must say which step it
    # came from, so a rejected save shows its error on that step
    # instead of throwing the user out of the wizard.
    wizard_field = ""
    if next_url and next_url.startswith("/setup"):
        wizard_field = ('<input type="hidden" name="wizard_step"'
                        ' value="2">')
    empty_note = (
        "<p><b>לא נמצאו נושאים — בדוק את הטקסט שהדבקת.</b></p>"
        if not parsed_topics else ""
    )
    return f"""<!doctype html>
<html lang="he" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>אישור נושאים</title>
<style>{SHARED_CSS}</style>
</head><body>
<main role="main">
<div class="card">
<h1>נושאים שנמצאו — {esc(course_name)}</h1>
<p>נמצאו {len(parsed_topics)} נושאים. כל שורה היא נושא אחד. תקן או מחק שורות לפני השמירה, ואז שמור.</p>
{empty_note}
<form method="post" action="/save-topics">
<input type="hidden" name="course_id" value="{course_id}">
{next_field}
{wizard_field}
<textarea name="topics">{esc(lines)}</textarea><br>
<button>שמור נושאים</button></form>
<p><a href="{esc(cancel)}">ביטול — חזרה</a></p>
</div>
</main>
</body></html>"""


def edit_topics_page(conn, course_id, error=None, saved=False):
    """Topic management for one course (/edit-topics): rename a
    topic, delete one topic, set importance and the heavy flag —
    all in one place, instead of scattered links. Delete-all is
    deliberately NOT a one-click link here either: it points at
    the /clear-topics confirmation page, so a careless click
    cannot wipe the course. Returns None when the course does
    not exist."""
    esc = html.escape
    course = conn.execute(
        "SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
    if course is None:
        return None
    topics = conn.execute(
        "SELECT * FROM topics WHERE course_id = ? ORDER BY id",
        (course_id,)).fetchall()
    back = quote(f"/edit-topics?course_id={course_id}", safe="")
    rows = ""
    for t in topics:
        importance = t["importance"] if "importance" in t.keys() else None
        importance_label = IMPORTANCE_HE.get(importance, "לא נקבעה חשיבות")
        buttons = " ".join(
            f'<a class="imp-btn{" imp-current" if value == importance else ""}"'
            f' href="/topic-importance?id={t["id"]}&importance={value}'
            f'&next={back}">'
            f'{"<b>" if value == importance else ""}{label}'
            f'{"</b>" if value == importance else ""}</a>'
            for value, label in
            (("low", "לא מאוד חשוב"), ("medium", "די חשוב"),
             ("high", "מאוד חשוב"))
        )
        heavy = t["heavy"] if "heavy" in t.keys() else 0
        heavy_link = (
            f' <a href="/topic-heavy?id={t["id"]}&heavy=0&next={back}">'
            "נושא כבד ✓ (בטל)</a>"
            if heavy else
            f' <a href="/topic-heavy?id={t["id"]}&heavy=1&next={back}">'
            "סמן כנושא כבד</a>")
        learned_link = (
            f' <a href="/topic-undone?id={t["id"]}&next={back}"'
            ' class="undo-link">בטל סימון</a>'
            if t["learned"]
            else f' <a href="/topic-done?id={t["id"]}&next={back}">'
            "סמן כנלמד</a>")
        rows += (
            "<li>"
            + ("✅ " if t["learned"] else "⬜ ")
            + f'<form method="post" action="/save-topic-title"'
            f' style="display:inline">'
            f'<input type="hidden" name="topic_id" value="{t["id"]}">'
            f'<input type="hidden" name="next" value="/edit-topics'
            f'?course_id={course_id}">'
            f'<input name="title" value="{esc(t["title"])}"'
            f' aria-label="שם הנושא"> '
            "<button>שמור שם</button></form>"
            f" — חשיבות: {importance_label} {buttons}{heavy_link}"
            f"{learned_link}"
            f' <a href="/delete-topic?id={t["id"]}">מחק נושא</a>'
            + "</li>"
        )
    rows = rows or "<li>אין נושאים עדיין — הדבק סילבוס בדף הנושאים.</li>"
    count = len(topics)
    noun = "נושא" if count == 1 else "נושאים"
    saved_note = "<p><b>נשמר ✓</b></p>" if saved else ""
    error_note = (
        f'<p class="error"><b>שגיאה:</b> {esc(error)}</p>' if error else "")
    return f"""<!doctype html>
<html lang="he" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>עריכת נושאים — {esc(course["name"])}</title>
<style>{SHARED_CSS}</style>
</head><body>
<main role="main">
<div class="card">
<h1>עריכת נושאים — {esc(course["name"])}</h1>
<p>{count} {noun} בקורס.</p>
{saved_note}
{error_note}
<ul>{rows}</ul>
<hr>
<p><b>אזור מסוכן:</b> מחיקת כל הנושאים של הקורס היא פעולה
שאי אפשר לבטל — לכן היא דורשת אישור במסך נפרד.</p>
<p><a href="/clear-topics?id={course_id}">מחק את כל הנושאים של הקורס…</a></p>
<p><a href="/topics">← חזרה לנושאים</a></p>
</div>
</main>
</body></html>"""


def _clear_topics_confirm_page(course, topic_count, session_count,
                               exam_count):
    """Confirmation before deleting ALL topics of a course: states
    exactly what will go, so the click that got here by mistake
    can still be walked back. Nothing is deleted by showing this
    page — only the POST from its button deletes."""
    esc = html.escape
    noun = "נושא" if topic_count == 1 else "נושאים"
    return f"""<!doctype html>
<html lang="he" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>מחיקת כל הנושאים — {esc(course["name"])}</title>
<style>{SHARED_CSS}</style>
</head><body>
<main role="main">
<div class="card">
<h1>למחוק את כל הנושאים של {esc(course["name"])}?</h1>
<p>ימחקו <b>{topic_count} {noun}</b>.</p>
<p>הנושאים יוסרו מהיקף של {exam_count} מבחנים, ו־{session_count}
מפגשים מתוכננים שמצביעים עליהם יימחקו והתוכניות ייבנו מחדש.
הקורס עצמו, המבחנים והמטלות נשארים — רק הנושאים הולכים,
ואז אפשר להדביק את הסילבוס מחדש.</p>
<p><b>אי אפשר לבטל את המחיקה.</b></p>
<form method="post" action="/confirm-clear-topics">
<input type="hidden" name="course_id" value="{course["id"]}">
<button>מחק את כל הנושאים</button></form>
<p><a href="/edit-topics?course_id={course["id"]}">ביטול — חזרה לעריכת נושאים</a></p>
</div>
</main>
</body></html>"""


def _delete_topic_confirm_page(topic, course_name, session_count,
                               exam_count):
    """Confirmation before deleting ONE topic: shows its title and
    what references it, so a mistaken click can still be walked
    back. Nothing is deleted by showing this page — only the POST
    from its button deletes."""
    esc = html.escape
    return f"""<!doctype html>
<html lang="he" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>מחיקת נושא — {esc(topic["title"])}</title>
<style>{SHARED_CSS}</style>
</head><body>
<main role="main">
<div class="card">
<h1>למחוק את הנושא “{esc(topic["title"])}”?</h1>
<p>הנושא שייך לקורס {esc(course_name)}.</p>
<p>הנושא יוסר מהיקף של {exam_count} מבחנים, ו־{session_count}
מפגשים מתוכננים שמצביעים עליו יימחקו והתוכניות ייבנו מחדש.
הקורס עצמו, שאר הנושאים, המבחנים והמטלות נשארים.</p>
<p><b>אי אפשר לבטל את המחיקה.</b></p>
<form method="post" action="/confirm-delete-topic">
<input type="hidden" name="topic_id" value="{topic["id"]}">
<button>מחק נושא</button></form>
<p><a href="/edit-topics?course_id={topic["course_id"]}">ביטול — חזרה לעריכת נושאים</a></p>
</div>
</main>
</body></html>"""


def _busy_preview_page(blocks, errors, next_url=None):
    """Confirm page for pasted busy hours (upgrade 9): how many
    valid blocks were found, which lines were bad and why — then
    save only the good ones. Same pattern as the syllabus
    preview: count first, save after."""
    esc = html.escape
    lines = serialize_busy_lines(blocks)
    cancel = _strip_saved(next_url) if next_url else "/?open=busy#busy"
    next_field = (f'<input type="hidden" name="next" value="{esc(next_url)}">'
                  if next_url else "")
    good_rows = "".join(
        f"<li>{esc(b['block_date'])} {esc(b['start_time'])}–"
        f"{esc(b['end_time'])}"
        + (f" — {esc(b['reason'])}" if b["reason"] else "")
        + "</li>"
        for b in blocks
    )
    bad_rows = "".join(
        f"<li>{esc(error)}</li>" for error in errors
    )
    return f"""<!doctype html>
<html lang="he" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>אישור שעות תפוסות</title>
<style>{SHARED_CSS}</style>
</head><body>
<main role="main">
<div class="card">
<h1>שעות תפוסות שנמצאו</h1>
<p>נמצאו {len(blocks)} חסימות תקינות.</p>
{"<ul>" + good_rows + "</ul>" if good_rows else ""}
{"<p><b>שורות שלא נמצאו תקינות — הן לא יישמרו:</b></p><ul class=\"error\">" + bad_rows + "</ul>" if bad_rows else ""}
{"<p><b>לא נמצאו חסימות תקינות — בדוק את הטקסט שהדבקת.</b></p>" if not blocks else ""}
{f'''<form method="post" action="/save-busy">
{next_field}
<textarea name="busy_lines" style="display:none">{esc(lines)}</textarea>
<button>שמור {len(blocks)} חסימות</button></form>''' if blocks else ""}
<p><a href="{esc(cancel)}">ביטול — חזרה</a></p>
</div>
</main>
</body></html>"""


# ---------------------------------------------------------------------------
# Dedicated exam page (/exam?id=...): one place where the exam's
# scope and the topics' importance are chosen together, instead of
# in two different collapsed sections — which is why Daniel could
# not find where to choose topics. Each topic line has its scope
# checkbox AND its importance buttons, and the plan for this exam
# is shown underneath.
# ---------------------------------------------------------------------------

def exam_scope_rows(conn, exam):
    """One line per topic of the exam's course: scope checkbox
    (ticked when the topic is already in scope) plus its importance
    buttons. The buttons link back to this exam page (next=...), so
    setting an importance does not lose the page."""
    esc = html.escape
    scoped_ids = {
        r["topic_id"] for r in conn.execute(
            "SELECT topic_id FROM exam_topics WHERE exam_id = ?",
            (exam["id"],))
    }
    topics = conn.execute(
        "SELECT * FROM topics WHERE course_id = ? ORDER BY id",
        (exam["course_id"],)).fetchall()
    if not topics:
        return (
            '<p class="notice">לקורס הזה עדיין אין נושאים — הוסף אותם '
            'ב<a href="/?open=topics#topics">נושאים (מסילבוס)</a>, '
            'ורק אז אפשר לבחור אותם.</p>'
        )
    back = quote(f"/exam?id={exam['id']}", safe="")
    rows = ""
    for t in topics:
        importance = t["importance"]
        label = IMPORTANCE_HE.get(importance, "לא נקבעה חשיבות")
        buttons = " ".join(
            f'<a class="imp-btn{" imp-current" if value == importance else ""}"'
            f' href="/topic-importance?id={t["id"]}&importance={value}'
            f'&next={back}">'
            f'{"<b>" if value == importance else ""}{button_label}'
            f'{"</b>" if value == importance else ""}</a>'
            for value, button_label in
            (("low", "לא מאוד חשוב"), ("medium", "די חשוב"),
             ("high", "מאוד חשוב"))
        )
        # Heavy topic (upgrade 12): same toggle as on the home
        # page, but it links back here (next=...), so setting it
        # does not lose the exam page.
        heavy = t["heavy"] if "heavy" in t.keys() else 0
        heavy_link = (
            f' <a href="/topic-heavy?id={t["id"]}&heavy=0'
            f'&next={back}">נושא כבד ✓ (בטל)</a>'
            if heavy else
            f' <a href="/topic-heavy?id={t["id"]}&heavy=1'
            f'&next={back}">סמן כנושא כבד</a>')
        rows += (
            f'<label><input type="checkbox" name="topic_id" '
            f'value="{t["id"]}"'
            f'{" checked" if t["id"] in scoped_ids else ""}> '
            f'{esc(t["title"])} — {label}</label> {buttons}{heavy_link}<br>'
        )
    # Same grouping as the creation form, but open: this page
    # already shows only its own course's topics, and choosing
    # the scope is what the page is for, so it must not start
    # hidden behind a collapsed list.
    course = conn.execute(
        "SELECT name FROM courses WHERE id = ?",
        (exam["course_id"],)).fetchone()
    course_name = course["name"] if course else ""
    count = len(topics)
    noun = "נושא" if count == 1 else "נושאים"
    return (
        f'<details class="course-topics" open>'
        f"<summary>{esc(course_name)} — {count} {noun}</summary>"
        f"{rows}</details>"
    )


def exam_page(conn, exam, error=None, saved=False, warn="",
              field_errors=None):
    """The dedicated page for one exam/quiz."""
    esc = html.escape
    course = conn.execute(
        "SELECT name FROM courses WHERE id = ?",
        (exam["course_id"],)).fetchone()
    # QA round 3, fix 9: an exam whose course is gone still says
    # so plainly instead of showing a blank course name.
    course_name = course["name"] if course else "קורס שנמחק"
    kind = "בוחן" if exam["kind"] == "quiz" else "מבחן"
    sessions = get_plan_sessions(conn, exam["id"])
    session_kind_he = {"learn": "למידה", "review": "חזרה",
                       "exercise": "תרגול", "simulation": "סימולציה",
                       "break": "הפסקה"}
    # QA fix 8: sessions grouped by date, condensed like the home
    # page's "בהמשך" view — one summary line per day, details
    # underneath — instead of one flat long list.
    sessions_by_date = {}
    for s in sessions:
        sessions_by_date.setdefault(s["session_date"], []).append(s)
    session_rows = ""
    for session_date in sorted(sessions_by_date):
        day_sessions = sessions_by_date[session_date]
        day = date.fromisoformat(session_date)
        counts = {}
        for s in day_sessions:
            counts[s["kind"]] = counts.get(s["kind"], 0) + 1
        summary = ", ".join(
            f"{count} {session_kind_he.get(kind, kind)}"
            for kind, count in sorted(counts.items()))
        details = ""
        for s in day_sessions:
            kind_text = session_kind_he.get(s["kind"], s["kind"])
            topic_text = (
                s["topic_title"]
                if s["kind"] in ("learn", "exercise", "review")
                and s["topic_title"] else ""
            )
            back = quote(f"/exam?id={exam['id']}", safe="")
            details += (
                f'<li class="session {esc(s["kind"])}">'
                f'<div class="tl-item"><div class="tl-time">'
                f"{esc(s['start_time'])}–{esc(s['end_time'])}</div>"
                f'<div class="tl-body"><span class="tl-kind">'
                f"{esc(kind_text)}</span> "
                + ("✅ " if s["done"] else "⬜ ")
                + (f"<b>{esc(topic_text)}</b>" if topic_text
                   else f"<b>{esc(kind_text)}</b>")
                + (f' <a href="/session-undone?id={s["id"]}'
                   f'&next={back}" class="undo-link">בטל סימון</a>'
                   if s["done"]
                   else f' <a href="/session-done?id={s["id"]}'
                        f'&next={back}" class="done-btn">'
                        f"סמן כבוצע ✓</a>")
                + "</div></div></li>"
            )
        session_rows += (
            f"<p><b>{WEEKDAYS_HE[day.weekday()]} "
            f"{esc(session_date)}</b>: {len(day_sessions)} פריטים "
            f"({summary}), {esc(day_sessions[0]['start_time'])}–"
            f"{esc(day_sessions[-1]['end_time'])}</p>"
            f"<small><ul class=\"timeline\">{details}</ul></small>"
        )
    # QA round 2, fix 7: per-kind session counts, always with all
    # four kinds — including zeros, so a 0%-learn exam cannot hide
    # that review and exercise are zero too.
    exam_counts = {}
    for s in sessions:
        exam_counts[s["kind"]] = exam_counts.get(s["kind"], 0) + 1
    counts_line = (
        "<p>מפגשים לפי סוג: "
        + ", ".join(
            f"{session_kind_he.get(kind, kind)} "
            f"{exam_counts.get(kind, 0)}"
            for kind in ("learn", "review", "exercise", "simulation"))
        + "</p>"
    )
    zero_learn_line = (
        "<p>עם 0% למידה לא יהיו גם מפגשי חזרה ותרגול — שניהם "
        "דורשים נושאים שנלמדו. רק סימולציה תתוכנן.</p>"
        if exam["learn_pct"] == 0 else ""
    )
    # QA round 2, fix 2: no topics in scope at all — say so
    # plainly, in the plan card, instead of an empty list.
    scoped_topics = get_scoped_topics(conn, exam)
    empty_scope_line = (
        "<p><b>אין נושאים בקורס, הוסף נושאים מהסילבוס קודם</b> — "
        "למבחן בלי נושאים אין מה לתכנן.</p>"
        if not scoped_topics else ""
    )
    # QA round 2, fix 5: an exam dated today has no learning day
    # before it, so an empty plan is expected — say why.
    try:
        exam_day = date.fromisoformat(exam["exam_date"])
    except (ValueError, TypeError):
        exam_day = None
    too_close_line = (
        "<p><b>אין מפגשים — המבחן קרוב מדי:</b> המבחן היום, ואין "
        "ימי למידה לפניו. רק חזרה אפשרית.</p>"
        if not sessions and exam_day is not None
        and exam_day <= date.today() and scoped_topics else ""
    )
    # QA round 2, fix 11: an exam whose scope is narrowed keeps
    # it narrowed — but says so, with a pointer to change it.
    total_course_topics = conn.execute(
        "SELECT COUNT(*) FROM topics WHERE course_id = ?",
        (exam["course_id"],)).fetchone()[0]
    narrowed_line = ""
    if (exam["kind"] == "exam" and scoped_topics
            and len(scoped_topics) < total_course_topics):
        narrowed_line = (
            f"<p>ההיקף נשאר מצומצם ל-{len(scoped_topics)} "
            f"נושאים מתוך {total_course_topics} — לשינוי, ערוך "
            "את הנושאים למטה.</p>"
        )
    session_rows = session_rows or (
        "<p>אין מפגשים עדיין — לחץ על "
        "״צור / רענן תוכנית״.</p>"
        if scoped_topics and not too_close_line else ""
    )
    # Unscheduled topics, honestly: same computation as the home
    # page (QA fix 6 — a plan is created right away, so if nothing
    # could be scheduled the exam page must say so, with the
    # reason, instead of a bare "no sessions yet").
    scheduled_topic_ids = {
        s["topic_id"] for s in sessions if s["kind"] == "learn"
    }
    unscheduled = [
        t for t in get_scoped_topics(conn, exam)
        if not t["learned"] and t["id"] not in scheduled_topic_ids
    ]
    reason = unscheduled_reason(exam["learn_pct"], unscheduled)
    unscheduled_html = ""
    if unscheduled:
        if reason == "learning share is 0%":
            reason_text = ("אחוז הלמידה הוא 0% — לא הוקצו מפגשי "
                           "למידה בכלל, לכן הנושאים לא נכנסו. כדי "
                           "לתקן: ערוך את אחוז הלמידה בטופס למעלה "
                           "ליותר מ-0.")
        else:
            reason_text = "אין מספיק זמן פנוי לפני התאריך."
        unscheduled_html = (
            "<p><b>לא נכנסו לתוכנית:</b> "
            + ", ".join(esc(t["title"]) for t in unscheduled)
            + f" — {reason_text}</p>"
        )
    # Countdown + progress (upgrade 5) and the emergency notice
    # (upgrade 11): both sit at the top of the exam page, so the
    # state of this exam is visible before any form.
    progress_html = (
        "<p><b>"
        f"{esc(exam_progress_text(exam_progress(conn, exam)))}</b></p>"
    )
    emergency_html = (
        "<p class=\"notice\"><b>🚨 מצב חירום פעיל:</b> מתוכננים רק "
        "נושאים מאוד חשובים, בלי סימולציה. כדי לחזור לתוכנית "
        "רגילה, בטל את הסימון בטופס למטה.</p>"
        if _topic_value(exam, "emergency", 0) else ""
    )
    # Lead time (new feature): the chosen window is shown plainly
    # on the exam page, next to the countdown, so the plan's start
    # is never a mystery.
    lead_html = (
        "<p>הלמידה מתחילה "
        f"{exam_lead_days(exam)} ימים לפני המבחן.</p>"
    )
    return f"""<!doctype html>
<html lang="he" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(exam["title"])} — מתכנן לימודים</title>
<style>{SHARED_CSS}</style>
</head><body>
{nav_html(active="/exams")}
<main role="main">
<div class="card">
<h1>{esc(exam["title"])}</h1>
<p>{kind} — {esc(course_name)} — {esc(exam["exam_date"])}</p>
{progress_html}
{emergency_html}
{lead_html}
<p>חלוקת הכנה: למידה {exam["learn_pct"]}%, חזרה {exam["review_pct"]}%, תרגול {exam["exercise_pct"]}%, סימולציה {exam["simulation_pct"]}%</p>
<p><a href="/">← חזרה למתכנן</a> · <a href="/generate-plan?exam_id={exam["id"]}">צור / רענן תוכנית</a> · <a href="/export-exam.ics?exam_id={exam["id"]}">הורד כקובץ יומן (.ics)</a> · <a href="/delete-exam?id={exam["id"]}">מחק מבחן</a></p>
{status_html(error=error, saved=saved, warn=warn)}
{narrowed_line}
<h2><span class="step-badge">שלב 1</span> עריכת המבחן</h2>
<p>כאן משנים שם, תאריך, סוג ואחוזי הכנה. השמירה בודקת בדיוק כמו ביצירה — תאריך אמיתי, אחוזים שמסתכמים ב-100, ולבוחן לפחות נושא אחד — ובונה את התוכנית מחדש.</p>
<form method="post" action="/save-exam">
<input type="hidden" name="exam_id" value="{exam["id"]}">
<div class="field"><label for="edit_exam_title">שם המבחן / הבוחן</label>
<input id="edit_exam_title" name="title" value="{esc(exam["title"])}" required{field_attrs(field_errors, "title")}>
<p class="help">שם קצר שתזהה מיד, למשל "מבחן אמצע בחשבון 2".</p>
{field_error_html(field_errors, "title")}</div>
<div class="field"><label for="edit_exam_date">תאריך</label>
<input id="edit_exam_date" name="exam_date" type="date" value="{esc(exam["exam_date"])}" required{field_attrs(field_errors, "exam_date")}>
<p class="help">תאריך אמיתי מהיום והלאה — אי אפשר לתזמן מבחן בעבר.</p>
{field_error_html(field_errors, "exam_date")}</div>
<div class="field"><label for="edit_exam_kind">סוג</label>
<select id="edit_exam_kind" name="kind"{field_attrs(field_errors, "kind")}>
<option value="exam"{" selected" if exam["kind"] != "quiz" else ""}>מבחן</option>
<option value="quiz"{" selected" if exam["kind"] == "quiz" else ""}>בוחן</option>
</select>
<p class="help">בוחן = חלק מהחומר (חובה לסמן נושאים למטה). מבחן = כל הקורס, אלא אם צמצמת למטה.</p>
{field_error_html(field_errors, "kind")}</div>
<label>למידה: <input name="learn_pct" type="number" min="0" max="100" value="{exam["learn_pct"]}" style="width:4em"{field_attrs(field_errors, "learn_pct")}>%</label>
<label>חזרה: <input name="review_pct" type="number" min="0" max="100" value="{exam["review_pct"]}" style="width:4em">%</label>
<label>תרגול: <input name="exercise_pct" type="number" min="0" max="100" value="{exam["exercise_pct"]}" style="width:4em">%</label>
<label>סימולציה: <input name="simulation_pct" type="number" min="0" max="100" value="{exam["simulation_pct"]}" style="width:4em">%</label>
<p>ארבעת האחוזים חייבים להסתכם ב-100.</p>
{field_error_html(field_errors, "learn_pct")}
<label>להתחיל ללמוד <input name="lead_days" type="number" min="1" max="30" value="{exam_lead_days(exam)}" style="width:4em"{field_attrs(field_errors, "lead_days")}> ימים לפני המבחן (1–30, ברירת המחדל 14 — אף פעם לא יותר מחודש מראש).</label>
{field_error_html(field_errors, "lead_days")}
<label><input type="checkbox" name="emergency" value="1"{' checked' if _topic_value(exam, "emergency", 0) else ''}> מצב חירום — רק נושאים מאוד חשובים, בלי סימולציה</label>
<button>שמור שינויים ובנה תוכנית מחדש</button></form>
<h2><span class="step-badge">שלב 2</span> אילו נושאים בחומר, ומה חשוב יותר</h2>
<p>סמן ב-V את הנושאים שנכנסים {("לבוחן" if exam["kind"] == "quiz" else "למבחן")}, ובחר לכל נושא כמה הוא חשוב. נושאים חשובים יותר מתוכננים קודם, ופחות חשובים נשארים בחוץ קודם כשאין מספיק זמן.</p>
<form method="post" action="/save-exam-scope">
<input type="hidden" name="exam_id" value="{exam["id"]}">
{exam_scope_rows(conn, exam)}
{field_error_html(field_errors, "topic_id")}
<button>שמור נושאים וצור תוכנית</button></form>
</div>
<div class="card">
<h2>התוכנית של {esc(exam["title"])}</h2>
{counts_line}
{zero_learn_line}
{empty_scope_line}
{too_close_line}
{session_rows}
{unscheduled_html}
</div>
</main>
</body></html>"""


# ---------------------------------------------------------------------------
# Dedicated assignment page (/assignment?id=...): one place to edit
# an assignment (QA fix 5) — name, due date, hours, parts — with
# the same validation as creation, plus a delete link.
# ---------------------------------------------------------------------------

def assignment_page(conn, assignment, error=None, field_errors=None):
    """The dedicated page for one assignment: edit form + blocks."""
    esc = html.escape
    course = conn.execute(
        "SELECT name FROM courses WHERE id = ?",
        (assignment["course_id"],)).fetchone()
    course_name = course["name"] if course else ""
    blocks = get_assignment_blocks(conn, assignment["id"])
    block_rows = "".join(
        f"<li>{esc(b['part_label'])}: {esc(b['block_date'])}"
        f" {esc(b['start_time'])}–{esc(b['end_time'])}</li>"
        for b in blocks
    ) or "<li>אין בלוקים מתוזמנים עדיין.</li>"
    return f"""<!doctype html>
<html lang="he" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(assignment["title"])} — מתכנן לימודים</title>
<style>{SHARED_CSS}</style>
</head><body>
{nav_html()}
<main role="main">
<div class="card">
<h1>{esc(assignment["title"])}</h1>
<p>מטלה — {esc(course_name)}{" — בוצעה ✅" if assignment["done"] else ""}</p>
<p><a href="/">← חזרה למתכנן</a> · <a href="/delete-assignment?id={assignment["id"]}">מחק מטלה</a>{f' · <a href="/undone?id={assignment["id"]}&next={quote(f"/assignment?id={assignment["id"]}", safe="")}">בטל סימון</a>' if assignment["done"] else ""}</p>
{status_html(error=error)}
<h2>עריכת המטלה</h2>
<p>השמירה בודקת בדיוק כמו ביצירה — תאריך הגשה אמיתי מהיום והלאה — ומתזמנת את המטלה מחדש, כדי שהזמן שהתפנה או זז ינוצל מיד.</p>
<form method="post" action="/save-assignment">
<input type="hidden" name="assignment_id" value="{assignment["id"]}">
<div class="field"><label for="edit_assignment_title">שם המטלה</label>
<input id="edit_assignment_title" name="title" value="{esc(assignment["title"])}" required{field_attrs(field_errors, "title")}>
<p class="help">שם קצר של המטלה, למשל "תרגיל 3 בחשבון".</p>
{field_error_html(field_errors, "title")}</div>
<div class="field"><label for="edit_assignment_due">תאריך הגשה</label>
<input id="edit_assignment_due" name="due_date" type="date" value="{esc(assignment["due_date"] or "")}"{field_attrs(field_errors, "due_date")}>
<p class="help">תאריך הגשה אמיתי מהיום והלאה.</p>
{field_error_html(field_errors, "due_date")}</div>
<div class="field"><label for="edit_assignment_hours">זמן משוער (שעות)</label>
<input id="edit_assignment_hours" name="estimated_hours" type="number" min="0.5" step="0.5" value="{assignment["estimated_hours"] if assignment["estimated_hours"] else ""}"{field_attrs(field_errors, "estimated_hours")}>
<p class="help">כמה שעות המטלה תיקח בסך הכול — כל שאלה מקבלת בלוק רצוף משלה.</p>
{field_error_html(field_errors, "estimated_hours")}</div>
<div class="field"><label for="edit_assignment_parts">מספר שאלות / חלקים</label>
<input id="edit_assignment_parts" name="num_parts" type="number" min="1" step="1" value="{assignment["num_parts"] if assignment["num_parts"] else ""}">
<p class="help">כמה שאלות או חלקים יש במטלה.</p></div>
<button>שמור שינויים</button></form>
</div>
<div class="card">
<h2>הבלוקים המתוזמנים</h2>
<ul>{block_rows}</ul>
</div>
</main>
</body></html>"""


# ---------------------------------------------------------------------------
# Setup wizard (Fix 3): first-run onboarding, one thing per screen.
# Plain forms + redirects only, no JavaScript. Every step saves with
# the existing handlers (the forms carry a hidden "next" field telling
# the handler where to redirect), has a "skip" link to the next step,
# and a "back to planner" link. Step 6 is a summary, not a form.
# ---------------------------------------------------------------------------

SETUP_STEPS = {
    1: "קורסים",
    2: "נושאים מסילבוס",
    3: "חלונות פנויים",
    4: "הרוטינה שלי",
    5: "מבחן או מטלה ראשונים",
    6: "סיימנו!",
}


def setup_page(conn, step, error=None, saved=False):
    """One wizard step. Step is clamped to 1..6. saved=True shows
    "נשמר ✓" at the top, so a save that just happened is visible
    instead of the page silently reloading."""
    esc = html.escape
    try:
        step = int(step)
    except (TypeError, ValueError):
        step = 1
    step = max(1, min(6, step))
    courses = get_courses(conn)
    topics = get_topics(conn)
    free_slots = get_free_slots(conn)
    prefs = get_prefs(conn)
    exams = get_exams(conn)
    assignments = get_assignments(conn)
    course_options = "".join(
        f'<option value="{c["id"]}">{esc(c["name"])}</option>'
        for c in courses
    )
    weekday_options = weekday_options_html()
    study_style = (prefs["study_style"]
                   if prefs and "study_style" in prefs.keys() else None)
    exam_default = (date.today() + timedelta(days=14)).isoformat()
    assignment_default = (date.today() + timedelta(days=7)).isoformat()
    next_step = step + 1 if step < 6 else None
    skip_href = f"/setup?step={next_step}" if next_step else "/"
    skip_label = (f"דלג לשלב {next_step} ←" if next_step
                  else "דלג למתכנן ←")
    back_link = (f'<a href="/setup?step={step - 1}">→ חזרה לשלב {step - 1}</a>'
                 if step > 1 else "")

    if step == 1:
        rows = "".join(
            f"<li><b>{esc(c['name'])}</b> — {esc(c['teacher'])}</li>"
            for c in courses
        ) or "<li>אין קורסים עדיין.</li>"
        body = f"""
<p>שלושת הקורסים שלך כבר כאן. אפשר להוסיף עוד קורס, או להמשיך.</p>
<ul>{rows}</ul>
<form method="post" action="/add-course">
<input type="hidden" name="next" value="/setup?step=2">
<input type="hidden" name="wizard_step" value="1">
<div class="field"><label for="w_course_name">שם הקורס</label>
<input id="w_course_name" name="name" placeholder="שם הקורס" required></div>
<div class="field"><label for="w_course_teacher">מרצה (לא חובה)</label>
<input id="w_course_teacher" name="teacher" placeholder="מרצה (לא חובה)"></div>
<button>הוסף קורס</button></form>
<p><a href="/setup?step=2">המשך לשלב 2: נושאים מסילבוס ←</a></p>"""
    elif step == 2:
        rows = ""
        for course in courses:
            course_topics = [t for t in topics
                             if t["course_id"] == course["id"]]
            items = "".join(
                f"<li>{esc(t['title'])}</li>" for t in course_topics
            ) or "<li>אין נושאים עדיין.</li>"
            rows += f"<h3>{esc(course['name'])}</h3><ul>{items}</ul>"
        body = f"""
<p>הדבק את הסילבוס של קורס — כל שורה תהפוך לנושא אחד. תאשר את
הרשימה במסך הבא לפני השמירה. אפשר גם להוסיף נושא בודד.</p>
{rows}
<form method="post" action="/parse-syllabus">
<input type="hidden" name="next" value="/setup?step=3">
<input type="hidden" name="wizard_step" value="2">
<div class="field"><label for="w_syllabus_course">קורס</label>
<select id="w_syllabus_course" name="course_id">{course_options}</select></div>
<div class="field"><label for="w_syllabus_text">טקסט הסילבוס — כל שורה תהפוך לנושא אחד</label>
<textarea id="w_syllabus_text" name="syllabus" placeholder="הדבק כאן את טקסט הסילבוס"></textarea></div>
<button>נתח סילבוס</button></form>
<form method="post" action="/add-topic">
<input type="hidden" name="next" value="/setup?step=3">
<input type="hidden" name="wizard_step" value="2">
<div class="field"><label for="w_topic_course">קורס</label>
<select id="w_topic_course" name="course_id">{course_options}</select></div>
<div class="field"><label for="w_topic_title">נושא בודד</label>
<input id="w_topic_title" name="title" placeholder="נושא בודד" required></div>
<button>הוסף נושא</button></form>
<p><a href="/setup?step=3">המשך לשלב 3: חלונות פנויים ←</a></p>"""
    elif step == 3:
        rows = "".join(
            f"<li>{weekday_name(s['weekday'])} {esc(s['start_time'])}–"
            f"{esc(s['end_time'])}</li>"
            for s in sorted(free_slots, key=sunday_first_key)
        ) or "<li>אין חלונות פנויים עדיין.</li>"
        body = f"""
<p>מתי אתה באמת פנוי ללמוד בשבוע? הוסף חלון אחד בכל פעם — למשל
יום שני 09:00–12:00. התוכנית תיבנה רק בתוך החלונות האלה.</p>
<ul>{rows}</ul>
<form method="post" action="/add-slot">
<input type="hidden" name="next" value="/setup?step=4">
<input type="hidden" name="wizard_step" value="3">
<div class="field"><label for="w_slot_weekday">יום בשבוע</label>
<select id="w_slot_weekday" name="weekday">{weekday_options}</select></div>
<div class="field"><label for="w_slot_start">שעת התחלה</label>
<input id="w_slot_start" name="start_time" type="time" required></div>
<div class="field"><label for="w_slot_end">שעת סיום</label>
<input id="w_slot_end" name="end_time" type="time" required></div>
<button>הוסף חלון פנוי</button></form>
<p><a href="/setup?step=4">המשך לשלב 4: הרוטינה ←</a></p>"""
    elif step == 4:
        body = f"""
<p>ארבע שאלות קצרות על איך אתה לומד. אפשר להשאיר ריק — אז התוכנית
תשתמש בברירת המחדל: מפגש של 50 דקות, הפסקה של 10 דקות, וערבוב
מקצועות באותו יום.</p>
<form method="post" action="/save-prefs">
<input type="hidden" name="next" value="/setup?step=5">
<input type="hidden" name="wizard_step" value="4">
<div class="field"><label for="w_session_minutes">אורך מפגש למידה (דקות)</label>
<input id="w_session_minutes" name="session_minutes" type="number" min="15" max="180" value="{prefs["session_minutes"] if prefs and prefs["session_minutes"] else ""}" placeholder="למשל 50"></div>
<div class="field"><label for="w_break_minutes">אורך הפסקה (דקות)</label>
<input id="w_break_minutes" name="break_minutes" type="number" min="0" max="60" value="{prefs["break_minutes"] if prefs and prefs["break_minutes"] is not None else ""}" placeholder="למשל 10"></div>
<div class="field"><label for="w_homework_pref">שיעורי בית עדיף</label>
<select id="w_homework_pref" name="homework_pref">
<option value=""{" selected" if not prefs or not prefs["homework_pref"] else ""}>— לא נבחר —</option>
<option value="any"{" selected" if prefs and prefs["homework_pref"] == "any" else ""}>אין העדפה</option>
<option value="morning"{" selected" if prefs and prefs["homework_pref"] == "morning" else ""}>בבוקר</option>
<option value="evening"{" selected" if prefs and prefs["homework_pref"] == "evening" else ""}>בערב</option>
</select></div>
<div class="field"><label for="w_study_style">איך אתה מעדיף ללמוד ביום אחד</label>
<select id="w_study_style" name="study_style">
<option value="variety"{" selected" if study_style in (None, "variety") else ""}>מערבבים מקצועות באותו יום (בבוקר מקצוע אחד, אחר כך מקצוע אחר)</option>
<option value="focus"{" selected" if study_style == "focus" else ""}>מתמקדים במקצוע אחד ביום (בוסט למקצוע בודד)</option>
</select></div>
<button>שמור רוטינה והמשך ←</button></form>"""
    elif step == 5:
        if topics:
            checkboxes = exam_topic_checkboxes(courses, topics)
        else:
            checkboxes = (
                '<p class="notice">עדיין אין נושאים — קודם מוסיפים '
                'נושאים ב<a href="/setup?step=2">שלב 2</a>, ורק אז '
                'אפשר לבחור אותם לבוחן.</p>'
            )
        body = f"""
<p>מבחן או בוחן אחד (לבוחן חובה לסמן נושאים), או מטלה אחת — מה
שדחוף לך עכשיו. השאר אפשר להוסיף אחר כך מהמתכנן עצמו.</p>
<form method="post" action="/add-exam">
<input type="hidden" name="next" value="/setup?step=6">
<input type="hidden" name="wizard_step" value="5">
<div class="field"><label for="w_exam_course">קורס</label>
<select id="w_exam_course" name="course_id">{course_options}</select></div>
<div class="field"><label for="w_exam_kind">סוג</label>
<select id="w_exam_kind" name="kind"><option value="exam">מבחן</option><option value="quiz">בוחן</option></select></div>
<div class="field"><label for="w_exam_title">שם המבחן / הבוחן</label>
<input id="w_exam_title" name="title" placeholder="שם המבחן / הבוחן" required></div>
<div class="field"><label for="w_exam_date">תאריך</label>
<input id="w_exam_date" name="exam_date" type="date" required value="{exam_default}"></div>
<label>למידה: <input name="learn_pct" type="number" min="0" max="100" value="40" style="width:4em">%</label>
<label>חזרה: <input name="review_pct" type="number" min="0" max="100" value="20" style="width:4em">%</label>
<label>תרגול: <input name="exercise_pct" type="number" min="0" max="100" value="20" style="width:4em">%</label>
<label>סימולציה: <input name="simulation_pct" type="number" min="0" max="100" value="20" style="width:4em">%</label>
<p>ארבעת האחוזים חייבים להסתכם ב-100.</p>
<label>להתחיל ללמוד <input name="lead_days" type="number" min="1" max="30" value="14" style="width:4em"> ימים לפני המבחן (1–30, ברירת המחדל 14 — אף פעם לא יותר מחודש מראש).</label>
<label><input type="checkbox" name="emergency" value="1"> מצב חירום — רק נושאים מאוד חשובים, בלי סימולציה</label>
<p>בחר נושאים: לבוחן חובה לסמן אילו נושאים בחומר. למבחן אפשר להשאיר ריק — ברירת המחדל היא כל הקורס.</p>
{checkboxes}
<button>הוסף מבחן / בוחן והמשך ←</button></form>
<form method="post" action="/add-assignment">
<input type="hidden" name="next" value="/setup?step=6">
<input type="hidden" name="wizard_step" value="5">
<div class="field"><label for="w_assignment_course">קורס</label>
<select id="w_assignment_course" name="course_id">{course_options}</select></div>
<div class="field"><label for="w_assignment_title">שם המטלה</label>
<input id="w_assignment_title" name="title" placeholder="שם המטלה" required></div>
<div class="field"><label for="w_assignment_due">תאריך הגשה</label>
<input id="w_assignment_due" name="due_date" type="date" value="{assignment_default}"></div>
<div class="field"><label for="w_assignment_hours">זמן משוער (שעות)</label>
<input id="w_assignment_hours" name="estimated_hours" type="number" min="0.5" step="0.5" placeholder="שעות (למשל 4)"></div>
<div class="field"><label for="w_assignment_parts">מספר שאלות / חלקים</label>
<input id="w_assignment_parts" name="num_parts" type="number" min="1" step="1" placeholder="מספר שאלות/חלקים"></div>
<button>הוסף מטלה והמשך ←</button></form>"""
    else:  # step 6 — summary, no form
        routine_done = bool(
            prefs and prefs["session_minutes"]
            and prefs["break_minutes"] is not None
            and prefs["homework_pref"] in ("morning", "evening", "any"))
        body = f"""
<p>זה מה שהגדרת עד עכשיו:</p>
<ul>
<li>{len(courses)} קורסים</li>
<li>{len(topics)} נושאים</li>
<li>{len(free_slots)} חלונות פנויים בשבוע</li>
<li>רוטינה: {"הוגדרה" if routine_done else "לא הוגדרה עדיין — אפשר להשלים אותה מהמתכנן"}</li>
<li>{len(exams)} מבחנים / בחנים</li>
<li>{len(assignments)} מטלות</li>
</ul>
<p>כל מה שחסר אפשר להוסיף אחר כך מהמתכנן עצמו. התוכנית נבנית
מהנושאים ומהחלונות שהגדרת — פתח את "מה עושים היום" למעלה.</p>
<p><a href="/">למתכנן ←</a></p>"""
    return f"""<!doctype html>
<html lang="he" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>אשף ההתחלה — שלב {step} מתוך 6</title>
<style>{SHARED_CSS}</style>
</head><body>
<main role="main">
<div class="card">
<h1>🧭 אשף ההתחלה — שלב {step} מתוך 6: {SETUP_STEPS[step]}</h1>
{f'<p class="error"><b>שגיאה:</b> {esc(error)}</p>' if error else ""}
{f'<p class="notice">נשמר ✓</p>' if saved else ""}
{body}
<p>{back_link} <a href="{skip_href}">{skip_label}</a> · <a href="/">חזרה למתכנן</a></p>
</div>
</main>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, body, status=200):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_text(self, body, content_type, filename=None):
        """Send a non-HTML body — the .ics download (upgrade 8):
        plain text, with an attachment filename so the browser
        downloads the file instead of showing it."""
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if filename:
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)

    def _redirect_to(self, url):
        """303 redirect. Only local paths are allowed, so a form
        cannot send the user to another site."""
        if not url.startswith("/") or url.startswith("//"):
            url = "/"
        self.send_response(303)
        self.send_header("Location", url)
        self.end_headers()

    def _redirect_home(self, section=None, saved=False, warn=None):
        # Fix 2: after a save, go back to the section that was just
        # used (/?open=exams#exams), so the page does not jump to
        # the top. No section means plain "/" (top). saved=True
        # shows "נשמר ✓" on the page (QA round 2, fix 10); warn
        # shows a warning notice (fixes 5, 7, 12).
        self._redirect_to(redirect_location(section, saved=saved,
                                            warn=warn))

    def _exam_error_page(self, conn, value, message):
        """Where a rejected exam shows its error: back on wizard
        step 5 if the form came from there, otherwise the home page
        with the exams section open (Fix 2), so the error is seen."""
        if value("wizard_step") == "5":
            return setup_page(conn, 5, error=message)
        return self._home_page(conn, error=message, open_section="exams")

    def _home_data(self, conn, open_section=None, topic_back=None):
        """The full data dict every page reads (see
        collect_page_data): the lists, today's items, the coming
        days, the attention box and the per-exam summaries. The
        dashboard, the full planner page and every section page all
        read this one dict, so they can never disagree. topic_back
        is passed through only for the standalone /topics page, so
        its buttons return there."""
        courses = get_courses(conn)
        assignments = get_assignments(conn)
        topics = get_topics(conn)
        free_slots = get_free_slots(conn)
        exams = get_exams(conn)
        sessions_by_exam = {}
        unscheduled_by_exam = {}
        session_counts_by_exam = {}
        unscheduled_reason_by_exam = {}
        empty_scope_by_exam = {}
        for exam in exams:
            sessions = get_plan_sessions(conn, exam["id"])
            sessions_by_exam[exam["id"]] = sessions
            counts = {}
            for s in sessions:
                counts[s["kind"]] = counts.get(s["kind"], 0) + 1
            session_counts_by_exam[exam["id"]] = counts
            scheduled_topic_ids = {
                s["topic_id"] for s in sessions if s["kind"] == "learn"
            }
            scoped = get_scoped_topics(conn, exam)
            empty_scope_by_exam[exam["id"]] = not scoped
            unscheduled = [
                t for t in scoped
                if not t["learned"] and t["id"] not in scheduled_topic_ids
            ]
            unscheduled_by_exam[exam["id"]] = unscheduled
            unscheduled_reason_by_exam[exam["id"]] = unscheduled_reason(
                exam["learn_pct"], unscheduled)
        exam_progress_by_exam = {
            exam["id"]: exam_progress(conn, exam) for exam in exams
        }
        prefs = get_prefs(conn)
        blocks_by_assignment = {}
        for block in get_all_assignment_blocks(conn):
            blocks_by_assignment.setdefault(
                block["assignment_id"], []).append(block)
        return collect_page_data(
            courses, assignments, topics, free_slots, exams,
            sessions_by_exam, unscheduled_by_exam, prefs=prefs,
            assignment_blocks_by_assignment=blocks_by_assignment,
            busy_blocks=get_busy_blocks(conn),
            session_counts_by_exam=session_counts_by_exam,
            unscheduled_reason_by_exam=unscheduled_reason_by_exam,
            open_section=open_section,
            empty_scope_by_exam=empty_scope_by_exam,
            exam_progress_by_exam=exam_progress_by_exam,
            topic_back=topic_back)

    def _home_page(self, conn, error=None, open_section=None,
                   saved=False, warn="", field_errors=None):
        """The full planner page (today first, then every section),
        used when ?open=<section> names a section — after a save or
        an error — so the user lands where they just worked."""
        return page_from_data(
            self._home_data(conn, open_section), error=error,
            open_section=open_section, saved=saved, warn=warn,
            field_errors=field_errors)

    def _dashboard(self, conn, error=None, saved=False, warn=""):
        """The plain home page: a daily dashboard only — today
        first, the rest of the week condensed, nothing else."""
        return dashboard_page(
            self._home_data(conn), error=error, saved=saved, warn=warn)

    def _section_page(self, conn, section, error=None, saved=False,
                      warn="", field_errors=None):
        """One section on its own page. The topic buttons return to
        /topics when that is the page being shown."""
        return section_page(
            self._home_data(conn,
                            topic_back="/topics" if section == "topics"
                            else None),
            section, error=error, saved=saved,
            warn=warn, field_errors=field_errors)

    def _post_error_page(self, conn, value, message, field_errors=None,
                         open_section=None):
        """Where a rejected save shows its error: back on the
        section page the form lives on (the error under its field),
        or on the full planner page when the form lives there. The
        message is the same Hebrew error either way. A form sent
        from the wizard stays in the wizard: the error is shown on
        the step the form came from (wizard_step), so a bad value
        never throws the user out of the setup flow."""
        next_url = value("next")
        wizard_step = value("wizard_step")
        if wizard_step in ("1", "2", "3", "4", "5", "6"):
            return setup_page(conn, int(wizard_step), error=message)
        section = SECTION_ROUTES.get(urlparse(next_url).path) \
            if next_url else None
        if section:
            return self._section_page(conn, section, error=message,
                                      field_errors=field_errors)
        return self._home_page(conn, error=message,
                               open_section=open_section,
                               field_errors=field_errors)

    def _redirect_next(self, query, fallback_section):
        """A link may carry next=/somewhere (e.g. back to the exam
        page after setting an importance there). Local paths only;
        otherwise fall back to the section redirect."""
        next_url = query.get("next", [""])[0]
        if next_url.startswith("/") and not next_url.startswith("//"):
            self._redirect_to(next_url)
        else:
            self._redirect_home(fallback_section)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/topic-importance":
            query = parse_qs(parsed.query)
            topic_id = query.get("id", [""])[0]
            importance = query.get("importance", [""])[0]
            if importance not in ("low", "medium", "high"):
                importance = None
            with connect() as conn:
                conn.execute(
                    "UPDATE topics SET importance = ? WHERE id = ?",
                    (importance, topic_id),
                )
            self._redirect_next(query, "topics")
            return
        if parsed.path == "/delete-busy":
            busy_id = parse_qs(parsed.query).get("id", [""])[0]
            with connect() as conn:
                conn.execute("DELETE FROM busy_blocks WHERE id = ?",
                             (busy_id,))
            self._redirect_home("busy")
            return
        if parsed.path == "/done":
            query = parse_qs(parsed.query)
            assignment_id = query.get("id", [""])[0]
            with connect() as conn:
                mark_assignment_done(conn, assignment_id)
            self._redirect_next(query, "assignments")
            return
        if parsed.path == "/undone":
            query = parse_qs(parsed.query)
            assignment_id = query.get("id", [""])[0]
            with connect() as conn:
                mark_assignment_undone(conn, assignment_id)
            self._redirect_next(query, "assignments")
            return
        if parsed.path == "/schedule-assignment":
            assignment_id = parse_qs(parsed.query).get("id", [""])[0]
            with connect() as conn:
                # Full cascade: the assignment is rescheduled in
                # due-date order with the rest, then the exam plans
                # are rebuilt around it.
                reschedule_everything(conn)
            self._redirect_home("assignments")
            return
        if parsed.path == "/topic-done":
            query = parse_qs(parsed.query)
            topic_id = query.get("id", [""])[0]
            with connect() as conn:
                conn.execute(
                    "UPDATE topics SET learned = 1 WHERE id = ?", (topic_id,)
                )
            self._redirect_next(query, "topics")
            return
        if parsed.path == "/topic-undone":
            query = parse_qs(parsed.query)
            topic_id = query.get("id", [""])[0]
            with connect() as conn:
                conn.execute(
                    "UPDATE topics SET learned = 0 WHERE id = ?", (topic_id,)
                )
            self._redirect_next(query, "topics")
            return
        if parsed.path == "/session-done":
            query = parse_qs(parsed.query)
            session_id = query.get("id", [""])[0]
            with connect() as conn:
                conn.execute(
                    "UPDATE plan_sessions SET done = 1 WHERE id = ?", (session_id,)
                )
                # Finishing a learning session means its topic is learned.
                conn.execute(
                    """
                    UPDATE topics SET learned = 1 WHERE id IN
                    (SELECT topic_id FROM plan_sessions WHERE id = ?)
                    """,
                    (session_id,),
                )
            self._redirect_next(query, "today")
            return
        if parsed.path == "/session-undone":
            query = parse_qs(parsed.query)
            session_id = query.get("id", [""])[0]
            with connect() as conn:
                mark_session_undone(conn, session_id)
            self._redirect_next(query, "today")
            return
        if parsed.path == "/delete-slot":
            slot_id = parse_qs(parsed.query).get("id", [""])[0]
            with connect() as conn:
                conn.execute("DELETE FROM free_slots WHERE id = ?", (slot_id,))
                # QA round 3, fix 3: sessions planned in the
                # deleted window must move — rebuild everything,
                # like deleting an exam or an assignment does.
                reschedule_everything(conn)
            self._redirect_home("slots")
            return
        if parsed.path == "/delete-course":
            course_id = parse_qs(parsed.query).get("id", [""])[0]
            with connect() as conn:
                delete_course(conn, course_id)
                # QA round 3, fix 4: the freed time is reused
                # right away by the other exams and assignments
                # instead of sitting empty until the next rebuild.
                reschedule_everything(conn)
            self._redirect_home("courses")
            return
        if parsed.path == "/edit-topics":
            # Topic management for one course: rename, delete one,
            # importance, heavy flag. Missing course → back to
            # the topics page.
            query = parse_qs(parsed.query)
            course_id = query.get("course_id", [""])[0]
            saved = query.get("saved", [""])[0] == "1"
            with connect() as conn:
                html_page = edit_topics_page(conn, course_id, saved=saved)
            if html_page is None:
                self._redirect_to("/topics")
            else:
                self._send(html_page)
            return
        if parsed.path == "/clear-topics":
            # Confirmation page ONLY: a GET never deletes anything
            # any more — one careless click on the old direct link
            # used to wipe the whole course. The button on this
            # page POSTs to /confirm-clear-topics.
            query = parse_qs(parsed.query)
            course_id = query.get("id", [""])[0]
            with connect() as conn:
                course = conn.execute(
                    "SELECT * FROM courses WHERE id = ?",
                    (course_id,)).fetchone()
                if course is None:
                    self._redirect_to("/topics")
                    return
                topic_ids = [r["id"] for r in conn.execute(
                    "SELECT id FROM topics WHERE course_id = ?",
                    (course_id,))]
                if topic_ids:
                    placeholders = ",".join("?" for _ in topic_ids)
                    session_count = conn.execute(
                        "SELECT COUNT(*) FROM plan_sessions"
                        f" WHERE topic_id IN ({placeholders})",
                        topic_ids).fetchone()[0]
                    exam_count = conn.execute(
                        "SELECT COUNT(DISTINCT exam_id) FROM exam_topics"
                        f" WHERE topic_id IN ({placeholders})",
                        topic_ids).fetchone()[0]
                else:
                    session_count = 0
                    exam_count = 0
                self._send(_clear_topics_confirm_page(
                    course, len(topic_ids), session_count, exam_count))
            return
        if parsed.path == "/delete-topic":
            # Confirmation page ONLY, same rule as /clear-topics:
            # showing it deletes nothing; the POST confirms.
            query = parse_qs(parsed.query)
            topic_id = query.get("id", [""])[0]
            with connect() as conn:
                topic = conn.execute(
                    "SELECT * FROM topics WHERE id = ?",
                    (topic_id,)).fetchone()
                if topic is None:
                    self._redirect_to("/topics")
                    return
                course = conn.execute(
                    "SELECT name FROM courses WHERE id = ?",
                    (topic["course_id"],)).fetchone()
                session_count = conn.execute(
                    "SELECT COUNT(*) FROM plan_sessions WHERE topic_id = ?",
                    (topic_id,)).fetchone()[0]
                exam_count = conn.execute(
                    "SELECT COUNT(DISTINCT exam_id) FROM exam_topics"
                    " WHERE topic_id = ?",
                    (topic_id,)).fetchone()[0]
                self._send(_delete_topic_confirm_page(
                    topic, course["name"] if course else "",
                    session_count, exam_count))
            return
        if parsed.path == "/delete-exam":
            exam_id = parse_qs(parsed.query).get("id", [""])[0]
            with connect() as conn:
                delete_exam(conn, exam_id)
                # The freed time is reused right away, like when
                # an assignment is marked done (QA fix 5).
                reschedule_everything(conn)
            self._redirect_home("exams")
            return
        if parsed.path == "/delete-assignment":
            assignment_id = parse_qs(parsed.query).get("id", [""])[0]
            with connect() as conn:
                delete_assignment(conn, assignment_id)
                reschedule_everything(conn)
            self._redirect_home("assignments")
            return
        if parsed.path == "/assignment":
            assignment_id = parse_qs(parsed.query).get("id", [""])[0]
            with connect() as conn:
                assignment = conn.execute(
                    "SELECT * FROM assignments WHERE id = ?",
                    (assignment_id,)).fetchone()
                if assignment is None:
                    self._redirect_home("assignments")
                    return
                self._send(assignment_page(conn, assignment))
            return
        if parsed.path == "/generate-plan":
            exam_id = parse_qs(parsed.query).get("exam_id", [""])[0]
            with connect() as conn:
                if regenerate_exam_plan(conn, exam_id) is None:
                    self._redirect_home("exams")
                    return
            self._redirect_home("summaries")
            return
        if parsed.path == "/export-plan.ics":
            # The whole plan as a calendar file (upgrade 8):
            # sessions and assignment blocks, not done yet.
            with connect() as conn:
                events = collect_plan_events(conn)
            self._send_text(build_ics(events), "text/calendar",
                            filename="study-plan.ics")
            return
        if parsed.path == "/export-exam.ics":
            exam_id = parse_qs(parsed.query).get("exam_id", [""])[0]
            with connect() as conn:
                exam = conn.execute(
                    "SELECT * FROM exams WHERE id = ?", (exam_id,)
                ).fetchone()
                if exam is None:
                    self._redirect_home("exams")
                    return
                events = collect_plan_events(conn, exam_id)
            self._send_text(build_ics(events), "text/calendar",
                            filename=f"exam-{exam_id}.ics")
            return
        if parsed.path == "/add-sample-data":
            with connect() as conn:
                create_sample_data(conn)
            self._redirect_home("courses", saved=True)
            return
        if parsed.path == "/delete-sample-data":
            with connect() as conn:
                delete_sample_data(conn)
            self._redirect_home("courses", saved=True)
            return
        if parsed.path == "/topic-heavy":
            # Heavy-topic toggle (upgrade 12): same shape as the
            # importance links — a GET that flips one flag and
            # goes back where it came from.
            query = parse_qs(parsed.query)
            topic_id = query.get("id", [""])[0]
            heavy = 1 if query.get("heavy", [""])[0] == "1" else 0
            with connect() as conn:
                conn.execute(
                    "UPDATE topics SET heavy = ? WHERE id = ?",
                    (heavy, topic_id),
                )
            self._redirect_next(query, "topics")
            return
        if parsed.path == "/exam":
            query = parse_qs(parsed.query)
            exam_id = query.get("id", [""])[0]
            saved = query.get("saved", [""])[0] == "1"
            warn = query.get("warn", [""])[0]
            with connect() as conn:
                exam = conn.execute(
                    "SELECT * FROM exams WHERE id = ?", (exam_id,)
                ).fetchone()
                if exam is None:
                    self._redirect_home("exams")
                    return
                self._send(exam_page(conn, exam, saved=saved, warn=warn))
            return
        if parsed.path == "/setup":
            query = parse_qs(parsed.query)
            step = query.get("step", ["1"])[0]
            saved = query.get("saved", [""])[0] == "1"
            with connect() as conn:
                self._send(setup_page(conn, step, saved=saved))
            return
        if parsed.path == "/add-exam":
            query = parse_qs(parsed.query)
            step = query.get("step", ["1"])[0]
            draft = _exam_draft_from_query(query)
            with connect() as conn:
                if step == "2":
                    if not _exam_draft_is_usable(conn, draft):
                        self._redirect_to("/add-exam?step=1")
                        return
                    self._send(exam_flow_step2_html(conn, draft))
                    return
                if step == "3":
                    if not _exam_draft_is_usable(conn, draft):
                        self._redirect_to("/add-exam?step=1")
                        return
                    self._send(exam_flow_step3_html(conn, draft))
                    return
                self._send(exam_flow_step1_html(conn, draft))
            return
        if parsed.path in SECTION_ROUTES:
            query = parse_qs(parsed.query)
            saved = query.get("saved", [""])[0] == "1"
            warn = query.get("warn", [""])[0]
            with connect() as conn:
                self._send(self._section_page(
                    conn, SECTION_ROUTES[parsed.path], saved=saved,
                    warn=warn))
            return
        if parsed.path != "/":
            self.send_error(404)
            return
        # ?open=exams names the section to render open (Fix 2).
        # ?saved=1 shows "נשמר ✓" (QA round 2, fix 10) and
        # ?warn=<code> shows a warning notice (fixes 5, 7, 12).
        query = parse_qs(parsed.query)
        open_section = query.get("open", [""])[0] or None
        saved = query.get("saved", [""])[0] == "1"
        warn = query.get("warn", [""])[0]
        with connect() as conn:
            if open_section in (None, "today", "future"):
                self._send(self._dashboard(conn, saved=saved, warn=warn))
            else:
                self._send(self._home_page(
                    conn, open_section=open_section, saved=saved,
                    warn=warn))

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        fields = parse_qs(self.rfile.read(length).decode())
        value = lambda k: fields.get(k, [""])[0].strip()
        if self.path == "/parse-syllabus":
            # Show the parsed topics for approval/editing, do not save yet.
            with connect() as conn:
                course = conn.execute(
                    "SELECT * FROM courses WHERE id = ?", (value("course_id"),)
                ).fetchone()
                if course is None:
                    # QA round 2, fix 3: no course, no preview —
                    # say so instead of silently going home.
                    message = "בחר קורס קיים — הסילבוס לא נותח."
                    self._send(self._post_error_page(
                        conn, value, message, {"course_id": message},
                        open_section="topics"))
                    return
                parsed = parse_syllabus(value("syllabus"))
                # The wizard's parse form lives on step 2 and its
                # "next" is step 3 (where a confirmed save lands),
                # but "ביטול" must go back to step 2, where the
                # paste form is — not forward to step 3.
                next_url = value("next") or None
                cancel_url = ("/setup?step=2"
                              if next_url
                              and next_url.startswith("/setup")
                              else None)
                self._send(_syllabus_preview_page(
                    course["name"], course["id"], parsed,
                    next_url=next_url, cancel_url=cancel_url))
            return
        if self.path == "/parse-busy":
            # Show the parsed busy blocks for approval, do not
            # save yet — same pattern as the syllabus preview
            # (upgrade 9).
            blocks, errors = parse_busy_lines(value("busy_lines"))
            self._send(_busy_preview_page(
                blocks, errors, next_url=value("next") or None))
            return
        with connect() as conn:
            if self.path == "/save-prefs":
                # Routine answers. A blank field means "not answered"
                # (NULL), not the default — the defaults only apply
                # when planning, never as a saved answer.
                # QA round 2, fix 9: garbage or out-of-range answers
                # are rejected with an error and NOTHING is saved,
                # so the previous routine stays exactly as it was.
                # A form without the study_style field keeps the
                # saved value instead of wiping it.
                if "study_style" in fields:
                    style_text = value("study_style")
                else:
                    current = get_prefs(conn)
                    style_text = (current["study_style"]
                                  if current and "study_style"
                                  in current.keys()
                                  else "")
                values, routine_error = validate_routine(
                    value("session_minutes"), value("break_minutes"),
                    value("homework_pref"), style_text)
                if routine_error is not None:
                    if value("next").startswith("/setup"):
                        self._send(setup_page(
                            conn, 4, error=routine_error))
                    else:
                        ferr = {}
                        for text, field, short in (
                                ("אורך מפגש", "session_minutes",
                                 "אורך מפגש חייב להיות מספר שלם בין 15 ל-180 דקות."),
                                ("אורך הפסקה", "break_minutes",
                                 "אורך הפסקה חייב להיות מספר שלם בין 0 ל-60 דקות."),
                                ("העדפת שיעורי בית", "homework_pref",
                                 "העדפת שיעורי בית חייבת להיות בוקר, ערב או אין העדפה."),
                                ("סגנון הלמידה", "study_style",
                                 "סגנון הלמידה חייב להיות פוקוס או ערבוב.")):
                            if text in routine_error:
                                ferr[field] = short
                        self._send(self._post_error_page(
                            conn, value, routine_error, ferr,
                            open_section="routine"))
                    return
                session, break_, pref, style = values
                save_prefs(conn, session, break_, pref, style)
            elif self.path == "/add-course":
                # QA round 3, fix 2: an empty name used to create
                # a nameless course, and a duplicate was silently
                # ignored by INSERT OR IGNORE — both showed
                # "נשמר ✓". Both are now rejected with an error
                # and nothing is saved.
                if not value("name"):
                    message = "שם הקורס חסר — הקורס לא נשמר."
                    self._send(self._post_error_page(
                        conn, value, message, {"name": message},
                        open_section="courses"))
                    return
                if conn.execute(
                        "SELECT 1 FROM courses WHERE name = ?",
                        (value("name"),)).fetchone() is not None:
                    message = "קורס כזה כבר קיים — הקורס לא נשמר."
                    self._send(self._post_error_page(
                        conn, value, message, {"name": message},
                        open_section="courses"))
                    return
                conn.execute(
                    "INSERT OR IGNORE INTO courses (name, teacher) VALUES (?, ?)",
                    (value("name"), value("teacher")),
                )
            elif self.path == "/add-assignment":
                # QA round 3, fix 2: an empty title used to fall
                # through to the generic redirect and show
                # "נשמר ✓" while nothing was saved.
                if not value("title"):
                    message = "שם המטלה חסר — המטלה לא נשמרה."
                    self._send(self._post_error_page(
                        conn, value, message, {"title": message},
                        open_section="assignments"))
                    return
                # QA round 2, fix 3: the course must exist first —
                # an empty or made-up course id used to crash the
                # request with an unhandled database error.
                if not course_exists(conn, value("course_id")):
                    message = "בחר קורס קיים — המטלה לא נשמרה."
                    self._send(self._post_error_page(
                        conn, value, message, {"course_id": message},
                        open_section="assignments"))
                    return
                # QA round 3, fix 6: a due date and a real hour
                # estimate are required — without them the
                # assignment was saved with 0 blocks and no
                # warning at all.
                if not value("due_date"):
                    message = ("תאריך הגשה חסר — בחר תאריך הגשה. "
                               "המטלה לא נשמרה.")
                    self._send(self._post_error_page(
                        conn, value, message, {"due_date": message},
                        open_section="assignments"))
                    return
                # A due date, when given, must be real and sensible:
                # year 1212 or a past date is rejected, nothing saved.
                if validate_date(value("due_date")) is None:
                    message = ("התאריך לא הגיוני — בחר תאריך הגשה "
                               "אמיתי מהיום והלאה. המטלה לא נשמרה.")
                    self._send(self._post_error_page(
                        conn, value, message, {"due_date": message},
                        open_section="assignments"))
                    return
                try:
                    hours = float(value("estimated_hours")) \
                        if value("estimated_hours") else None
                except ValueError:
                    hours = None
                if hours is None or hours <= 0:
                    message = ("הערכת שעות לא תקינה — כתוב כמה שעות "
                               "המטלה תיקח. המטלה לא נשמרה.")
                    self._send(self._post_error_page(
                        conn, value, message,
                        {"estimated_hours": message},
                        open_section="assignments"))
                    return
                try:
                    parts = int(value("num_parts")) \
                        if value("num_parts") else None
                except ValueError:
                    parts = None
                if parts is not None and parts < 1:
                    parts = None
                cursor = conn.execute(
                    "INSERT INTO assignments"
                    " (course_id, title, due_date, estimated_hours, num_parts)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (value("course_id"), value("title"), value("due_date"),
                     hours, parts),
                )
                # Schedule its whole blocks right away, so the new
                # assignment never shows "0 minutes scheduled" until
                # the user presses something. The full cascade runs:
                # every open assignment is rescheduled in due-date
                # order, then the exam plans are rebuilt — a review
                # that has to yield its slot to the new assignment
                # moves into the time that is left.
                reschedule_everything(conn)
            elif self.path == "/add-topic":
                # QA round 3, fix 2: an empty title used to fall
                # through to the generic redirect and show
                # "נשמר ✓" while nothing was saved.
                if not value("title"):
                    message = "שם הנושא חסר — הנושא לא נשמר."
                    self._send(self._post_error_page(
                        conn, value, message, {"title": message},
                        open_section="topics"))
                    return
                # QA round 2, fix 3: same course check as exams and
                # assignments — an empty course id must show an
                # error, never crash the request.
                if not course_exists(conn, value("course_id")):
                    message = "בחר קורס קיים — הנושא לא נשמר."
                    self._send(self._post_error_page(
                        conn, value, message, {"course_id": message},
                        open_section="topics"))
                    return
                conn.execute(
                    "INSERT INTO topics (course_id, title) VALUES (?, ?)",
                    (value("course_id"), value("title")),
                )
            elif self.path == "/save-topic-title":
                # Rename one topic, from the edit page. An empty
                # title is rejected with a Hebrew error and
                # nothing is saved — same rule as the other forms,
                # no fake "נשמר ✓". The error is shown back on
                # the edit page, where the form lives.
                topic_id = value("topic_id")
                topic = conn.execute(
                    "SELECT * FROM topics WHERE id = ?",
                    (topic_id,)).fetchone()
                if topic is None:
                    self._redirect_to("/topics")
                    return
                if not value("title"):
                    message = "שם הנושא חסר — השם לא נשמר."
                    self._send(edit_topics_page(
                        conn, topic["course_id"], error=message))
                    return
                conn.execute(
                    "UPDATE topics SET title = ? WHERE id = ?",
                    (value("title"), topic_id),
                )
                self._redirect_to(
                    f"/edit-topics?course_id={topic['course_id']}&saved=1")
                return
            elif self.path == "/confirm-clear-topics":
                # The confirm button on the /clear-topics page:
                # only this POST deletes. A GET never deletes.
                course_id = value("course_id")
                if course_exists(conn, course_id):
                    clear_course_topics(conn, course_id)
                self._redirect_to("/topics?saved=1")
                return
            elif self.path == "/confirm-delete-topic":
                # The confirm button on the /delete-topic page:
                # only this POST deletes one topic.
                topic_id = value("topic_id")
                course_id = delete_topic(conn, topic_id)
                if course_id is None:
                    self._redirect_to("/topics")
                else:
                    self._redirect_to(
                        f"/edit-topics?course_id={course_id}&saved=1")
                return
            elif self.path == "/save-topics":
                # One topic per line, from the approved/edited textarea.
                if not course_exists(conn, value("course_id")):
                    message = "בחר קורס קיים — הנושאים לא נשמרו."
                    self._send(self._post_error_page(
                        conn, value, message, {"course_id": message},
                        open_section="topics"))
                    return
                # QA round 3, fix 2: an empty syllabus used to
                # fall through to "נשמר ✓" while saving nothing.
                parsed_topics = parse_syllabus(value("topics"))
                if not parsed_topics:
                    message = "לא נמצאו נושאים — הנושאים לא נשמרו."
                    self._send(self._post_error_page(
                        conn, value, message, None,
                        open_section="topics"))
                    return
                for title in parsed_topics:
                    conn.execute(
                        "INSERT INTO topics (course_id, title) VALUES (?, ?)",
                        (value("course_id"), title),
                    )
            elif self.path == "/add-slot":
                # QA round 2, fix 1: weekday and times are validated
                # before saving — a bad slot is rejected with an
                # error, nothing is saved, and the home page can
                # never crash on it again.
                slot_error = validate_slot(
                    value("weekday"), value("start_time"),
                    value("end_time"))
                if slot_error is not None:
                    message = slot_error + " החלון לא נשמר."
                    if "יום בשבוע" in slot_error:
                        ferr = {"weekday": message}
                    elif "שעת הסיום" in slot_error:
                        ferr = {"end_time": message}
                    else:
                        ferr = {"start_time": message}
                    self._send(self._post_error_page(
                        conn, value, message, ferr,
                        open_section="slots"))
                    return
                weekday = int(value("weekday"))
                # QA round 2, fix 12: an exact duplicate is
                # rejected; an overlap is saved but warns, because
                # the planner merges overlapping windows anyway.
                if slot_is_duplicate(conn, weekday, value("start_time"),
                                     value("end_time")):
                    message = "חלון כזה כבר קיים — החלון לא נשמר."
                    self._send(self._post_error_page(
                        conn, value, message, {"weekday": message},
                        open_section="slots"))
                    return
                overlaps = slot_overlaps(
                    conn, weekday, value("start_time"), value("end_time"))
                conn.execute(
                    "INSERT INTO free_slots (weekday, start_time, end_time)"
                    " VALUES (?, ?, ?)",
                    (weekday, value("start_time"), value("end_time")),
                )
                if overlaps:
                    # Save first, then redirect with the warning:
                    # the slot is in, and the user is told it
                    # overlaps an existing one. A wizard "next"
                    # wins, as usual — the wizard page has no
                    # place to show the warning.
                    next_url = value("next")
                    if next_url.startswith("/") \
                            and not next_url.startswith("//"):
                        self._redirect_to(_with_saved(next_url))
                    else:
                        self._redirect_home("slots", saved=True,
                                            warn="slot_overlap")
                    return
            elif self.path == "/add-busy":
                # QA round 3, fix 2: a missing date or invalid
                # times used to fall through to "נשמר ✓" while
                # saving nothing.
                if not value("block_date"):
                    message = ("תאריך חסר — בחר תאריך ליום החריג. "
                               "היום החריג לא נשמר.")
                    self._send(self._post_error_page(
                        conn, value, message, {"block_date": message},
                        open_section="busy"))
                    return
                if validate_date(value("block_date")) is None:
                    message = ("התאריך לא הגיוני — בחר תאריך אמיתי "
                               "מהיום והלאה. היום החריג לא נשמר.")
                    self._send(self._post_error_page(
                        conn, value, message, {"block_date": message},
                        open_section="busy"))
                    return
                if not is_valid_slot(
                        value("start_time"), value("end_time")):
                    message = ("השעות חייבות להיות שעות אמיתיות, "
                               "וסיום אחרי התחלה. היום החריג לא נשמר.")
                    self._send(self._post_error_page(
                        conn, value, message, {"start_time": message},
                        open_section="busy"))
                    return
                conn.execute(
                    "INSERT INTO busy_blocks"
                    " (block_date, start_time, end_time, reason)"
                    " VALUES (?, ?, ?, ?)",
                    (value("block_date"), value("start_time"),
                     value("end_time"), value("reason")),
                )
                # QA fix 4: a new busy block means existing
                # sessions and blocks may sit on blocked time —
                # move them, the same cascade as marking an
                # assignment done.
                reschedule_everything(conn)
            elif self.path == "/save-busy":
                # The confirmed blocks from the preview page:
                # re-parse defensively (a bad line is skipped,
                # never saved), insert, and rebuild everything so
                # sessions move off the new busy time (upgrade 9).
                blocks, _errors = parse_busy_lines(value("busy_lines"))
                if not blocks:
                    # No valid blocks: say so, instead of falling
                    # through to a fake "נשמר ✓" (QA round 3,
                    # fix 2).
                    message = "לא נמצאו חסימות תקינות — כלום לא נשמר."
                    self._send(self._post_error_page(
                        conn, value, message, None,
                        open_section="busy"))
                    return
                for b in blocks:
                    conn.execute(
                        "INSERT INTO busy_blocks"
                        " (block_date, start_time, end_time, reason)"
                        " VALUES (?, ?, ?, ?)",
                        (b["block_date"], b["start_time"],
                         b["end_time"], b["reason"]),
                    )
                reschedule_everything(conn)
            elif self.path == "/add-exam-step1":
                # Step 1 of the flow: course, kind, title, date.
                # Errors re-render the step with the error under
                # its field; success moves to step 2 with the draft
                # in the URL.
                draft = _exam_draft_from_query(fields)
                ferr = {}
                if not course_exists(conn, draft["course_id"]):
                    ferr["course_id"] = "בחר קורס קיים — המבחן לא נשמר."
                if draft["kind"] not in ("exam", "quiz"):
                    ferr["kind"] = "סוג המבחן לא תקין — המבחן לא נשמר."
                if not draft["title"]:
                    ferr["title"] = "שם המבחן חסר — המבחן לא נשמר."
                if validate_date(draft["exam_date"]) is None:
                    ferr["exam_date"] = ("התאריך לא הגיוני — בחר תאריך "
                                         "אמיתי מהיום והלאה. המבחן לא נשמר.")
                if ferr:
                    self._send(exam_flow_step1_html(
                        conn, draft, field_errors=ferr,
                        error="תקן את השדות המסומנים — המבחן לא נשמר."))
                    return
                self._redirect_to("/add-exam?step=2&" + _draft_query(draft))
                return
            elif self.path == "/add-exam-step2":
                # Step 2 of the flow: topics + importance. A quiz
                # with no topics picked is rejected here, before
                # anything is saved. Importance radios only change
                # a topic when one is picked.
                draft = _exam_draft_from_query(fields)
                if not _exam_draft_is_usable(conn, draft):
                    self._redirect_to("/add-exam?step=1")
                    return
                scope = resolve_exam_scope(
                    conn, draft["kind"], draft["course_id"],
                    draft["topic_id"])
                if scope is None:
                    message = ("בוחן חייב לכלול לפחות נושא אחד — "
                               "סמן נושאים לפני השמירה.")
                    self._send(exam_flow_step2_html(
                        conn, draft, field_errors={"topic_id": message},
                        error=message))
                    return
                for topic in get_topics(conn):
                    imp = value(f"importance_{topic['id']}")
                    if imp in ("low", "medium", "high"):
                        conn.execute(
                            "UPDATE topics SET importance = ? WHERE id = ?",
                            (imp, topic["id"]),
                        )
                self._redirect_to("/add-exam?step=3&" + _draft_query(draft))
                return
            elif self.path == "/add-exam-step3":
                # Step 3 of the flow: prep split, lead time,
                # emergency. Saving creates the exam and its plan
                # with the same validation as the old form.
                draft = _exam_draft_from_query(fields)
                if not _exam_draft_is_usable(conn, draft):
                    self._redirect_to("/add-exam?step=1")
                    return
                exam_id, message, ferr = create_exam_from_fields(
                    conn, draft["course_id"], draft["kind"],
                    draft["title"], draft["exam_date"],
                    value("learn_pct"), value("review_pct"),
                    value("exercise_pct"), value("simulation_pct"),
                    value("lead_days"),
                    1 if value("emergency") == "1" else 0,
                    draft["topic_id"],
                )
                if exam_id is None:
                    self._send(exam_flow_step3_html(
                        conn, draft, field_errors=ferr, error=message))
                    return
                warn_codes = []
                if draft["exam_date"] == date.today().isoformat():
                    warn_codes.append("sameday")
                if parse_percentage(value("learn_pct"), 40) == 0:
                    warn_codes.append("zerolearn")
                target = f"/exam?id={exam_id}&saved=1"
                if warn_codes:
                    target += "&warn=" + ",".join(warn_codes)
                self._redirect_to(target)
                return
            elif self.path == "/add-exam":
                # The old single-form creation (the wizard's step 5
                # still posts here): same validation and creation
                # as the stepped flow's step 3.
                exam_id, message, _ferr = create_exam_from_fields(
                    conn, value("course_id"), value("kind"),
                    value("title"), value("exam_date"),
                    value("learn_pct"), value("review_pct"),
                    value("exercise_pct"), value("simulation_pct"),
                    value("lead_days"),
                    1 if value("emergency") == "1" else 0,
                    [t for t in fields.get("topic_id", []) if t.strip()],
                )
                if exam_id is None:
                    self._send(self._exam_error_page(
                        conn, value, message))
                    return
                warn_codes = []
                if value("exam_date") == date.today().isoformat():
                    warn_codes.append("sameday")
                if parse_percentage(value("learn_pct"), 40) == 0:
                    warn_codes.append("zerolearn")
                if value("next").startswith("/setup"):
                    self._redirect_to(_with_saved(value("next")))
                    return
                self._redirect_home(
                    "exams", saved=True,
                    warn=",".join(warn_codes) or None)
                return
            elif self.path == "/save-exam":
                # The exam page's edit form: same validation as
                # creation (QA fix 5) — real date, percentages
                # summing to 100, and a quiz must keep at least
                # one topic in scope — then rebuild this exam's
                # plan from today.
                exam_id = value("exam_id")
                exam = conn.execute(
                    "SELECT * FROM exams WHERE id = ?", (exam_id,)
                ).fetchone()
                if exam is None:
                    self._redirect_home("exams")
                    return
                if not value("title"):
                    message = "שם המבחן חסר — המבחן לא נשמר."
                    self._send(exam_page(
                        conn, exam, error=message,
                        field_errors={"title": message}))
                    return
                # QA round 3, fix 5: a past date is rejected only
                # when it CHANGED — a past exam can still be
                # renamed or re-scoped, it just cannot be moved to
                # a different past date.
                if value("exam_date") != exam["exam_date"] \
                        and validate_date(value("exam_date")) is None:
                    message = ("התאריך לא הגיוני — בחר תאריך אמיתי "
                               "מהיום והלאה. המבחן לא נשמר.")
                    self._send(exam_page(
                        conn, exam, error=message,
                        field_errors={"exam_date": message}))
                    return
                pcts = (
                    parse_percentage(value("learn_pct"), 40),
                    parse_percentage(value("review_pct"), 20),
                    parse_percentage(value("exercise_pct"), 20),
                    parse_percentage(value("simulation_pct"), 20),
                )
                if not validate_percentages(*pcts):
                    message = ("אחוזי ההכנה חייבים להסתכם ב-100 — "
                               "המבחן לא נשמר.")
                    self._send(exam_page(
                        conn, exam, error=message,
                        field_errors={"learn_pct": message}))
                    return
                kind = value("kind") or "exam"
                if kind not in ("exam", "quiz"):
                    kind = "exam"
                if kind == "quiz":
                    scoped = conn.execute(
                        "SELECT COUNT(*) FROM exam_topics"
                        " WHERE exam_id = ?",
                        (exam_id,)).fetchone()[0]
                    if scoped == 0:
                        message = ("בוחן חייב לכלול לפחות נושא אחד — "
                                   "סמן נושאים לפני השמירה.")
                        self._send(exam_page(
                            conn, exam, error=message,
                            field_errors={"topic_id": message}))
                        return
                # Lead time (new feature): same validation as
                # creation — 1–30 days, default 14 when blank — so
                # the edit form cannot store a bad window either.
                lead_days = parse_lead_days(value("lead_days"))
                if not validate_lead_days(lead_days):
                    message = ("מספר הימים לא תקין — הלמידה מתחילה "
                               "1 עד 30 ימים לפני המבחן. "
                               "המבחן לא נשמר.")
                    self._send(exam_page(
                        conn, exam, error=message,
                        field_errors={"lead_days": message}))
                    return
                # Emergency mode (upgrade 11): same as creation —
                # an unticked checkbox sends nothing, which means
                # off, so turning it off restores the normal plan.
                emergency = 1 if value("emergency") == "1" else 0
                conn.execute(
                    "UPDATE exams SET kind = ?, title = ?, exam_date = ?,"
                    " learn_pct = ?, review_pct = ?, exercise_pct = ?,"
                    " simulation_pct = ?, emergency = ?, lead_days = ?"
                    " WHERE id = ?",
                    (kind, value("title"), value("exam_date"), *pcts,
                     emergency, lead_days, exam_id),
                )
                exam = conn.execute(
                    "SELECT * FROM exams WHERE id = ?", (exam_id,)
                ).fetchone()
                regenerate_exam_plan(conn, exam_id, exam)
                # QA round 2, fixes 5, 7 and 10: back to the exam
                # page with "נשמר ✓", plus a warning when the exam
                # is today or the learning share is 0%.
                warn_codes = []
                if value("exam_date") == date.today().isoformat():
                    warn_codes.append("sameday")
                if pcts[0] == 0:
                    warn_codes.append("zerolearn")
                target = f"/exam?id={exam_id}&saved=1"
                if warn_codes:
                    target += "&warn=" + ",".join(warn_codes)
                self._redirect_to(target)
                return
            elif self.path == "/save-assignment":
                # The assignment page's edit form: same validation
                # as creation (QA fix 5) — a due date, when given,
                # must be real and sensible — then reschedule, so
                # freed or moved time is reused at once.
                assignment_id = value("assignment_id")
                row = conn.execute(
                    "SELECT * FROM assignments WHERE id = ?",
                    (assignment_id,)).fetchone()
                if row is None:
                    self._redirect_home("assignments")
                    return
                if not value("title"):
                    message = "שם המטלה חסר — המטלה לא נשמרה."
                    self._send(assignment_page(
                        conn, row, error=message,
                        field_errors={"title": message}))
                    return
                # QA round 3, fix 5: a past date is rejected only
                # when it CHANGED — a past assignment can still be
                # renamed, it just cannot be moved to a different
                # past date.
                if value("due_date") \
                        and value("due_date") != (row["due_date"] or "") \
                        and validate_date(value("due_date")) is None:
                    message = ("התאריך לא הגיוני — בחר תאריך הגשה "
                               "אמיתי מהיום והלאה. המטלה לא נשמרה.")
                    self._send(assignment_page(
                        conn, row, error=message,
                        field_errors={"due_date": message}))
                    return
                try:
                    hours = float(value("estimated_hours")) \
                        if value("estimated_hours") else None
                except ValueError:
                    hours = None
                # QA round 3, fix 6 (edit side): garbage hours
                # used to become None silently — a non-empty hours
                # field that is not a positive number is rejected.
                if value("estimated_hours") \
                        and (hours is None or hours <= 0):
                    message = "הערכת שעות לא תקינה — המטלה לא נשמרה."
                    self._send(assignment_page(
                        conn, row, error=message,
                        field_errors={"estimated_hours": message}))
                    return
                try:
                    parts = int(value("num_parts")) \
                        if value("num_parts") else None
                except ValueError:
                    parts = None
                if parts is not None and parts < 1:
                    parts = None
                conn.execute(
                    "UPDATE assignments SET title = ?, due_date = ?,"
                    " estimated_hours = ?, num_parts = ? WHERE id = ?",
                    (value("title"), value("due_date"), hours, parts,
                     assignment_id),
                )
                reschedule_everything(conn)
                self._redirect_home("assignments", saved=True)
                return
            elif self.path == "/save-exam-scope":
                # The exam page's save: replace the scope, then
                # rebuild that exam's plan from today.
                exam_id = value("exam_id")
                exam = conn.execute(
                    "SELECT * FROM exams WHERE id = ?", (exam_id,)
                ).fetchone()
                if exam is None:
                    self._redirect_home("exams")
                    return
                picked = [t for t in fields.get("topic_id", [])
                          if t.strip()]
                scope = resolve_exam_scope(
                    conn, exam["kind"], exam["course_id"], picked)
                if scope is None:
                    message = ("בוחן חייב לכלול לפחות נושא אחד — "
                               "סמן נושאים לפני השמירה.")
                    self._send(exam_page(
                        conn, exam, error=message,
                        field_errors={"topic_id": message}))
                    return
                conn.execute(
                    "DELETE FROM exam_topics WHERE exam_id = ?",
                    (exam_id,))
                for topic_id in scope:
                    conn.execute(
                        "INSERT OR IGNORE INTO exam_topics (exam_id, topic_id)"
                        " VALUES (?, ?)",
                        (exam_id, topic_id),
                    )
                regenerate_exam_plan(conn, exam_id, exam)
                # QA round 2, fix 10: "נשמר ✓" on the exam page.
                self._redirect_to(f"/exam?id={exam_id}&saved=1")
                return
        # A "next" field (the wizard) wins over the section
        # redirect; otherwise go back to the section just used.
        # QA round 2, fix 10: the section redirect carries
        # "נשמר ✓", so a successful save is visible. A wizard
        # "next" gets ?saved=1 too, so the next step shows
        # "נשמר ✓" right where the user lands.
        next_url = value("next")
        if next_url.startswith("/") and not next_url.startswith("//"):
            if next_url.startswith("/setup"):
                next_url = _with_saved(next_url)
            self._redirect_to(next_url)
        else:
            self._redirect_home(POST_SECTIONS.get(self.path), saved=True)

    def log_message(self, *args):  # keep the terminal quiet
        pass


if __name__ == "__main__":
    init_db()
    print("Study Planner running at http://localhost:8000")
    print("On this Wi-Fi network, a phone can open it at "
          "http://<this computer's IPv4 address>:8000")
    print("Trusted home Wi-Fi only — do not expose port 8000 to the "
          "internet: this local version has no login.")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
