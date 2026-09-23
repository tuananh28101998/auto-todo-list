# Daily TODO for the Product Dev Team — AUTOBOOST

Two scripts that read the GitHub Project `XAION-DATA-Inc/projects/2` (AUTOBOOST)
to generate a daily TODO list per team member, and to check data quality before
you trust any of the numbers.

| File | When to use it |
|---|---|
| `daily_todo.py` | Every morning — generates the per-person TODO list |
| `autoboost_diagnose.py` | When you need to check the data, or when output looks wrong |

`daily_todo.py` **imports** `autoboost_diagnose.py`, so both files must sit in the
same directory. No third-party dependencies — stdlib only, Python 3.9+.

---

## 1. Setup

```bash
export GITHUB_TOKEN=ghp_xxxxxxxxxxxx
```

The token needs two scopes:

| Scope | What it's for | If missing |
|---|---|---|
| `read:project` | Read the project, its fields and iterations | Script reports the project was not found |
| `repo` | Read issues, PRs and reviews | Reviewer data comes back empty → every `In review` item lands in "no actor resolved" |

You set the token in your own environment. Neither script ever writes to
GitHub — both are read-only.

---

## 2. Daily run

```bash
# Preview, writes nothing
python3 daily_todo.py --html todo.html

# Lock in today's list (writes to state so carryover can be counted)
python3 daily_todo.py --html todo.html --commit
```

**Dry-run by default.** Without `--commit` nothing is written to state. Nothing
is sent anywhere unless a Slack bot token or webhook is set.

### Flags

| Flag | Default | Meaning |
|---|---|---|
| `--commit` | off | Write today's list to state |
| `--html FILE` | — | Write the HTML version |
| `--out FILE` | — | Write the text version |
| `--extra FILE` | — | JSON file of escalation items from Slack |
| `--due-within N` | off | Only list items due within N working days (see 5) |
| `--slack-bot-token T` | `$SLACK_BOT_TOKEN` | Post via a Slack bot: text list + the `--html` file. Needs `--slack-channel` |
| `--slack-channel CID` | `$SLACK_CHANNEL` | Channel ID for the bot post |
| `--slack-webhook URL` | `$SLACK_WEBHOOK` | Fallback: text list only, to an Incoming Webhook. Ignored when a bot token is set |
| `--snapshot DIR` | — | Also freeze today's list as `DIR/YYYY-MM-DD.json` (see 8.1) |
| `--render-snapshot FILE` | — | Re-render an old snapshot (text to stdout, `--html` for HTML) and exit. No token needed |
| `--state FILE` | `todo_state.jsonl` next to the script | Change where state lives |
| `--no-review` | off | Skip the reviewer-fetch phase (faster, but `In review` loses its actor) |
| `--today YYYY-MM-DD` | today | Simulate the run date, for testing |
| `--org` / `--project` | `XAION-DATA-Inc` / `2` | Point at a different project |

`--commit` is safe to run twice: the second run on the same day reports
*"no new rows"* instead of duplicating.

### Scheduled run — GitHub Actions

`.github/workflows/daily-todo.yml` runs the list every weekday at **08:15
Asia/Ho_Chi_Minh** (`15 1 * * 1-5` UTC), posts it to Slack, keeps `todo.html`
as a workflow artifact for 30 days, and commits `todo_state.jsonl` plus the
day's `snapshots/YYYY-MM-DD.json` back to the repo so carryover and history
survive between runs. No machine of yours needs to be on.

Two repository secrets (Settings → Secrets and variables → Actions):

| Secret | Value |
|---|---|
| `AUTOBOOST_TOKEN` | A GitHub PAT with `read:project` + `repo` (section 1). The workflow's own `GITHUB_TOKEN` cannot read org projects. |
| `SLACK_BOT_TOKEN` | Bot token (`xoxb-…`) of a Slack app with scopes `chat:write` + `files:write`. The bot must be invited to the channel (`/invite @app`). Never commit it. |

The channel ID is not a secret; it is set as `SLACK_CHANNEL` in the workflow.

Manual runs: *Actions → Daily TODO → Run workflow*. Both `commit` and `slack`
default to **off** there, so a manual run is a preview — download the artifact
to check the HTML. Tick them to make a manual run behave like the schedule.

GitHub's cron can start several minutes late under load; if 08:15 sharp
matters, set the cron a few minutes early.

