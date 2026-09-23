#!/usr/bin/env python3
"""
daily_todo.py - Generate the daily TODO list for each Product Dev Team member.

Four sources, each with its own mode (see README):
  GitHub     -> automatic, no approval needed
  Carryover  -> automatic, escalates by how many times it was carried
  Escalation -> REQUIRES approval (--extra file), enters the list on the next run
  Escalated  -> reported to the leader only

DRY-RUN BY DEFAULT: writes no state, sends nothing. Use --commit to write.

Requires autoboost_diagnose.py in the same directory (reuses its GitHub reader).

    export GITHUB_TOKEN=ghp_xxx        # scopes: read:project, repo
    python3 daily_todo.py                          # preview
    python3 daily_todo.py --commit                 # lock in today's list
    python3 daily_todo.py --extra esc.json         # include escalation items
"""

import argparse
import json
import os
import sys
import urllib.request
from collections import Counter, defaultdict
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    import autoboost_diagnose as ab
except ImportError:
    sys.exit("autoboost_diagnose.py not found - keep both files in one directory.")

DEFAULT_STATE = os.path.join(HERE, "todo_state.jsonl")

# Escalation by how many times a task has been carried to the next day.
# The point is NOT to repeat the same line, but to CHANGE behaviour by count.
CARRY_ASK = 3      # from here: ask the assignee what is blocking them
CARRY_LEADER = 4   # from here: surface it to the leader


# ---------------------------------------------------------------- state

def load_state(path):
    """[{date, key, person, source, note}] - append-only, one row per appearance."""
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"! skipping unparseable line {ln} in {path}",
                      file=sys.stderr)
    return rows


