# Study Planner

A small study planner for students, built in pure Python
(standard library only — no dependencies to install, no JavaScript).

Add courses, paste a course syllabus to get its topics, mark your
weekly free time, add an exam or a quiz, and the planner builds a
day-by-day study plan that fits only inside your free time. Data is
stored in SQLite (`planner.db`, created on first run).

## Features

- **Topics from a syllabus** — paste syllabus text; the parser
  extracts one topic per line. Pasting a whole university course
  page parses only its topics section. The list is shown back with
  its count for approval before saving.
- **Weekly free slots** — the plan is built only inside your real
  free time (week shown Sunday-first).
- **Exams and quizzes** — a quiz must pick which topics are in
  scope; an exam covers the whole course by default. Each exam has
  a lead time (how many days before it learning starts, 1–30,
  default 14), topic importance, prep percentages
  (learn / review / exercise / simulation, must sum to 100), and
  an emergency mode for near exams.
- **Smart plan** — one learning session per topic, in importance
  order, with breaks; reviews by spaced repetition (1, 3 and 7
  days after learning); exercise only after first learning;
  simulations near the exam. Sessions that cannot fit are reported
  honestly, never silently dropped.
- **Assignments** — hours and number of parts become one continuous
  block per part, scheduled around exam sessions. Assignments
  outrank general review sessions: a review that has not been done
  yet yields its time to homework, and the exam plan is rebuilt so
  the review moves into whatever free time is left. Assignments are
  scheduled in due-date order (most urgent first), and there is a
  quick add-assignment form right on the dashboard.
- **Today-first dashboard** — what to do now and next, missed
  sessions (a session that ended today and was not done is flagged
  missed the same day), blocked time shown with its reason, a "needs
  attention" box, and per-exam countdown and progress.
- **Export** — download the whole plan (or one exam) as an `.ics`
  file and import it into Google or Apple Calendar. The export
  includes the exam itself as an all-day event, and blocked time.
- **Undo** — marking anything done can be undone; deleting topics
  always asks for confirmation first.

## Run it

```bash
python3 app.py
# open http://localhost:8000
```

**From your phone (same home Wi-Fi):** the server listens on
`0.0.0.0`. On Windows, run `ipconfig` and take the IPv4 address,
then open `http://<that address>:8000` in the phone's browser.

> ⚠️ **Only on trusted home Wi-Fi.** The local version has no
> login. Do NOT expose port 8000 to the internet.

## Tests

```bash
python3 -m unittest test_app -v
```

205 tests, covering parsing, planning, validation, and every
route — all passing, zero `<script>` tags in the app.

## Project structure

- `app.py` — the whole app: SQLite schema, planning algorithms,
  and the web UI (one file, standard library only)
- `test_app.py` — the test suite
- `README.md` — this file
- `.gitignore` — keeps `planner.db` (your data) and `__pycache__`
  out of git

The database file (`planner.db`) sits next to `app.py` and holds
all your data. It is never committed — it is yours, not the
project's.