Slack rendering: the text list goes into code blocks, split into messages of
at most ~3500 chars so nothing is truncated, followed by `todo.html` uploaded
as a file — click it and Slack renders the grouped view. Without `--html`
only the text is posted. `--slack-webhook` is the text-only fallback for a
channel that has no bot.

---

## 3. Reading the list

Tasks are grouped by person, most-loaded person first. Inside a person's block
they are split into two groups with a divider between them: **PR** (the work is
on a pull request — review it, merge it, fix it after review) and **Ticket** (the
work is the issue itself — you are the owner, or it has not been started). PR
comes first because it blocks someone else. Within each group the order is still
deadline-first. A person with only one kind gets one group and no divider.

Each line carries four pieces of information:

```
duc-hoang-trung - 3 tasks
  PR (1)
  [ ] [OVERDUE 2d]  #11533  [Feature] Add contact to favorite list
      👀 In review - review requested PR 2d  [carried 3d - what is blocking this?]
       └ status       └ why it's yours  └ PR age   └ carryover
  ------------------------------------------------------------------
  Ticket (2)
  [ ] [d-1]         #11707  [HubSpot app] Create matching DAGs
      🏗 In progress - owner
  ...
```

**Why it's yours** is the most important field — it says why *this person*
rather than someone else:

| Reason | Meaning |
|---|---|
| `owner` | You're the assignee; the work is in your hands |
| `NOT STARTED - start +Nd` | Still `Ready`, but the planned start passed N working days ago and no PR exists — see 4.10 |
| `review requested` | You were requested as reviewer on the PR |
| `approved but not merged` | Just needs the merge button — the cheapest unblock |
| `changes requested - ball is with the author` | Review sent it back; it's yours again |
| `PR is a draft - not ready for review` | Status says `In review` but the PR isn't really open |
| `waiting on someone outside the team (PdM)` | Nobody in the team can unblock it → leader block |

**PR age** is the most trustworthy time signal in the system, because git
generates it — nobody can set it to look good. Contrast with `Dev End Date`
(see 9.2).

### The trailing blocks

- **No actor resolved** — `In review` where the reviewer couldn't be determined.
  The script does **not** fall back to the assignee (see 4.4).
- **Leader / PdM** — everything in `In External Review`.
- **Awaiting approval (Slack)** — escalation items not yet approved.
- **Escalated** — carried 4+ days, with the date it first appeared.
- **Not due yet** — only when `--due-within` is used.
- **Not due to start** — `Ready` items whose `Dev Start Date` is still ahead (4.11).

---

## 4. When a task lands on the TODO list

### 4.1. Three required conditions — miss one and it appears nowhere

An item enters consideration only if **all three** hold:

| # | Condition | If not met |
|---|---|---|
| 1 | `Dev Sprint` = the iteration spanning today | Not considered. Next sprint's work isn't today's work |
| 2 | `Status` is one of the four in-scope statuses (9.3) | Not considered. `Staging` / `Done` / `Backlog` / `Management` / QA are all out |
| 3 | Not a draft item | Not considered. Drafts have no number, no assignee, and can't have a PR |

Miss any one and the item shows up **nowhere at all**, including the trailing
blocks. This is a hard filter with no exceptions.

One more gate is softer: a `Ready` item whose `Dev Start Date` is still in the
future *is* considered, but goes to the *Not due to start* block instead of a
person's list, and does not accumulate carryover (4.11).

### 4.2. Once considered — who gets it

`Status` determines the **role** that must act. The role determines where the
actual person is looked up:

| Status | Role | Person comes from |
|---|---|---|
| `Ready` | owner | The item's `Assigned To` |
| `In progress` | owner | The item's `Assigned To` |
| `In review` | reviewer | The **PR**, not the item — see 4.3 |
| `In External Review` | external | Not looked up → *Leader / PdM* block |

Easy to get wrong: for `In review`, the person who gets the task is **not the
assignee**. The assignee is waiting on review and has nothing to do. The
assignee also never changes over a ticket's life, so it can't tell you who has
to act today.

### 4.3. `In review` — all nine cases

PRs linked to the issue are found through **two paths** (closing keywords may
not be used): `closedByPullRequestsReferences` and timeline
`CROSS_REFERENCED_EVENT`. The **newest OPEN PR** by `createdAt` is then
evaluated in this order — first match wins:

| # | PR situation | Goes to | Reason shown |
|---|---|---|---|
| 1 | No PR at all | — | `NO PR FOUND` |
| 2 | All PRs merged | — | `PR already merged - board status is lagging` |
| 3 | All PRs closed | — | `PRs are closed` |
| 4 | PR is a draft | **author** | `PR is a draft - not ready for review` |
| 5 | Review requested | **reviewer(s)** | `review requested` |
| 6 | Latest review = `CHANGES_REQUESTED` | **author** | `changes requested - ball is with the author` |
| 7 | Latest review = `APPROVED` | **author** | `approved but not merged` |
| 8 | Comments only | **reviewer** | `commented, no verdict yet` |
| 9 | PR open, nobody requested, no reviews | — | `PR open, NOBODY requested, no reviews yet` |

Four notes on this table:

- **Only one PR is ever evaluated.** Other open PRs on the same ticket are
  ignored completely — they cannot route work, and their age never shows.
  The shape to watch is a newer *draft* outranking an older ready-for-review
  PR: case 4 then fires and the review task goes to the draft's author while
  the real PR waits unseen. Section 9 counts and names these (see 10).
- **Order matters.** Review requests (5) are checked before review history
  (6–8). A PR with both a fresh request and older reviews goes to whoever was
  requested.
- **Self-requested review is discarded.** Without that filter the author becomes
  reviewer of their own PR and the task is routed to the wrong person.
- **Cases 1, 2, 3 and 9 go to nobody** — they land in *no actor resolved*
  (see 4.4).

### 4.4. Considered but routed to nobody

Three trailing blocks. They are **not** in anyone's personal list:

| Block | Contents | What you do |
|---|---|---|
| *No actor resolved* | `In review` hitting case 1/2/3/9 above, or `Ready`/`In progress` with **no assignee** | Assign a reviewer or an assignee |
| *Leader / PdM* | Everything in `In External Review` | Settle it with PdM |
| *Awaiting approval (Slack)* | Escalation items not yet approved — see 4.5 | Approve or drop |

The script **does not fall back to the assignee** when the reviewer is unknown.
Reasoning in 9.1.

If *no actor resolved* is large, read the reason column to know what to fix: lots
of `NO PR FOUND` is a PR ↔ issue linking convention problem; lots of
`NOBODY requested` is a review-assignment problem.

### 4.5. Slack escalation — different rules entirely

These don't go through 4.1 at all. An item from the `--extra` file needs only
**two things**:

```
approved: true   AND   person: "<github-login>"
```

Missing either → it goes to *awaiting approval* and enters nobody's list.

This is the **only source that needs your decision**. The GitHub side is fully
automatic. An approved item enters the list on the **next run**, not the current
one — by the time you approve, today's list has already been generated.

### 4.6. Carryover is not a separate condition

Easy to misread. **There is no mechanism that pushes work to tomorrow.**

A task reappears today simply because it **still satisfies 4.1** — still in the
sprint, still in an in-scope status, still hasn't reached `Staging`. It was never
carried over; it never left.

`carryover_count` is only a **count**, derived from state: how many distinct days
before today this task appeared in a locked-in list. It doesn't decide whether a
task is listed — only **how it's displayed** (see 6).

Practical consequence: if you skip `--commit` for a day, the task still appears
the next day as normal; only the count doesn't increment for that day.

### 4.7. How a task leaves the list

Nobody marks anything complete. A task leaves when it **stops satisfying 4.1**:

| Exit | Meaning |
|---|---|
| `Status` → `Staging` | Dev work done — the main exit |
| `Status` → `Done` | Finished |
| `Status` → `QA Needed` / `QA In Progress` | Moved to the QA branch (deferred) |
| `Status` → `Backlog` | Parked, pulled out of the sprint |
| `Dev Sprint` changed | Moved to another sprint |

Ticking a checkbox in the HTML does **not** remove a task. Checkboxes are for
reading only. Completion is always derived from GitHub — one source of truth.

The list ends with *"Left the list since \<date\>"*, counting tasks that were in
the previous locked-in list but are no longer in scope.

### 4.8. One task can appear for several people

If a PR has two requested reviewers, the task appears in **both** lists, one row
each. That's deliberate — both are being waited on.

State records two rows too (same `key`, different `person`). But
`carryover_count` counts **distinct days**, not rows, so this doesn't double the
carryover.

### 4.9. Decision flow