def append_state(path, entries):
    with open(path, "a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def carry_count(rows, key, today):
    """Number of DISTINCT DAYS before today this task appeared in a locked list.

    carryover_count is never stored - it is counted from state. One source of truth.
    """
    return len({r["date"] for r in rows
                if str(r.get("key")) == str(key) and r.get("date") < str(today)})


def first_seen(rows, key):
    ds = sorted(r["date"] for r in rows if str(r.get("key")) == str(key))
    return ds[0] if ds else None


# ---------------------------------------------------------------- actor

def actor_for(item, prs):
    """Who must act on this TODAY -> (people, reason, pr).

    Different from 'owner' (= assignee, which never changes over a ticket's life).
    For In review the person who must act is the PR reviewer, not the assignee.
    """
    sp = ab.spec(item["status"])
    if not sp:
        return [], "unmapped status", None
    role = sp["actor"]

    if role == "owner":
        return list(item["assignees"]), "owner", None
    if role == "external":
        return [], "waiting on someone outside the team (PdM)", None
    if role == "reviewer":
        actors, why, pr = ab.resolve_actor(prs or [])
        if actors:
            return actors, why, pr
        # Reviewer could not be resolved -> do NOT fall back to the assignee.
        # Sending a review task to a non-reviewer is worse than leaving it blank.
        return [], why, pr
    return list(item["assignees"]), role or "?", None


# ---------------------------------------------------------------- build

def build(items, prmap, rows, today, extra, due_within=None):
    dev_it = ab.current_iteration(items, "dev_sprint", today)
    if not dev_it:
        sys.exit("No Dev Sprint iteration covers today.")

    scope = [i for i in items
             if i["dev_sprint"] and i["dev_sprint"]["title"] == dev_it["title"]
             and ab.in_flow(i) and not i["is_draft"]]
    live_keys = {str(i["key"]) for i in scope}

    tasks, unassigned, leader_only, deferred, upcoming = [], [], [], [], []
    for i in scope:
        prs = prmap.get((i["repo"], i["number"]), [])
        actors, why, pr = actor_for(i, prs)
        cc = carry_count(rows, i["key"], today)
        due = i["dev_end"]
        due_in = ab.workdays_between(today, due) if due else None
        # Computed HERE, not in actor_for(): that function answers ROUTING and
        # takes no date. 'owner' is a true routing reason but a weak one - if
        # the planned start has passed, THAT is why this is on the list today.
        slip = ab.start_slip(i, today)
        if slip is not None:
            why = f"NOT STARTED - start +{slip}d"
        rec = {
            "key": i["key"], "id": ab.nid(i), "title": i["title"],
            "status": i["status"], "url": i["url"], "why": why,
            "carry": cc, "since": first_seen(rows, i["key"]),
            "pr_age": ab.pr_age(pr, today) if pr else None,
            "source": "github", "owner": ",".join(i["assignees"]) or "-",
            "due": due, "due_in": due_in, "slip": slip,
            # 'pr' = the work is on a PR (review, merge, fix after review);
            # 'ticket' = the work is the issue itself. Decided by whether
            # actor_for() routed via a PR - not by pr_age, which can be None
            # for a PR with no createdAt.
            "kind": "pr" if pr else "ticket",
            "_start": str(i["dev_start"]) if i["dev_start"] else "",
        }
        # Due-date filter is OPT-IN. Items with no Dev End Date are NEVER
        # dropped by it — silently hiding undated work is worse than noise.
        # A never-started item is exempt too: its Dev End Date is usually far
        # out precisely because nobody has touched it (README 5.3).
        if (due_within is not None and due_in is not None
                and due_in > due_within and slip is None):
            deferred.append(rec)
            continue
        # Planned but not due to start: a 'Ready' item whose Dev Start Date
        # is still ahead. Listed in its own block, routed to nobody, and NOT
        # written to state - so carryover starts counting on the planned
        # start day, not on day 1 of the sprint (README 4.11).
        if ab.not_due_to_start(i, today):
            upcoming.append(rec)
            continue
        if actors:
            for a in actors:
                tasks.append(dict(rec, person=a))
        elif ab.phase_of(i) == "blocked-ext":
            leader_only.append(rec)
        else:
            unassigned.append(rec)

    # Escalation: only approved items enter the list.
    pending = []
    for e in extra:
        rec = {
            "key": e.get("key") or f"extra:{e.get('title','')[:40]}",
            "id": "(slack)", "title": e.get("title", "(untitled)"),
            "status": "escalation", "url": e.get("link"),
            "why": e.get("note") or "from Slack", "carry": 0, "since": None,
            "pr_age": None, "source": "slack", "owner": e.get("person") or "-",
            "due": None, "due_in": None, "slip": None, "kind": "ticket",
        }
        if e.get("approved") and e.get("person"):
            tasks.append(dict(rec, person=e["person"]))
        else:
            pending.append(rec)

    # Left the list since the previous run = done, or moved out of the sprint
    prev_day = max((r["date"] for r in rows if r["date"] < str(today)), default=None)
    closed = []
    if prev_day:
        for r in rows:
            if r["date"] == prev_day and str(r.get("key")) not in live_keys:
                closed.append(r)

    return {
        "sprint": dev_it["title"],
        "days_left": ab.workdays_between(today, dev_it["end"]),
        "tasks": tasks, "unassigned": unassigned, "leader_only": leader_only,
        "pending": pending, "closed": closed, "prev_day": prev_day,
        "deferred": deferred, "due_within": due_within,
        "upcoming": upcoming,
    }


# ---------------------------------------------------------------- render

def plural(n, word):
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def due_label(t):
    """Compact due marker: OVERDUE / DUE TODAY / d-N / no date."""
    if t["due"] is None:
        return "[no date]"
    n = t["due_in"]
    if n is None:
        return "[no date]"
    if n < 0:
        return f"[OVERDUE {-n}d]"
    if n == 0:
        return "[DUE TODAY]"
    return f"[d-{n}]"


def sort_key(t):
    """Soonest deadline first; undated last; slip, then carryover, break ties.

    Sorting is always on — unlike the filter, ordering by date has no
    downside even when dates are clustered at sprint end.

    slip sits AFTER the date terms on purpose: deadline-first is the promise
    (5.1), so a never-started item must not outrank genuinely overdue work.
    It only reorders the undated group, where a never-started item would
    otherwise sink to the very bottom of the list under [no date].
    """
    return (t["due"] is None, t["due"] or date.max,
            -(t["slip"] or 0), -t["carry"])


def bucket(t):
    if t["carry"] >= CARRY_LEADER:
        return "leader"
    if t["carry"] >= CARRY_ASK:
        return "ask"
    return "normal"


# Display order inside a person's block: PR work first (it blocks someone
# else), then the person's own tickets.
KIND_LABEL = [("pr", "PR"), ("ticket", "Ticket")]


def by_kind(ts):
    """[(label, tasks)] for the NON-empty kinds only, PR before Ticket.

    Only splits - the order inside each group is whatever the caller sorted
    (deadline-first). A person with a single kind gets one group and no
    divider, not an empty 'PR (0)' header.
    """
    return [(label, [t for t in ts if t.get("kind", "ticket") == kind])
            for kind, label in KIND_LABEL
            if any(t.get("kind", "ticket") == kind for t in ts)]


def render_text(d, today):
    L = []
    w = L.append
    w("=" * 70)
    w(f"TODO {today} - sprint {d['sprint']}, "
      f"{plural(d['days_left'], 'working day')} left")
    w("=" * 70)

    byp = defaultdict(list)
    for t in d["tasks"]:
        byp[t["person"]].append(t)

    for who in sorted(byp, key=lambda x: (-len(byp[x]), x)):
        ts = sorted(byp[who], key=sort_key)
        w(f"\n{who} - {plural(len(ts), 'task')}")
        for gi, (label, group) in enumerate(by_kind(ts)):
            if gi:
                # Divider between PR and Ticket only - shorter than the
                # 70-wide rules that separate the trailing blocks.
                w("  " + "-" * 66)
            w(f"  {label} ({len(group)})")
            for t in group:
                tag = ""
                if t["carry"] >= CARRY_LEADER:
                    tag = f"  [carried {t['carry']}d - reported to leader]"
                elif t["carry"] >= CARRY_ASK:
                    tag = f"  [carried {t['carry']}d - what is blocking this?]"
                elif t["carry"]:
                    tag = f"  [carried {t['carry']}d]"
                age = f" PR {t['pr_age']}d" if t["pr_age"] is not None else ""
                # No 'not started' tag here: 'why' already says it in the same
                # line, in full (23 chars, under the 34-char cut). The HTML
                # render does add a tag - there it buys colour, not words.
                w(f"  [ ] {due_label(t):<15}{t['id']:<9} {t['title'][:40]}")
                w(f"      {t['status'][:18]} - {t['why'][:34]}{age}{tag}")

    if d["unassigned"]:
        w(f"\n{'-' * 70}\nNO ACTOR RESOLVED - {plural(len(d['unassigned']), 'task')}")
        w("(reviewer unresolved; NOT routed to the assignee to avoid wrong routing)")
        for t in d["unassigned"]:
            w(f"  [ ] {t['id']:<9} {t['title'][:40]} - {t['why'][:28]}")

    if d["leader_only"]:
        w(f"\nLEADER / PdM - {plural(len(d['leader_only']), 'task')}")
        for t in d["leader_only"]:
            w(f"  [ ] {t['id']:<9} {t['title'][:44]}")

    if d["pending"]:
        w(f"\n{'-' * 70}\nAWAITING APPROVAL (Slack) - {plural(len(d['pending']), 'item')}")
        w("Approve by setting approved=true and person in the --extra file.")
        w("Approved items enter the list on the NEXT run.")
        for t in d["pending"]:
            w(f"  [ ] {t['title'][:46]}")
            w(f"      suggested: {t['owner']} - {t['why'][:40]}")

    esc = [t for t in d["tasks"] if bucket(t) == "leader"]
    if esc:
        w(f"\n{'-' * 70}\nESCALATED - carried >= {CARRY_LEADER} days")
        for t in sorted(esc, key=lambda x: -x["carry"]):
            w(f"  {t['carry']}d  {t['id']:<9} {t['person']:<20} {t['title'][:34]}")
            if t["since"]:
                w(f"      first listed on {t['since']}")

    if d["deferred"]:
        w(f"\n{'-' * 70}\nNOT DUE YET - {plural(len(d['deferred']), 'item')}"
          f" (Dev End Date > +{d['due_within']} working days)")
        w("Hidden by --due-within. Remove the flag to see them.")
        for t in sorted(d["deferred"], key=sort_key)[:15]:
            w(f"      {due_label(t):<15}{t['id']:<9} {t['title'][:38]}"
              f" - {t['owner'][:16]}")
        if len(d["deferred"]) > 15:
            w(f"      ... and {len(d['deferred']) - 15} more")

    if d["upcoming"]:
        w(f"\n{'-' * 70}\nNOT DUE TO START - {plural(len(d['upcoming']), 'item')}"
          " (Ready, Dev Start Date still ahead)")
        w("Planned work. Enters the owner's list on its Dev Start Date;"
          " carryover starts counting from that day.")
        for t in sorted(d["upcoming"], key=lambda t: (t["_start"], t["id"])):
            w(f"      starts {t['_start']}  {t['id']:<9} {t['title'][:38]}"
              f" - {t['owner'][:16]}")

    if d["closed"]:
        w(f"\nLeft the list since {d['prev_day']}: {plural(len(d['closed']), 'task')}")

    w("\n" + "=" * 70)
    parts = [f"{plural(len(d['tasks']), 'task')} / {plural(len(byp), 'person') if len(byp) == 1 else str(len(byp)) + ' people'}"]
    if d["unassigned"]:
        parts.append(f"no actor: {len(d['unassigned'])}")
    if d["deferred"]:
        parts.append(f"not due: {len(d['deferred'])}")
    if d["upcoming"]:
        parts.append(f"not due to start: {len(d['upcoming'])}")
    if d["pending"]:
        parts.append(f"awaiting approval: {len(d['pending'])}")
    overdue = sum(1 for t in d["tasks"]
                  if t["due_in"] is not None and t["due_in"] < 0)
    if overdue:
        parts.append(f"overdue: {overdue}")
    never = sum(1 for t in d["tasks"] if t["slip"] is not None)
    if never:
        parts.append(f"not started: {never}")
    undated = sum(1 for t in d["tasks"] if t["due"] is None)
    if undated:
        parts.append(f"no Dev End Date: {undated}")
    w("Total: " + " | ".join(parts))
    return "\n".join(L)


HTML_HEAD = """<!doctype html><meta charset="utf-8">
<title>TODO %(date)s</title>
<style>
:root{--bg:#0d1117;--fg:#e6edf3;--dim:#8b949e;--line:#21262d;
      --warn:#d29922;--hot:#f85149;--ok:#3fb950;--card:#161b22}
*{box-sizing:border-box}
body{margin:0;padding:24px;background:var(--bg);color:var(--fg);
     font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.wrap{max-width:900px;margin:0 auto}
h1{font-size:19px;margin:0 0 4px}
.sub{color:var(--dim);font-size:13px;margin-bottom:20px}
.person{background:var(--card);border:1px solid var(--line);
        border-radius:10px;padding:14px 16px;margin-bottom:12px}
.person h2{font-size:14px;margin:0 0 10px;display:flex;
           justify-content:space-between;align-items:center}
.n{color:var(--dim);font-weight:400;font-size:12px}
.grp+.grp{border-top:1px dashed var(--line);margin-top:10px;padding-top:8px}
.gh{font-size:11px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;
    color:var(--dim);margin:0 0 4px;display:flex;justify-content:space-between}
.t{display:flex;gap:10px;padding:8px 0;border-top:1px solid var(--line)}
.t:first-of-type{border-top:0}
.t input{margin-top:3px;flex:0 0 auto;accent-color:var(--ok)}
.t .b{min-width:0;flex:1}
.ttl{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ttl a{color:var(--fg);text-decoration:none}
.ttl a:hover{text-decoration:underline}
.meta{color:var(--dim);font-size:12px;margin-top:2px}
.id{color:var(--dim);font-variant-numeric:tabular-nums}
.tag{display:inline-block;padding:1px 6px;border-radius:20px;
     font-size:11px;margin-left:6px;border:1px solid}
.c-ask{color:var(--warn);border-color:var(--warn)}
.c-hot{color:var(--hot);border-color:var(--hot)}
.c-soft{color:var(--dim);border-color:var(--line)}
.box{background:var(--card);border:1px solid var(--line);border-left:3px solid;
     border-radius:8px;padding:12px 16px;margin-bottom:12px}
.box.w{border-left-color:var(--warn)}
.box.h{border-left-color:var(--hot)}
.box.d{border-left-color:var(--dim)}
.box h3{font-size:13px;margin:0 0 6px}
.box p{color:var(--dim);font-size:12px;margin:0 0 8px}
.foot{color:var(--dim);font-size:12px;margin-top:20px;
      border-top:1px solid var(--line);padding-top:12px}
</style>
<div class="wrap">
<h1>TODO %(date)s</h1>
<div class="sub">Sprint %(sprint)s &middot; %(days)s working days left
&middot; %(ntask)s tasks / %(nper)s people</div>
"""


def esc(x):
    return (str(x).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def render_html(d, today):
    byp = defaultdict(list)
    for t in d["tasks"]:
        byp[t["person"]].append(t)
    out = [HTML_HEAD % {"date": today, "sprint": esc(d["sprint"]),
                        "days": d["days_left"], "ntask": len(d["tasks"]),
                        "nper": len(byp)}]

    hot = [t for t in d["tasks"] if bucket(t) == "leader"]
    if hot:
        out.append('<div class="box h"><h3>Escalated &mdash; carried &ge; %d days</h3>'
                   '<p>These have been carried over repeatedly. What is needed is '
                   'to know what is blocking them, not another reminder.</p>'
                   % CARRY_LEADER)
        for t in sorted(hot, key=lambda x: -x["carry"]):
            out.append('<div class="t"><div class="b"><span class="ttl">'
                       '<span class="id">%s</span> %s</span>'
                       '<div class="meta">%s &middot; carried %dd%s</div></div></div>'
                       % (esc(t["id"]), esc(t["title"]), esc(t["person"]),
                          t["carry"],
                          " &middot; since " + esc(t["since"]) if t["since"] else ""))
        out.append("</div>")

    for who in sorted(byp, key=lambda x: (-len(byp[x]), x)):
        ts = sorted(byp[who], key=sort_key)
        out.append('<div class="person"><h2>%s<span class="n">%d tasks</span></h2>'
                   % (esc(who), len(ts)))
        for label, group in by_kind(ts):
            # .grp+.grp draws the dashed divider, so it only appears between
            # the PR and Ticket groups, never above the first one.
            out.append('<div class="grp"><h3 class="gh">%s<span class="n">%d</span></h3>'
                       % (esc(label), len(group)))
            for t in group:
                tag = ""
                if t["carry"] >= CARRY_LEADER:
                    tag = '<span class="tag c-hot">carried %dd</span>' % t["carry"]
                elif t["carry"] >= CARRY_ASK:
                    tag = '<span class="tag c-ask">carried %dd &middot; blocked?</span>' % t["carry"]
                elif t["carry"]:
                    tag = '<span class="tag c-soft">carried %dd</span>' % t["carry"]
                if t["slip"] is not None:
                    tag = ('<span class="tag c-hot">not started &middot; +%dd</span>'
                           % t["slip"]) + tag
                ttl = esc(t["title"])
                if t["url"]:
                    ttl = '<a href="%s" target="_blank">%s</a>' % (esc(t["url"]), ttl)
                age = " &middot; PR %dd" % t["pr_age"] if t["pr_age"] is not None else ""
                dl = due_label(t)
                cls = ("c-hot" if dl.startswith("[OVERDUE") else
                       "c-ask" if dl == "[DUE TODAY]" else "c-soft")
                out.append('<div class="t"><input type="checkbox"><div class="b">'
                           '<span class="ttl"><span class="id">%s</span> %s%s</span>'
                           '<div class="meta"><span class="tag %s">%s</span> '
                           '%s &middot; %s%s</div></div></div>'
                           % (esc(t["id"]), ttl, tag, cls, esc(dl.strip("[]")),
                              esc(t["status"]), esc(t["why"]), age))
            out.append("</div>")
        out.append("</div>")

    if d["unassigned"]:
        out.append('<div class="box w"><h3>No actor resolved &mdash; %d</h3>'
                   '<p>Reviewer could not be resolved from the PR. Not routed to the '
                   'assignee, to avoid sending review work to the wrong person.</p>'
                   % len(d["unassigned"]))
        for t in d["unassigned"]:
            out.append('<div class="t"><div class="b"><span class="ttl">'
                       '<span class="id">%s</span> %s</span>'
                       '<div class="meta">%s</div></div></div>'
                       % (esc(t["id"]), esc(t["title"]), esc(t["why"])))
        out.append("</div>")

    if d["leader_only"]:
        out.append('<div class="box d"><h3>Leader / PdM &mdash; %d</h3>'
                   '<p>Waiting on a decision from outside the team.</p>'
                   % len(d["leader_only"]))
        for t in d["leader_only"]:
            out.append('<div class="t"><input type="checkbox"><div class="b">'
                       '<span class="ttl"><span class="id">%s</span> %s</span></div></div>'
                       % (esc(t["id"]), esc(t["title"])))
        out.append("</div>")

    if d["upcoming"]:
        out.append('<div class="box d"><h3>Not due to start &mdash; %d</h3>'
                   '<p>Planned work: <code>Ready</code> with a Dev Start Date still '
                   'ahead. Each enters its owner\'s list on that date; carryover '
                   'starts counting from then.</p>' % len(d["upcoming"]))
        for t in sorted(d["upcoming"], key=lambda t: (t["_start"], t["id"])):
            out.append('<div class="t"><div class="b"><span class="ttl">'
                       '<span class="id">%s</span> %s</span>'
                       '<div class="meta">starts %s &middot; %s</div></div></div>'
                       % (esc(t["id"]), esc(t["title"]), esc(t["_start"]),
                          esc(t["owner"])))
        out.append("</div>")

    if d["pending"]:
        out.append('<div class="box w"><h3>Awaiting approval (Slack) &mdash; %d</h3>'
                   '<p>Set <code>approved: true</code> and <code>person</code> in the '
                   'escalation file. Approved items enter the list on the next run.</p>'
                   % len(d["pending"]))
        for t in d["pending"]:
            ttl = esc(t["title"])
            if t["url"]:
                ttl = '<a href="%s" target="_blank">%s</a>' % (esc(t["url"]), ttl)
            out.append('<div class="t"><div class="b"><span class="ttl">%s</span>'
                       '<div class="meta">suggested: %s &middot; %s</div></div></div>'
                       % (ttl, esc(t["owner"]), esc(t["why"])))
        out.append("</div>")

    out.append('<div class="foot">These checkboxes are for reading only and do not '
               'persist. Completion is derived from GitHub (reaching Staging). '
               'This list has not been written to state.</div></div>')
    return "\n".join(out)


# ---------------------------------------------------------------- snapshot

SNAPSHOT_LISTS = ("tasks", "unassigned", "leader_only", "pending",
                  "deferred", "upcoming", "closed")


def write_snapshot(d, today, dirpath):
    """Freeze the built list as DIR/YYYY-MM-DD.json - data, not markup.

    The HTML can always be re-rendered from this (see --render-snapshot); the
    reverse is not true. This is also what a Timesheet integration should read
    when it needs "what was on X's list on day D": the list as posted at
    08:15, not the board's end-of-day state.
    """
    os.makedirs(dirpath, exist_ok=True)
    path = os.path.join(dirpath, f"{today}.json")
    payload = {"today": str(today), **d}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1, default=str)
    return path


def load_snapshot(path):
    """Inverse of write_snapshot: dates come back as date objects so the
    renderers' comparisons (sort_key, due_label) keep working."""
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    today = ab.parse_date(d.pop("today"))
    for name in SNAPSHOT_LISTS:
        for t in d.get(name) or []:
            if isinstance(t.get("due"), str):
                t["due"] = ab.parse_date(t["due"])
    return d, today


# ---------------------------------------------------------------- slack

SLACK_CHUNK = 3500   # Slack renders ~4000 chars per message reliably


def post_slack(webhook, text, header):
    """Post the text list to a Slack Incoming Webhook, as code blocks.

    The text renderer's fixed-width layout only survives inside ``` so the
    whole list goes in code blocks, split at line boundaries so no message
    exceeds SLACK_CHUNK chars. The header goes above the first chunk only.
    Any HTTP error is fatal: a missing morning post must fail the cron run
    loudly rather than be silently swallowed.
    """
    chunks, cur = [], ""
    for line in text.splitlines():
        if cur and len(cur) + len(line) + 1 > SLACK_CHUNK:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur:
        chunks.append(cur)
    for n, chunk in enumerate(chunks):
        body = f"{header}\n```{chunk}```" if n == 0 else f"```{chunk}```"
        if len(chunks) > 1:
            body += f"\n_({n + 1}/{len(chunks)})_"
        req = urllib.request.Request(
            webhook, data=json.dumps({"text": body}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            if r.status != 200:
                sys.exit(f"Slack webhook returned {r.status}")
    return len(chunks)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", default="XAION-DATA-Inc")
    ap.add_argument("--project", type=int, default=2)
    ap.add_argument("--state", default=DEFAULT_STATE)
    ap.add_argument("--extra", help="JSON file of escalation items (from Slack)")
    ap.add_argument("--commit", action="store_true",
                    help="write today's list to state (default: no write)")
    ap.add_argument("--html", help="write the HTML version to a file")
    ap.add_argument("--out", help="write the text version to a file")
    ap.add_argument("--due-within", type=int, metavar="N",
                    help="only list items with Dev End Date within N working "
                         "days (opt-in; undated items are always kept)")
    ap.add_argument("--snapshot", metavar="DIR",
                    help="also freeze today's list as DIR/YYYY-MM-DD.json")
    ap.add_argument("--render-snapshot", metavar="FILE",
                    help="re-render an old snapshot (text to stdout, --html "
                         "for HTML) and exit; needs no token and no network")
    ap.add_argument("--slack-webhook", metavar="URL",
                    default=os.environ.get("SLACK_WEBHOOK"),
                    help="post the text list to this Slack Incoming Webhook "
                         "(default: $SLACK_WEBHOOK; unset = do not post)")
    ap.add_argument("--no-review", action="store_true",
                    help="skip phase 2 (faster, but In review loses its actor)")
    ap.add_argument("--today", help="simulate the run date (YYYY-MM-DD)")
    a = ap.parse_args()

    if a.render_snapshot:
        d, today = load_snapshot(a.render_snapshot)
        txt = render_text(d, today)
        print(txt)
        if a.out:
            open(a.out, "w", encoding="utf-8").write(txt)
        if a.html:
            open(a.html, "w", encoding="utf-8").write(render_html(d, today))
            print(f"-> {a.html}", file=sys.stderr)
        return

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("GITHUB_TOKEN is not set (scopes: read:project, repo)")

    today = ab.parse_date(a.today) or date.today()
    extra = []
    if a.extra:
        with open(a.extra, encoding="utf-8") as f:
            extra = json.load(f)
        if isinstance(extra, dict):
            extra = extra.get("items", [])

    print("Reading GitHub project...", file=sys.stderr)
    title, raw = ab.fetch_items(token, a.org, a.project)
    items = [ab.normalize(x) for x in raw]

    prmap = {}
    if not a.no_review:
        dev_it = ab.current_iteration(items, "dev_sprint", today)
        need = [i for i in items
                if dev_it and i["dev_sprint"]
                and i["dev_sprint"]["title"] == dev_it["title"]
                and ab.in_flow(i) and not i["is_draft"]
                and (ab.spec(i["status"]) or {}).get("actor") == "reviewer"]
        if need:
            print(f"Reading reviewers for {len(need)} PRs...", file=sys.stderr)
            prmap = ab.fetch_reviews(token, need)

    rows = load_state(a.state)
    d = build(items, prmap, rows, today, extra, a.due_within)

    txt = render_text(d, today)
    print(txt)
    if a.out:
        open(a.out, "w", encoding="utf-8").write(txt)
        print(f"\n-> {a.out}", file=sys.stderr)
    if a.html:
        open(a.html, "w", encoding="utf-8").write(render_html(d, today))
        print(f"-> {a.html}", file=sys.stderr)
    if a.snapshot:
        print(f"-> {write_snapshot(d, today, a.snapshot)}", file=sys.stderr)
    if a.slack_webhook:
        hdr = (f":clipboard: *Daily TODO — {today}*  ·  sprint {d['sprint']}"
               f"  ·  {d['days_left']} working days left")
        n = post_slack(a.slack_webhook, txt, hdr)
        print(f"-> Slack ({plural(n, 'message')})", file=sys.stderr)

    if a.commit:
        already = {(r["date"], str(r.get("key")), r.get("person")) for r in rows}
        new = [{"date": str(today), "key": t["key"], "person": t["person"],
                "source": t["source"],
                **({"note": t["why"]} if t["source"] != "github" else {})}
               for t in d["tasks"]
               if (str(today), str(t["key"]), t["person"]) not in already]
        if new:
            append_state(a.state, new)
            print(f"\nWrote {len(new)} rows to {a.state}")
        else:
            print("\nNo new rows (today's list was already locked in).")
    else:
        print("\n[DRY-RUN] State not written. Add --commit to lock in today's list.")
        print("          Nothing was sent to anyone - delivery is not built yet.")


if __name__ == "__main__":
    main()