```
Item in project
  │
  ├─ Not in the current sprint ──────────────→ skip, not considered
  ├─ Status outside the four in scope ───────→ skip, not considered
  ├─ Is a draft item ────────────────────────→ skip, not considered
  │
  └─ Considered, by Status:
       │
       ├─ Ready, Dev Start Date in the future, no PR
       │                          ──────────→ "not due to start" block (4.11)
       │
       ├─ Ready / In progress
       │     ├─ has assignee ───────────────→ assignee's TODO
       │     └─ none ───────────────────────→ "no actor resolved" block
       │     (Ready only: planned start passed + no PR → the reason
       │      becomes NOT STARTED, P1 or P0 by slip. Routing is
       │      unchanged — see 4.10)
       │
       ├─ In review  → newest OPEN PR
       │     ├─ draft ──────────────────────→ author's TODO
       │     ├─ review requested ───────────→ reviewer(s)' TODO
       │     ├─ CHANGES_REQUESTED ──────────→ author's TODO
       │     ├─ approved, not merged ───────→ author's TODO
       │     ├─ comments only ──────────────→ reviewer's TODO
       │     └─ no PR / merged / closed / nobody requested
       │                          ──────────→ "no actor resolved" block
       │
       └─ In External Review ───────────────→ "Leader / PdM" block

Item from --extra (Slack)
  ├─ approved=true AND person set ──────────→ that person's TODO
  └─ either missing ────────────────────────→ "awaiting approval" block
```

### 4.10. Ready but never started

An item still in `Ready` whose `Dev Start Date` has passed, with no PR, used to
match **no rule at all**. `Ready` carries `dl=dev_end` (9.3), so nothing fired
until `Dev End Date` also passed — and then it was reported as *overdue*, which
is the right severity attributed to the wrong cause. If `Dev End Date` was
empty it fell into the blind set and was never reported by anything.

The condition, all three parts required:

| # | Condition |
|---|---|
| 1 | `Status` is `Ready` — the only status that means not started |
| 2 | `Dev Start Date` is set and has passed |
| 3 | No PR exists and the issue is still open |

Tier by how far the planned start has slipped, in **working** days:

| Slip | Tier |
|---|---|
| 1–2 working days | P1 — a nudge, nothing more |
| more than 2 | P0 — joins the dedupe pool and the 5–7/day budget |

Three details that are deliberate, not accidents:

- **`updatedAt` is ignored here**, unlike in section 4. For a `Ready` item
  having no PR is the *normal* state, so the `updatedAt` fallback in
  `has_activity()` would be the deciding signal — and it is bumped by editing
  any board field. One bulk edit (re-triage, setting `Dev Sprint` across the
  sprint, fixing a title) would silence the rule for the whole team for three
  days. `has_started()` exists to be narrower: a PR, or a closed issue, and
  nothing else.
- **A `Ready` item that has a PR is board lag, not slip.** It is listed
  separately in section 6b and never counted as never-started — saying work
  that visibly has a PR "never started" is simply false. Even a stale draft PR
  counts as started.
- **A weekend `Dev Start Date` rolls to the following Monday** before the slip
  is measured. `workdays_between` is exclusive of its first argument, so
  measuring straight from Saturday would report a 1-day slip on Monday
  *morning*, when Monday is the real start day.

Routing does not change: the item still goes to its assignee, or to the
*no actor resolved* block if it has none. Only the reason and the tier change.

### 4.11. Ready but not due to start yet

The mirror image of 4.10. Without it, every ticket planned for the sprint sat
in its owner's list from the **first morning of the sprint**, and carryover
started counting from that day. A ticket planned for week 2 hit `CARRY_ASK`
on day 4 and `CARRY_LEADER` on day 5 — while nothing was wrong with it. The
*Escalated* block filled with "not my turn yet" and lost its signal.

The condition, all three parts required:

| # | Condition |
|---|---|
| 1 | `Status` is `Ready` — `In progress` is already started, whatever the date says |
| 2 | `Dev Start Date` is set and is **after** today |
| 3 | No PR exists and the issue is still open (same `has_started()` as 4.10) |

What happens to a matching item:

- It goes to the **Not due to start** block at the end, sorted by start date,
  with its owner shown. Nobody's personal list.
- It is **not written to state** on `--commit`, so `carryover_count` stays 0
  until the item actually enters a list. Carryover therefore measures days
  since the *planned start*, not days since the sprint began.
- It still counts as in scope (`live_keys`), so it is never reported as
  *"Left the list"*.

Two consequences to know:

- **The rule depends on `Dev Start Date` being filled in.** An item without
  one is shown from day 1 exactly as before — hiding undated work silently
  is the failure mode 5.3 exists to prevent. The *Not due to start* block on
  the daily list is the pressure to fill the field in at sprint planning.
- A `Ready` item **with a PR** is shown normally even if its start date is
  ahead: work visibly began, so calling it "not due" would be false. Same
  reasoning as the board-lag note in 4.10.

`--due-within` (5.2) is a different, opt-in filter keyed on `Dev End Date`.
The two can be combined; an item hidden by both lands in *Not due yet*, which
is checked first.

---

## 5. `Dev End Date` — sorting and filtering

Used two ways, kept separate because the risk differs.

### 5.1. Sorting — always on

Every line carries a due marker, and lists are sorted soonest-deadline-first:

```
[OVERDUE 2d]  →  [DUE TODAY]  →  [d-1]  →  [d-5]  →  [no date]
```

`d-N` means N working days remaining. Sorting has no downside even when dates
cluster at sprint end — the order is still right, just less discriminating.

Within `[no date]` only, never-started items (4.10) sort first, by slip. That
keeps deadline-first intact — a never-started item never outranks genuinely
overdue work — while stopping the least visible item in the system from sinking
to the very bottom of the list.

### 5.2. Filtering — opt-in via `--due-within N`

```bash
python3 daily_todo.py --due-within 2 --html todo.html
```

Shows only items due within N working days. Filtered items are **not lost** —
they move to a `NOT DUE YET` block at the end, with a count and a reminder of
how to unhide them.

It defaults to **off** for a measured reason. On sprint Affogato, 18 of 19 dated
items had `Dev End Date` inside the last 3 days of a 21-day sprint. With that
distribution, `--due-within 2` yields a nearly empty list in the first half of
the sprint and then fires on everything at once in the last three days. Turn it
on once dates are spread out — check with `--scan-sprints` (see 9.2).

### 5.3. Undated items are never filtered out

Coverage is currently 86%, so roughly 3 of 22 items have no `Dev End Date`. If
the due filter dropped undated items, that work would **disappear silently** —
nobody sees it, nobody knows it was hidden.

They stay in the list marked `[no date]`, sorted last. The summary line counts
them separately: `no Dev End Date: 2`. That number is the pressure to fill the
field in, and it sits on the list everyone reads every day.

**Never-started items (4.10) are exempt from `--due-within` too**, for the same
reason. Their `Dev End Date` usually sits at sprint end *because* nobody has
touched them, so the filter would hide exactly the work most in need of being
seen. The summary counts them as `not started: N`.

---

## 6. Carryover — escalate, don't repeat

Unfinished work rolls into the next day automatically. But **behaviour changes
with the carry count** instead of repeating the same line. "Times carried" is
the number of earlier mornings the item was on a locked-in list — **not** days
past `Dev End Date` (that is the `[OVERDUE Nd]` marker, 5.1) and not days
since the sprint began (items not yet due to start are excluded, 4.11):

| Times carried | Behaviour |
|---|---|
| 1–2 | Listed normally. No nagging, no leader report |
| 3 | Asks the assignee *"what is blocking this?"* — only they see it |
| 4+ | Surfaces in the leader's escalation block |

The reasoning: the question worth answering isn't *"what's unfinished"* (always:
a lot), it's *"what's stuck that nobody has mentioned"*. Four days carried with
no stated reason is the strongest signal this system can produce.

Thresholds live at the top of `daily_todo.py`:

```python
CARRY_ASK = 3      # from here: ask the assignee what is blocking them
CARRY_LEADER = 4   # from here: surface it to the leader
```

---

## 7. Slack escalation file

Slack isn't wired up yet. The interim intake is a JSON file:

```json
[
  {
    "title": "Fix export flow failing on prod",
    "person": "duc-hoang-trung",
    "approved": true,
    "link": "https://xaion.slack.com/archives/...",
    "note": "from #team-dev-product"
  },
  {
    "title": "Can someone look at the HubSpot rate limit?",
    "person": "lan",
    "approved": false,
    "note": "unclear whether this is real work"
  }
]
```

```bash
python3 daily_todo.py --extra esc.json --html todo.html
```

| Field | Required | Notes |
|---|---|---|
| `title` | yes | The task text |
| `person` | to approve | GitHub login of the person who should do it |
| `approved` | yes | `false` → sits in the awaiting-approval block |
| `link` | no | Slack permalink |
| `note` | no | Displayed as the reason |

---

## 8. State file

`todo_state.jsonl` — append-only, one row per appearance of a task in a
locked-in list:

```jsonl
{"date":"2026-09-08","key":11530,"person":"duc-hoang-trung","source":"github"}
{"date":"2026-09-08","key":"extra:Fix export flow","person":"lan","source":"slack","note":"from #team-dev-product"}
```

Four fields only. The principle: **store only what can't be re-read.**

| Not stored | Because |
|---|---|
| Whether it's done | Derived from GitHub — reaching `Staging` |
| `carryover_count` | Counted from distinct days with the same `key` |
| Title, status, assignee | Re-read from GitHub every run |

Worth committing to git for the history. It contains GitHub logins and nothing
more sensitive than that.


### 8.1. Daily snapshots

`--snapshot DIR` writes the whole built list — every block, with title,
status, reason, deadline, carry count — as `DIR/YYYY-MM-DD.json`. The state
file (8) only records *who had which key*; the snapshot records *what the list
said*. The scheduled workflow commits one per weekday into `snapshots/`.

- A snapshot is **the list as posted at 08:15**, not the board's end-of-day
  state. "What got done on day D" is the difference between D and D+1 — the
  *Left the list* block already computes exactly that.
- HTML is not stored: `--render-snapshot snapshots/2026-09-24.json --html
  x.html` rebuilds it byte-for-byte, with no token and no network.
- This is the file a Timesheet integration should read to answer "what was on
  X's list on day D", so hours can be logged against a past day's items.
- Size: ~10–20 KB per day; a year is under 5 MB.

---

## 9. Three design decisions to know before changing anything

### 9.1. No fallback to the assignee when the reviewer is unknown

For `In review`, the person who must act is the **PR reviewer**, not the
assignee — the assignee is waiting and has nothing to do. The assignee **never
changes** over a ticket's life, so it can't express who has to act today.

When the reviewer can't be resolved, the script routes the task to the *no actor
resolved* block rather than to the assignee. Sending review work to a
non-reviewer is worse than leaving it blank: a few wrong routings and the team
stops reading, and that trust doesn't come back.

If that block dominates the list, the fix is a **convention for assigning
reviewers on PRs**, not a code change.

### 9.2. `Dev End Date` is not used for prioritisation

Measured on sprint Affogato: 58% of items had `Dev End Date` exactly on the
sprint end date, and 18 of 19 fell inside the last 3 days of a 21-day sprint.
A "due within 2 days" rule then fires on 16 of 22 items — that lists nearly the
whole sprint rather than signalling anything.

That sprint may be an exception (a lot of fix work bunched at the end). Check
with:

```bash
python3 autoboost_diagnose.py --scan-sprints 8
```

If previous sprints show a median in the middle of the sprint, the dates are
real estimates and this axis can come back. If every sprint clusters at the end,
it's a default value that can't tell urgent work apart.

Note the date is still used for **sorting and the optional filter** (section 5) —
just not as the priority signal.

### 9.3. The QA branch is deferred

`Staging` means dev work is finished, so it is **out** of the digest's scope.
`QA Needed`, `QA In Progress` and `QA - Dev In Review` are out too — the QA
fields were added recently and have no data yet (`QA End Date` coverage is 0%).

Current scope is exactly four statuses:

| Status | actor | deadline |
|---|---|---|
| `Ready` | owner | `Dev End Date` |
| `In progress` | owner | `Dev End Date` |
| `In review` | reviewer (from the PR) | `Dev End Date` |
| `In External Review` | PdM / leader | `Dev End Date` |

To re-enable QA: flip `flow=False` → `True` in `STATUS_MAP` inside
`autoboost_diagnose.py`. Enable `QA - Dev In Review` **first** — that's dev work
(QA handed it back), so it doesn't need a `QA Assignee` field to be useful.

---

## 10. Diagnostic script

```bash
python3 autoboost_diagnose.py --out report.txt              # 8 core sections
python3 autoboost_diagnose.py --review --out report.txt     # + PR reviewers
python3 autoboost_diagnose.py --scan-sprints 8              # + cross-sprint comparison
python3 autoboost_diagnose.py --json raw.json               # dump normalized data
```

| Section | Answers |
|---|---|
| 0 | Is the right sprint selected? **Item count must match the UI** |
| 1 / 1b | Which statuses are in use; does the board contradict git state |
| 2 | Which fields are missing — `Dev End Date` and `Assignee` are the critical ones |
| 3 | Is `Dev End Date` a real estimate or a default value |
| 4 | Overdue × activity-signal matrix |
| 5 | Sprint progress (% past the dev finish line) |
| 6 | `In progress` over 2 weeks — the team's own convention |
| 6b | `Ready` past its `Dev Start Date` — the never-started rule (4.10) |
| 7 | Other P0s: no owner, `Dev Answer Needed By` overdue, `In External Review` |
| 8 | Total P0 against the 5–7 items/day budget |
| 9 | (`--review`) actor-resolution rate, review load, PR age, open PRs per item |

**Section 0 is a precondition.** The `item` column on the row marked
`<-- spans today` must match what the UI shows when filtering
`dev-sprint:@current`. A mismatch means pagination is incomplete, and every
number below it is meaningless.

**Section 9 decides whether direct sending is viable.** If "with ≥1 linked PR"
and "actor resolved" are below ~90%, the remainder must keep an approval step.

**"Open PRs per item"** measures how often that decision was made on a partial
candidate set. Anything above `1 open` means older open PRs were ignored; each
such ticket is listed with its open PRs, `->` marking the one that decided the
routing. Items where a newer draft outranks an open ready-for-review PR are
called out separately as `HIJACKED BY A DRAFT` — those are actively routed to
the wrong person. Merged and closed PRs never cause this, so a ticket with
several *sequential* PRs is fine; only simultaneously open ones matter.

---

## 11. Troubleshooting

| Symptom | Cause |
|---|---|
| `GITHUB_TOKEN is not set` | Token not exported |
| `Project #2 not found` | Token missing `read:project`, or wrong `--org` |
| Section 9 empty / reviewers 0% | Token missing `repo` scope |
| No section 9 in the report | `--review` flag not passed |
| `unrecognized arguments: --review` | Running an older copy of the script |
| Section 0 item count differs from the UI | Pagination incomplete — please report |
| `! N items have an unmapped Status` | Team added a new status → add it to `STATUS_MAP` |
| `autoboost_diagnose.py not found` | The two files aren't in the same directory |
| Many `NO PR FOUND` | Team uses neither closing keywords nor issue references in PRs |

### When the team adds a new status

The script **never guesses** — an unknown status is named explicitly in section
1b. Add one line to `STATUS_MAP` in `autoboost_diagnose.py`:

```python
"New status name": dict(flow=True, actor="owner", dl="dev_end", phase="dev"),
```

Emoji don't need to be included — `norm_status()` strips them before lookup
(the API returns `💻Staging` and `👀 In review` with the emoji inside the
option name, and the spacing after it is inconsistent).

---

## 12. Not built yet

- **Slack DM delivery.** The list is posted to one channel as code blocks.
  Per-person DMs, and a native Block Kit layout instead of a code block, are
  not built.
- **Reading Slack automatically** for escalation. Currently via `--extra`.
- **Capturing the reason** when something is carried 3+ days. The system asks
  but has nowhere to record the answer. When built, the reason should go into
  state so the escalation block can display it — "carried 4 days, no reason" and
  "carried 4 days, waiting on PdM to settle the spec" are very different
  situations.
- **Both `Dev Start Date` rules measure the planned start, not the actual one.**
  The 2-week SLA (section 6) and the never-started rule (section 6b, 4.10) read
  `Dev Start Date`, not the date the status actually changed — `fieldValues`
  carries no change history. Accuracy would require querying `timelineItems`, at
  the cost of one extra query per item. For 6b the gap is smaller than it looks:
  it asks whether work *began*, and it answers that from PR existence, not from
  the date field.
- **Whether `Dev Start Date` is a real estimate** is not checked. Section 3 does
  this for `Dev End Date` (>40% sitting on the sprint end date means it is a
  default value). There is no equivalent check for `Dev Start Date`, so if most
  items carry the sprint start date, section 6b will fire on nearly every
  `Ready` item from day 2 of the sprint. Watch the 6b counts for a few days
  before trusting them.
