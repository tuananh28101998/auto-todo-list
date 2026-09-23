#!/usr/bin/env python3
"""
autoboost_diagnose.py — Chan doan du lieu GitHub Project truoc khi xay TODO digest.

Read-only. Never writes anything to GitHub.

Usage:
    export GITHUB_TOKEN=ghp_xxx          # PAT co scope: read:project, repo
    python3 autoboost_diagnose.py

Tuy chon:
    --org XAION-DATA-Inc
    --project 2
    --json out.json      # dump normalized data for your own analysis
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

API = "https://api.github.com/graphql"

# ---------------------------------------------------------------- STATUS MAP
# Single source of truth for phase/actor/deadline. Edit here, nowhere else.
#   flow   : is it in the working pipeline (False -> excluded from all stats)
#   actor  : who must act THIS TURN. Not 'owner' (= assignee, never changes).
#   dl     : which date field is the deadline for this turn
STATUS_MAP = {
    "Backlog":            dict(flow=False, actor=None,       dl=None,       phase="parked"),
    "Management":         dict(flow=False, actor=None,       dl=None,       phase="parent"),
    "Done":               dict(flow=False, actor=None,       dl=None,       phase="terminal"),

    "Ready":              dict(flow=True,  actor="owner",    dl="dev_end",  phase="dev"),
    "In progress":        dict(flow=True,  actor="owner",    dl="dev_end",  phase="dev"),
    "In review":          dict(flow=True,  actor="reviewer", dl="dev_end",  phase="dev-review"),
    "In External Review": dict(flow=True,  actor="external", dl="dev_end",  phase="blocked-ext"),
    # Reaching Staging = dev work is done -> out of the digest's scope
    "Staging":            dict(flow=False, actor=None,       dl=None,       phase="dev-done"),

    # QA branch: fully DEFERRED. QA fields were added recently, so no data yet.
    # Including 'QA - Dev In Review' (QA handed it back to dev) - also out of scope.
    # Re-enable by flipping flow=False -> True once QA Assignee + QA End Date exist.
    "QA Needed":          dict(flow=False, actor=None,       dl=None,       phase="qa-deferred"),
    "QA In Progress":     dict(flow=False, actor=None,       dl=None,       phase="qa-deferred"),

    "QA - Dev In Review": dict(flow=False, actor=None,       dl=None,       phase="qa-deferred"),
}

# Past the dev finish line (used to measure sprint progress, generates no tasks)
DEV_DONE_PHASES = {"dev-done", "qa-deferred", "terminal"}

def in_flow(it):
    sp = spec(it["status"])
    return bool(sp and sp["flow"])


def phase_of(it):
    sp = spec(it["status"])
    return sp["phase"] if sp else None




# Beyond this many days overdue, the updatedAt fallback is no longer trusted
WEAK_SIGNAL_MAX_OVERDUE = 7

# A planned start that has slipped MORE than this many WORKING days is P0;
# 1..this is P1. Measured from Dev Start Date = the PLANNED start. There is no
# change history to read the actual one from - same caveat as section 6.
START_SLIP_P0 = 2


def norm_status(s):
    """The API returns the emoji INSIDE the option name ('\U0001f4bbStaging',
    '\U0001f440 In review'), and the space after it is inconsistent. Normalize first."""
    if not s:
        return None
    keep = [ch for ch in s if ch.isalnum() or ch in " -/()_"]
    return " ".join("".join(keep).split()).lower()


STATUS_NORM = {norm_status(k): v for k, v in STATUS_MAP.items()}


def spec(status):
    """Unmapped status -> None so it gets flagged loudly, never guessed."""
    return STATUS_NORM.get(norm_status(status))


def deadline_of(it):
    sp = spec(it["status"])
    if not sp or not sp["dl"]:
        return None, None
    return it.get(sp["dl"]), sp["dl"]

QUERY = """
query($org:String!, $num:Int!, $cursor:String) {
  organization(login:$org) {
    projectV2(number:$num) {
      title
      items(first:100, after:$cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          content {
            __typename
            ... on Issue {
              number title url state updatedAt closedAt
              repository { nameWithOwner }
              assignees(first:5){ nodes{ login } }
              closedByPullRequestsReferences(first:5){
                nodes{ number state isDraft updatedAt }
              }
            }
            ... on PullRequest {
              number title url state isDraft updatedAt mergedAt
            }
            ... on DraftIssue { title }
          }
          fieldValues(first:30){
            nodes{
              __typename
              ... on ProjectV2ItemFieldDateValue {
                date field{ ... on ProjectV2FieldCommon { name } } }
              ... on ProjectV2ItemFieldSingleSelectValue {
                name field{ ... on ProjectV2FieldCommon { name } } }
              ... on ProjectV2ItemFieldNumberValue {
                number field{ ... on ProjectV2FieldCommon { name } } }
              ... on ProjectV2ItemFieldTextValue {
                text field{ ... on ProjectV2FieldCommon { name } } }
              ... on ProjectV2ItemFieldIterationValue {
                title startDate duration
                field{ ... on ProjectV2FieldCommon { name } } }
            }
          }
        }
      }
    }
  }
}
"""


# ---------------------------------------------------------------- API

def gql_raw(token, query, variables=None):
    return _post(token, {"query": query, "variables": variables or {}})


def gql(token, variables):
    return _post(token, {"query": QUERY, "variables": variables})


def _post(token, payload_in):
    body = json.dumps(payload_in).encode()
    req = urllib.request.Request(
        API,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "GraphQL-Features": "projects_next_graphql",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            payload = json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read().decode()[:500]}")
    except urllib.error.URLError as e:
        sys.exit(f"Cannot reach api.github.com: {e.reason}")

    if "errors" in payload:
        msgs = "; ".join(x.get("message", "?") for x in payload["errors"])
        sys.exit(f"GraphQL loi: {msgs}")
    return payload["data"]


def fetch_items(token, org, num):
    items, cursor, title, pages = [], None, None, 0
    while True:
        data = gql(token, {"org": org, "num": num, "cursor": cursor})
        proj = (data.get("organization") or {}).get("projectV2")
        if not proj:
            sys.exit(f"Project #{num} not found in org {org} "
                     f"(check the org name and the token's read:project scope).")
        title = proj["title"]
        blk = proj["items"]
        items.extend(blk["nodes"])
        pages += 1
        if not blk["pageInfo"]["hasNextPage"]:
            break
        cursor = blk["pageInfo"]["endCursor"]
        if pages % 5 == 0:
            print(f"  ... {len(items)} item", file=sys.stderr)
        if pages > 200:  # infinite-loop guard only; should never be reached
            print("! Stopped at 200 pages - please report this",
                  file=sys.stderr)
            break
    return title, items


# ---------------------------------------------------------------- normalize

def parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def flatten(item):
    """fieldValues -> flat dict. Keyed by field.name, NEVER by position."""
    out = {}
    for fv in (item.get("fieldValues") or {}).get("nodes") or []:
        fname = ((fv.get("field") or {}).get("name"))
        if not fname:
            continue
        t = fv.get("__typename")
        if t == "ProjectV2ItemFieldDateValue":
            out[fname] = parse_date(fv.get("date"))
        elif t == "ProjectV2ItemFieldSingleSelectValue":
            out[fname] = fv.get("name")
        elif t == "ProjectV2ItemFieldNumberValue":
            out[fname] = fv.get("number")
        elif t == "ProjectV2ItemFieldTextValue":
            out[fname] = fv.get("text")
        elif t == "ProjectV2ItemFieldIterationValue":
            start = parse_date(fv.get("startDate"))
            dur = fv.get("duration") or 0
            out[fname] = {
                "title": fv.get("title"),
                "start": start,
                "duration": dur,
                # GitHub: end = start + duration - 1 (inclusive)
                "end": (start + timedelta(days=dur - 1)) if start and dur else None,
            }
    return out


def normalize(item):
    c = item.get("content") or {}
    f = flatten(item)
    prs = ((c.get("closedByPullRequestsReferences") or {}).get("nodes")) or []
    num = c.get("number")
    ttl = c.get("title") or "(untitled)"
    return {
        "type": c.get("__typename"),
        "is_draft": c.get("__typename") == "DraftIssue",
        # DraftIssue has no number -> needs a stable key for P0 dedupe
        "key": num if num is not None else f"draft:{ttl[:60]}",
        "number": num,
        "title": c.get("title") or "(untitled)",
        "url": c.get("url"),
        "repo": ((c.get("repository") or {}).get("nameWithOwner")),
        "state": c.get("state"),          # OPEN / CLOSED / MERGED
        "updated": parse_date(c.get("updatedAt")),
        "assignees": [a["login"] for a in
                      ((c.get("assignees") or {}).get("nodes") or [])],
        "prs": prs,
        "status": f.get("Status"),
        "dev_sprint": f.get("Dev Sprint"),
        "qa_sprint": f.get("QA Sprint"),
        "dev_start": f.get("Dev Start Date"),
        "dev_end": f.get("Dev End Date"),
        "qa_start": f.get("QA Start Date"),
        "qa_end": f.get("QA End Date"),
        "due": f.get("Due date"),
        "answer_by": f.get("Dev Answer Needed By"),
        "sp": f.get("Dev Story point"),
        "actual_h": f.get("Dev Actual Hours"),
        "priority_no": f.get("Dev Priority (No.)"),
        "clarity": f.get("Dev Uncertainty/Clarity"),
        "notion": f.get("Notion"),
    }


# ---------------------------------------------------------------- helpers

def workdays_between(a, b):
    """Working days (Mon-Fri) from a to b, exclusive of a, inclusive of b."""
    if not a or not b:
        return None
    step = 1 if b >= a else -1
    n, cur = 0, a
    while cur != b:
        cur += timedelta(days=step)
        if cur.weekday() < 5:
            n += step
    return n


def current_iteration(items, key="dev_sprint", today=None):
    """Find the iteration that spans today, across all items."""
    today = today or date.today()
    for it in items:
        s = it.get(key)
        if s and s["start"] and s["end"] and s["start"] <= today <= s["end"]:
            return s
    return None


def has_activity(it, today, days=3, overdue=None):
    if it.get("state") == "CLOSED":
        return True, "issue is CLOSED (PR merged) - dev done, waiting on QA"
    """Activity signal. Prefers PR state, falls back to updatedAt only if needed."""
    for pr in it["prs"]:
        if not pr.get("isDraft"):
            return True, "has a non-draft PR"
        d = parse_date(pr.get("updatedAt"))
        if d and (today - d).days <= days:
            return True, "draft PR updated recently"
    if it["prs"]:
        return False, "only draft PRs, no recent updates"
    # No PR at all. updatedAt is a WEAK signal (editing a field bumps it too),
    # so it must not rescue a long-overdue item: 'In progress' for many days
    # with no PR opened is genuinely stuck, even if the board was just tidied.
    if overdue is not None and overdue > WEAK_SIGNAL_MAX_OVERDUE:
        return False, (f"no PR at all and {overdue}d overdue "
                       f"(updatedAt ignored)")
    if it["updated"] and (today - it["updated"]).days <= days:
        return True, f"issue updatedAt within {days}d (weak signal)"
    return False, "no PR, no recent updates"


def has_started(it):
    """Did work actually BEGIN? -> (bool, reason)

    Deliberately NARROWER than has_activity(): updatedAt is excluded entirely.
    'Ready' means not started, so having no PR is the NORMAL state - and a
    field edit on the board is not evidence that anyone opened an editor.
    Letting updatedAt count here would let ONE bulk board edit (re-triage,
    setting Dev Sprint across the sprint, fixing a title) silence the rule for
    the whole team for 3 days.

    A stale draft PR still counts as started: the work began, so calling it
    'never started' would be a false statement about it.
    """
    if it.get("state") == "CLOSED":
        return True, "issue is CLOSED - the board is lagging, not un-started"
    if it["prs"]:
        return True, ("has a PR (#%s) - the board is lagging, not un-started"
                      % it["prs"][0].get("number"))
    return False, "no PR, issue still open - never started"


def not_due_to_start(it, today):
    """A 'Ready' item whose planned start is still in the future -> True.

    The mirror image of start_slip(): that rule fires when Dev Start Date has
    PASSED with no PR; this one fires when it has NOT ARRIVED yet. Such an
    item is planned work, not today's work, so it must not sit in anyone's
    list and must not accumulate carryover from day 1 of the sprint.

    Same narrowness as has_started(): a PR (even a draft) or a CLOSED issue
    means work began, so the item is shown normally regardless of the date.
    Only 'Ready' qualifies - 'In progress' is by definition already started.
    No Dev Start Date -> False: the field is the only signal, and an item
    without it is shown (hiding it silently would be worse, see README 5.3).
    """
    if norm_status(it["status"]) != "ready":
        return False
    ds = it["dev_start"]
    if not ds or ds <= today:
        return False
    return not has_started(it)[0]


def start_slip(it, today):
    """Working days a 'Ready' item is past its planned start, else None.

    None = the rule does not apply: wrong status, no Dev Start Date, not due to
    start yet, or work already began. A positive int is the slip in WORKING
    days; > START_SLIP_P0 is P0, 1..START_SLIP_P0 is P1.

    Status is matched by name, not through STATUS_MAP: 'Ready' is the only
    status that means not-started, and section 6 already reads 'in progress'
    the same way. One rule does not earn a new STATUS_MAP key.
    """
    if norm_status(it["status"]) != "ready":
        return None
    ds = it["dev_start"]
    if not ds:
        return None
    # A weekend start date really means the following Monday. Measuring from
    # Saturday would report a 1-day slip on Monday MORNING, because
    # workdays_between is exclusive of `a`: (Sat -> Mon) == 1.
    while ds.weekday() >= 5:
        ds += timedelta(days=1)
    slip = workdays_between(ds, today)
    if slip is None or slip <= 0:
        return None
    if has_started(it)[0]:
        return None
    return slip


def bar(n, total, width=28):
    if not total:
        return ""
    return "#" * max(1, round(n / total * width)) if n else ""


def nid(i):
    """DraftIssue has no number -> render distinctly instead of crashing."""
    return f"#{i['number']}" if i["number"] is not None else "(draft)"


def stx(i, w=16):
    return (i["status"] or "(rong)")[:w]


def hdr(t):
    print(f"\n{'=' * 68}\n{t}\n{'=' * 68}")


class Tee:
    """Write to stdout and a file at once, so you can watch and keep a copy."""

    def __init__(self, path):
        self.f = open(path, "w", encoding="utf-8")

    def write(self, x):
        sys.__stdout__.write(x)
        self.f.write(x)

    def flush(self):
        sys.__stdout__.flush()
        self.f.flush()

    def close(self):
        self.f.close()


# ---------------------------------------------------------------- report

def scan_sprints(items, n_sprints, today):
    """Compare how Dev End Date is set across several sprints.

    Purpose: tell apart 'real estimates that happen to cluster this sprint'
    from 'a default value, every sprint looks like this'. One sprint cannot
    answer that.

    Scope note: in past sprints everything is already Done/Staging, so
    in_flow() would return ~0. This measures DATE-SETTING BEHAVIOUR, not work
    in flight -> take every real item (minus drafts and Management),
    regardless of current status.
    """
    hdr(f"SCAN OF {n_sprints} SPRINTS - IS DEV END DATE AN ESTIMATE OR A DEFAULT?")

    its = {}
    for i in items:
        s_ = i.get("dev_sprint")
        if s_ and s_["title"] and s_["start"] and s_["end"]:
            its.setdefault(s_["title"], s_)
    order = sorted(its.values(), key=lambda x: x["start"], reverse=True)[:n_sprints]

    print(f"{'Sprint':<24}{'days':>5}{'item':>6}{'dated':>9}"
          f"{'=end':>7}{'last 3 days':>13}{'median':>10}")
    print("-" * 78)
    rows = []
    for it in order:
        grp = [i for i in items
               if i["dev_sprint"] and i["dev_sprint"]["title"] == it["title"]
               and not i["is_draft"] and phase_of(i) != "parent"]
        dated = [i for i in grp if i["dev_end"]]
        if not dated:
            print(f"{it['title'][:23]:<24}{it['duration']:>5}{len(grp):>6}"
                  f"{'0':>9}{'-':>7}{'-':>13}{'-':>10}")
            continue
        exact = sum(1 for i in dated if i["dev_end"] == it["end"])
        tail = sum(1 for i in dated
                   if 0 <= (it["end"] - i["dev_end"]).days <= 2)
        offs = sorted((i["dev_end"] - it["start"]).days for i in dated)
        med = offs[len(offs) // 2]
        pe = exact / len(dated) * 100
        pt = tail / len(dated) * 100
        rows.append((it["title"], pe, pt, med, it["duration"]))
        print(f"{it['title'][:23]:<24}{it['duration']:>5}{len(grp):>6}"
              f"{len(dated):>9}{pe:>6.0f}%{pt:>12.0f}%"
              f"{str(med)+'/'+str(it['duration']):>10}")

    if len(rows) < 2:
        print("\nNot enough sprints to compare.")
        return

    cur = rows[0]
    past = rows[1:]
    avg_e = sum(r[1] for r in past) / len(past)
    avg_t = sum(r[2] for r in past) / len(past)
    print(f"\nCurrent sprint      : exactly on end date {cur[1]:.0f}%  "
          f"| within last 3 days {cur[2]:.0f}%")
    print(f"Average of {len(past)} previous: {avg_e:.0f}%  | {avg_t:.0f}%")

    print()
    if cur[2] - avg_t > 20:
        print("-> This sprint clusters at the end UNUSUALLY hard vs previous ones.")
        print("   The dates are real estimates; keep the 'due soon' threshold as is.")
        print("   Treat this sprint as an exception rather than changing the rule.")
    elif avg_t > 60:
        print("-> Every sprint clusters at the end. This is a default value, not")
        print("   an estimate -> the due-date axis cannot tell urgent work apart.")
    else:
        print("-> Clustering matches previous sprints and is not extreme. The")
        print("   due-date axis is usable, but a fixed 2-day threshold fires on most")
        print("   items -> use a percentile threshold instead of a day count.")
    print("\nThe 'median' column is day N / sprint length. If it always sits near")
    print("the sprint length, nothing is ever given a deadline in the first half.")


def report(proj_title, raw, today):
    items = [normalize(i) for i in raw]

    print(f"\nProject: {proj_title}")
    print(f"Run date: {today}")
    print(f"Total items fetched: {len(items)}")

    # --- 0. pick the sprint. Print EVERY iteration spanning today, to verify.
    hdr("0. SPRINT SELECTION")
    seen = {}
    for i in items:
        it = i.get("dev_sprint")
        if it and it["title"]:
            seen.setdefault(it["title"], {"it": it, "n": 0})["n"] += 1
    live = [v for v in seen.values()
            if v["it"]["start"] and v["it"]["end"]
            and v["it"]["start"] <= today <= v["it"]["end"]]
    print(f"{'Dev Sprint':<28}{'start':<12}{'end':<12}{'days':>5}{'item':>7}")
    print("-" * 68)
    for v in sorted(seen.values(), key=lambda x: x["it"]["start"] or date.min,
                    reverse=True)[:8]:
        it = v["it"]
        mark = " <-- spans today" if v in live else ""
        print(f"{it['title'][:27]:<28}{str(it['start']):<12}"
              f"{str(it['end']):<12}{it['duration']:>5}{v['n']:>7}{mark}")
    if len(live) > 1:
        print("\n! Multiple iterations span today -> check the field config")
    dev_it = live[0]["it"] if live else None
    if not dev_it:
        print("\n! No Dev Sprint iteration spans today")
        return items
    left = workdays_between(today, dev_it["end"])
    print(f"\nSelected: {dev_it['title']} - {left} working days left")
    print("-> The last column MUST match what the UI shows when filtering by")
    print("   dev-sprint:@current. A large gap means pagination is incomplete.")

    cur = [i for i in items
           if i["dev_sprint"] and i["dev_sprint"]["title"] == dev_it["title"]]
    drafts = [i for i in cur if i["is_draft"]]
    work = [i for i in cur if in_flow(i) and not i["is_draft"]]

    print(f"\nItems in sprint: {len(cur)}")
    for st in sorted({i["status"] for i in cur if not in_flow(i) and spec(i["status"])}):
        k = sum(1 for i in cur if i["status"] == st)
        print(f"  - excluded [{st}] ({spec(st)['phase']}): {k}")
    if drafts:
        print(f"  - excluded [draft item] (not converted to an issue): {len(drafts)}")
    print(f"  - In the working pipeline: {len(work)}")
    print("  (filtered by Status, NOT by issue state - see section 1b)")

    # --- 1. phan bo status
    hdr("1. STATUS DISTRIBUTION")
    cnt = Counter((i["status"] or "(rong)") for i in cur)
    st_state = defaultdict(Counter)
    for i in cur:
        st_state[i["status"] or "(rong)"][i["state"] or "?"] += 1
    print(f"{'Status (API name, emoji included)':<32}{'n':>4}  {'OPEN/CLOSED':<13}")
    print("-" * 68)
    for st, n in cnt.most_common():
        mix = st_state[st]
        ok = "" if spec(st) else "  ! unmapped"
        print(f"{st[:31]:<32}{n:>4}  "
              f"{str(mix.get('OPEN',0))+'/'+str(mix.get('CLOSED',0)):<13}{ok}")

    # --- 1b. status vs git state
    hdr("1b. STATUS vs GIT STATE  (CLOSED != done)")
    unknown = [i for i in cur if not spec(i["status"])]
    if unknown:
        print(f"! {len(unknown)} items have an unmapped Status -> add to STATUS_MAP")
        for st, k in Counter(i["status"] or "(empty - Status not set)"
                             for i in unknown).most_common():
            print(f"    [{st}]  {k}")
        print()
    lag = [i for i in work if i["state"] == "CLOSED"]
    print(f"Still in the pipeline but the issue is CLOSED: {len(lag)}")
    if lag:
        for ph, k in Counter(phase_of(i) for i in lag).most_common():
            print(f"    phase={ph}: {k}")
        print("\n  Merging a PR auto-closes the issue. But Staging only means")
        print("  'deployed to app-dev', and Done requires 'approvals from QA'. So:")
        print("  code done, issue closed, WORK NOT DONE. Must not be filtered out.")
    if drafts:
        print(f"\nDraft items (not converted to issues): {len(drafts)}")
        print("  No number, no assignee, cannot have a PR.")
        print("  -> excluded from classification, else each becomes a false 'no owner' P0.")
        for i in drafts[:10]:
            print(f"    {stx(i):<17} {i['title'][:44]}")
        if len(drafts) > 10:
            print(f"    ... and {len(drafts)-10} more")

    done_open = [i for i in cur
                 if phase_of(i) == "terminal" and i["state"] == "OPEN"]
    print(f"\nStatus=Done but the issue is still OPEN: {len(done_open)}")
    parked = [i for i in cur if phase_of(i) == "parked"]
    print(f"[Backlog] but still carrying Dev Sprint = current: {len(parked)}")
    for i in parked[:8]:
        print(f"    {nid(i):<9} {i['title'][:46]}")

    # --- 2. coverage
    hdr("2. FIELD COVERAGE  (pipeline items only)")
    n = len(work) or 1
    for label, get, crit in [
        ("Dev End Date", lambda i: i["dev_end"], True),
        ("Dev Start Date", lambda i: i["dev_start"], False),
        ("Dev Story point", lambda i: i["sp"], False),
        ("Notion (link)", lambda i: i["notion"], False),
        ("Assignee", lambda i: i["assignees"], True),
        ("QA End Date  (n/a)", lambda i: i["qa_end"], False),
        ("Due date     (n/a)", lambda i: i["due"], False),
    ]:
        have = sum(1 for i in work if get(i))
        pct = have / n * 100
        flag = "  <-- below 80%" if pct < 80 and crit else ""
        print(f"{label:<22}{have:>5}{n - have:>7}{pct:>6.0f}%{flag}")
    print("\n(n/a) = no rule uses it -> 0% is not a problem.")
    print("The digest's only time source is Dev End Date.")

    # --- 3. dev end date co that
    hdr("3. IS DEV END DATE A REAL ESTIMATE?")
    dated = [i for i in work if i["dev_end"]]
    same = [i for i in dated if i["dev_end"] == dev_it["end"]]
    pct = len(same) / len(dated) * 100 if dated else 0
    print(f"Sprint end date: {dev_it['end']}   items with Dev End Date: {len(dated)}")
    print(f"Exactly on the sprint end date: {len(same)}  ({pct:.0f}%)")
    print("! >40% means it is a default value, unusable for early warning"
          if pct > 40 else "-> Below 40%: the dates look like real estimates.")
    if dated:
        dist = Counter((i["dev_end"] - dev_it["start"]).days for i in dated)
        for d in sorted(dist):
            print(f"  day +{d:<3} {dist[d]:>3}  {bar(dist[d], len(dated))}")

    # --- 4. overdue x activity
    hdr("4. OVERDUE x ACTIVITY SIGNAL")
    buckets = defaultdict(list)
    for i in work:
        dl, _ = deadline_of(i)
        if not dl:
            buckets["blind (no deadline)"].append(i)
            continue
        od = (today - dl).days
        act, _ = has_activity(i, today, overdue=od)
        if od > 0:
            buckets["overdue + NOT moving" if not act
                    else "overdue + still moving"].append(i)
        elif od >= -2:
            buckets["due soon (<=2 days)"].append(i)
        else:
            buckets["not due yet"].append(i)
    for k in ["overdue + NOT moving", "overdue + still moving",
              "due soon (<=2 days)", "blind (no deadline)", "not due yet"]:
        v = buckets.get(k, [])
        tag = "P0" if k == "overdue + NOT moving" else (
            "P1" if k.startswith(("overdue", "due soon")) else "--")
        print(f"[{tag}] {k:<30}{len(v):>4}")
    worst = sorted(buckets.get("overdue + NOT moving", []),
                   key=lambda i: (today - deadline_of(i)[0]).days, reverse=True)[:8]
    if worst:
        print("\n8 longest-stuck items:")
        for i in worst:
            dl, _ = deadline_of(i)
            _, why = has_activity(i, today, overdue=(today - dl).days)
            who = ",".join(i["assignees"]) or "NO OWNER"
            # Root cause, when there is one: overdue is the symptom, never
            # having started is the reason. Section 6b lists these in full.
            ns = start_slip(i, today)
            print(f"  +{(today - dl).days:>3}d  {nid(i):<9} "
                  f"{stx(i):<17} {who:<16} {i['title'][:32]}")
            print(f"        {why}"
                  + (f"  (never started +{ns}wd - see 6b)"
                     if ns is not None else ""))

    # --- 5. dev progress for the sprint
    hdr("5. DEV PROGRESS  (finish line = reaching Staging)")
    done_ph = [i for i in cur if phase_of(i) in DEV_DONE_PHASES]
    by_ph = Counter(phase_of(i) for i in done_ph)
    tot = len(work) + len(done_ph)
    pct = len(done_ph) / tot * 100 if tot else 0
    print(f"Past the dev finish line: {len(done_ph)}/{tot}  ({pct:.0f}%)")
    for ph, k in by_ph.most_common():
        print(f"    {ph:<14}{k:>4}")
    print(f"Still in the dev phase:   {len(work)}")
    for ph, k in Counter(phase_of(i) for i in work).most_common():
        print(f"    {ph:<14}{k:>4}")
    print(f"\n{left} working days left. This ratio is the only thing in the report")
    print("that speaks to sprint health - everything else is per-item blockers.")
    print("\n(QA branch is DEFERRED by request: Staging / QA Needed / QA In Progress")
    print(" are all flow=False. Re-enable by flipping flow=True in STATUS_MAP")
    print(" once QA Assignee and QA End Date are populated.)")

    # --- 6. SLA 2 tuan
    hdr("6. IN PROGRESS OVER 2 WEEKS  (the team's own convention)")
    ip = [i for i in work if phase_of(i) == "dev"
          and norm_status(i["status"]) == "in progress"]
    print(f"In progress items: {len(ip)}   (measured from Dev Start Date, approx)")
    flagged = [i for i in ip if i["dev_start"]
               and (today - i["dev_start"]).days > 14]
    print(f"Over 14 days: {len(flagged)}")
    for i in flagged:
        print(f"  +{(today - i['dev_start']).days:>3}d  {nid(i):<9} "
              f"{','.join(i['assignees']) or 'NO OWNER':<18} {i['title'][:34]}")
    nostart = sum(1 for i in ip if not i["dev_start"])
    if nostart:
        print(f"! {nostart} items have no Dev Start Date -> cannot be measured")

    # --- 6b. chua start theo ke hoach
    # Lettered, not numbered, so sections 7/8/9 keep the numbers the README
    # documents - same reason section 1b is lettered.
    hdr("6b. READY BUT NEVER STARTED  (Dev Start Date has passed)")
    rdy = [i for i in work if phase_of(i) == "dev"
           and norm_status(i["status"]) == "ready"]
    slipped, lagging = [], []
    for i in rdy:
        s = start_slip(i, today)
        if s is not None:
            slipped.append((s, i))
        elif i["dev_start"] and has_started(i)[0]:
            lagging.append(i)
    slipped.sort(key=lambda x: -x[0])
    p0_slip = [i for s, i in slipped if s > START_SLIP_P0]
    p1_slip = [i for s, i in slipped if s <= START_SLIP_P0]
    print(f"Ready items: {len(rdy)}   (slip in WORKING days from Dev Start Date)")
    print(f"[P0] {'slipped more than %d working days' % START_SLIP_P0:<34}"
          f"{len(p0_slip):>4}")
    print(f"[P1] {'slipped 1-%d working days' % START_SLIP_P0:<34}"
          f"{len(p1_slip):>4}")
    for s, i in slipped:
        dl, _ = deadline_of(i)
        mark = ("  also OVERDUE (also in section 4)" if dl and dl < today
                else "  no Dev End Date - section 4 is blind to it" if not dl
                else "")
        print(f"  [{'P0' if s > START_SLIP_P0 else 'P1'}] +{s:>2}wd  {nid(i):<9} "
              f"{','.join(i['assignees']) or 'NO OWNER':<18} "
              f"{i['title'][:30]}{mark}")
    if lagging:
        print(f"\nReady items that already have a PR or a closed issue: "
              f"{len(lagging)}")
        print("  -> work started, the board did not move. NOT counted above.")
        for i in lagging[:8]:
            print(f"    {nid(i):<9} {has_started(i)[1]}")
    nostart_rdy = sum(1 for i in rdy if not i["dev_start"])
    if nostart_rdy:
        print(f"\n! Ready items with no Dev Start Date: {nostart_rdy}"
              f" -> unmeasurable")
    print("\n-> Nothing fired on these before: 'Ready' carries dl=dev_end, so an")
    print("   un-started item stays invisible until Dev End Date also passes,")
    print("   and is then reported as 'overdue' rather than 'never started'.")
    print("   updatedAt is deliberately ignored here - see has_started().")

    # --- 7. P0 khac
    hdr("7. OTHER P0 SIGNALS")
    noown = [i for i in work if not i["assignees"]]
    print(f"No assignee: {len(noown)}")
    for i in noown[:10]:
        print(f"  {nid(i):<9} {stx(i):<17} {i['title'][:38]}")
    late_ans = [i for i in work if i["answer_by"] and i["answer_by"] < today]
    print(f"\nDev Answer Needed By overdue: {len(late_ans)}")
    for i in late_ans:
        print(f"  +{(today - i['answer_by']).days:>3}d  {nid(i):<9} "
              f"{i['title'][:42]}")
    ext = [i for i in work if phase_of(i) == "blocked-ext"]
    print(f"\nIn External Review (team cannot unblock these): {len(ext)}")
    for i in ext:
        print(f"  {nid(i):<9} {i['title'][:48]}")

    # --- 8. tom tat
    hdr("8. SUMMARY - P0 BUDGET")
    p0 = {i["key"] for grp in
          (noown, late_ans, ext, buckets.get("overdue + NOT moving", []),
           flagged, p0_slip)
          for i in grp}
    print(f"Total P0 after per-item dedupe: {len(p0)}")
    print("Suggested budget: 5-7 items/day")
    if len(p0) > 7:
        print(f"! Over budget ({len(p0)} > 7) -> the rules are too broad, it does")
        print("  not mean the team is on fire. Sections 4, 6 and 6b show the big")
        print("  contributors.")
    blind = len(buckets.get("blind (no deadline)", []))
    if blind:
        print(f"\n! {blind} items have no deadline -> no rule runs on them at all.")
        print("  This is the BLIND set, not the safe set.")
    return items


PR_FRAGMENT = '''
fragment PRB on PullRequest {
  number url state isDraft createdAt updatedAt
  author { login }
  reviewRequests(first:10) {
    nodes { requestedReviewer {
      __typename
      ... on User { login }
      ... on Team { name }
    } }
  }
  reviews(first:20) { nodes { author { login } state submittedAt } }
}
'''


def build_review_query(targets):
    """Aliased query: fetch reviews only for the issues that actually need it.

    PRs are found via TWO paths, since closing keywords may not be used:
      - closedByPullRequestsReferences: only sees PRs saying 'Closes #N'
      - timelineItems CROSS_REFERENCED_EVENT: also sees PRs that merely reference
    """
    parts = []
    for n, (repo, num) in enumerate(targets):
        owner, name = repo.split("/", 1)
        parts.append(
            '\n  i%d: repository(owner:"%s", name:"%s") {'
            '\n    issue(number:%d) {'
            '\n      number'
            '\n      closedByPullRequestsReferences(first:5) { nodes { ...PRB } }'
            '\n      timelineItems(first:15, itemTypes:[CROSS_REFERENCED_EVENT]) {'
            '\n        nodes { ... on CrossReferencedEvent {'
            '\n          source { __typename ... on PullRequest { ...PRB } } } }'
            '\n      }'
            '\n    }'
            '\n  }' % (n, owner, name, num))
    return PR_FRAGMENT + "query {" + "".join(parts) + "\n}"


def fetch_reviews(token, work, batch=15):
    """Returns {(repo, issue_number): [pr, ...]}. Skips items with no repo/number."""
    targets = [(i["repo"], i["number"]) for i in work
               if i["repo"] and i["number"] is not None]
    out = {}
    for k in range(0, len(targets), batch):
        chunk = targets[k:k + batch]
        data = gql_raw(token, build_review_query(chunk))
        for n, key in enumerate(chunk):
            node = (data.get("i%d" % n) or {}).get("issue")
            if not node:
                continue
            prs, seen = [], set()
            for pr in (node.get("closedByPullRequestsReferences")
                       or {}).get("nodes") or []:
                if pr and pr.get("number") not in seen:
                    seen.add(pr["number"])
                    prs.append(pr)
            for ev in (node.get("timelineItems") or {}).get("nodes") or []:
                src = (ev or {}).get("source") or {}
                if (src.get("__typename") == "PullRequest"
                        and src.get("number") not in seen):
                    seen.add(src["number"])
                    prs.append(src)
            out[key] = prs
        print("  ... review %d/%d" % (min(k + batch, len(targets)), len(targets)),
              file=sys.stderr)
    return out


def resolve_actor(prs):
    """Who must act on this PR -> (actors, reason, pr_used_for_timing)."""
    live = [p for p in prs if p.get("state") == "OPEN"]
    if not live:
        merged = [p for p in prs if p.get("state") == "MERGED"]
        if merged:
            return [], "PR already merged - board status is lagging", merged[0]
        return [], ("NO PR FOUND" if not prs else "PRs are closed"), None

    pr = sorted(live, key=lambda p: p.get("createdAt") or "")[-1]
    author = (pr.get("author") or {}).get("login")

    if pr.get("isDraft"):
        return ([author] if author else []), "PR is a draft - not ready for review", pr

    req = []
    for rr in (pr.get("reviewRequests") or {}).get("nodes") or []:
        r = (rr or {}).get("requestedReviewer") or {}
        who = r.get("login") or r.get("name")
        if who and who != author:
            req.append(who)
    if req:
        return req, "review requested", pr

    revs = [r for r in ((pr.get("reviews") or {}).get("nodes") or [])
            if r and r.get("state") not in (None, "PENDING")]
    revs.sort(key=lambda r: r.get("submittedAt") or "")
    if revs:
        last = revs[-1]
        who = (last.get("author") or {}).get("login")
        st = last.get("state")
        if st == "CHANGES_REQUESTED":
            return ([author] if author else []), \
                "changes requested - ball is with the author", pr
        if st == "APPROVED":
            return ([author] if author else []), "approved but not merged", pr
        return ([who] if who else []), "commented, no verdict yet", pr

    return [], "PR open, NOBODY requested, no reviews yet", pr


def pr_age(pr, today):
    d = parse_date(pr.get("createdAt")) if pr else None
    return (today - d).days if d else None


def open_prs(prs):
    """Open PRs, newest first. resolve_actor only ever looks at the first one."""
    return sorted((p for p in prs or [] if p.get("state") == "OPEN"),
                  key=lambda p: p.get("createdAt") or "", reverse=True)


def report_review(work, prmap, today):
    """Section 9: can we send directly? Measures the two gating conditions."""
    hdr("9. WHO MUST ACT  (gating conditions for sending without approval)")

    rows = []
    for i in work:
        prs = prmap.get((i["repo"], i["number"]), [])
        actors, why, pr = resolve_actor(prs)
        rows.append((i, prs, actors, why, pr))

    n = len(rows) or 1
    have_pr = sum(1 for r in rows if r[1])
    resolved = sum(1 for r in rows if r[2])
    print("Items in scope: %d" % len(rows))
    print("  with >=1 linked PR  : %d  (%.0f%%)" % (have_pr, have_pr / n * 100))
    print("  actor resolved      : %d  (%.0f%%)" % (resolved, resolved / n * 100))
    print("\n-> These two are the gating conditions. Below ~90%, sending directly")
    print("   routes the remainder to the wrong people - keep approval for those.")

    print("\nReason breakdown:")
    for why, k in Counter(r[3] for r in rows).most_common():
        print("  %-46s %3d" % (why[:46], k))

    # review load per person
    load = Counter()
    for i, prs, actors, why, pr in rows:
        if why == "review requested":
            for a in actors:
                load[a] += 1
    if load:
        print("\nReview load (PRs waiting on each person):")
        tot = sum(load.values())
        for who, k in load.most_common():
            print("  %-24s %3d  %s" % (who, k, bar(k, tot)))
        top = load.most_common(1)[0]
        if top[1] / tot > 0.5:
            print("\n! %s holds %.0f%% of the review queue." % (top[0], top[1] / tot * 100))
            print("  A digest cannot fix this - the review load needs rebalancing.")

    # PR age = the alternative time axis, generated by git itself
    ages = sorted(a for a in (pr_age(r[4], today) for r in rows) if a is not None)
    if ages:
        print("\nOpen PR age (days) - a time axis git generates itself:")
        print("  min %d   trung vi %d   max %d" % (ages[0], ages[len(ages) // 2], ages[-1]))
        for lo, hi, lb in [(0, 1, "0-1 days"), (2, 3, "2-3 days"),
                           (4, 7, "4-7 days"), (8, 999, "> 7 days")]:
            k = sum(1 for a in ages if lo <= a <= hi)
            if k:
                print("  %-10s %3d  %s" % (lb, k, bar(k, len(ages))))
        print("\n-> Unlike Dev End Date, nobody can set this value to look good.")

    # How many PRs are open per item. resolve_actor evaluates ONLY the newest
    # one, so anything above 1 is a decision made on a partial candidate set.
    dist = Counter(len(open_prs(r[1])) for r in rows)
    print("\nOpen PRs per item  (only the newest open PR is evaluated):")
    for k in sorted(dist):
        lb = "none open" if k == 0 else "%d open" % k
        print("  %-10s %3d  %s" % (lb, dist[k], bar(dist[k], n)))

    multi = [r for r in rows if len(open_prs(r[1])) > 1]
    if not multi:
        print("\n-> No item carries more than one open PR, so every routing")
        print("   decision above saw the full set of candidates.")
    else:
        print("\n! %d items carry more than one OPEN PR." % len(multi))
        print("  The older ones are ignored entirely: they cannot route work, and")
        print("  their age never reaches the distribution above.")
        for i, prs, actors, why, pr in multi:
            print("\n  %-9s %-16s %s" % (nid(i), stx(i), i["title"][:38]))
            for p in open_prs(prs):
                used = pr and p.get("number") == pr.get("number")
                age = pr_age(p, today)
                print("      %s #%-6s %-6s %-5s %s" % (
                    "->" if used else "  ", p.get("number"),
                    "draft" if p.get("isDraft") else "ready",
                    "%dd" % age if age is not None else "?",
                    (p.get("author") or {}).get("login") or "?"))
        print("\n  '->' is the PR the decision was based on (newest by createdAt).")

        # The dangerous shape: a newer draft outranks an older ready PR, so the
        # task goes to the draft's author and the real PR waits unseen.
        hijacked = [r for r in multi
                    if (r[4] or {}).get("isDraft")
                    and any(not p.get("isDraft") for p in open_prs(r[1]))]
        if hijacked:
            print("\n! %d of those are HIJACKED BY A DRAFT: the newest open PR is a"
                  % len(hijacked))
            print("  draft while a ready-for-review PR is also open. The review task")
            print("  goes to the draft's author instead of to a reviewer.")
            for i, prs, actors, why, pr in hijacked:
                print("    %-9s %-38s -> %s" % (
                    nid(i), i["title"][:38], ",".join(actors) or "?"))

    stuck = [r for r in rows if r[3] == "approved but not merged"]
    if stuck:
        print("\nApproved but not merged: %d - the cheapest unblock" % len(stuck))
        for i, prs, actors, why, pr in stuck:
            print("  %-9s %-30s -> %s" % (nid(i), i["title"][:30],
                                          ",".join(actors) or "?"))

    noone = [r for r in rows if not r[2]]
    if noone:
        print("\nActor could not be resolved: %d" % len(noone))
        for i, prs, actors, why, pr in noone[:12]:
            print("  %-9s %-16s %-28s %s" % (nid(i), stx(i), i["title"][:28], why[:24]))

    print("\nDry-run routing table (nothing is sent):")
    byp = defaultdict(list)
    for i, prs, actors, why, pr in rows:
        for a in (actors or ["(UNRESOLVED)"]):
            byp[a].append((i, why, pr))
    for who in sorted(byp, key=lambda x: -len(byp[x])):
        print("\n  %s - %d tasks" % (who, len(byp[who])))
        for i, why, pr in byp[who]:
            age = pr_age(pr, today)
            agestr = "PR %dd" % age if age is not None else "no PR"
            print("    %-9s %-30s [%s] %s" % (nid(i), i["title"][:30],
                                              agestr, why[:30]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", default="XAION-DATA-Inc")
    ap.add_argument("--project", type=int, default=2)
    ap.add_argument("--review", action="store_true",
                    help="phase 2: fetch PR reviewers, measure send-direct readiness")
    ap.add_argument("--scan-sprints", type=int, metavar="N",
                    help="compare Dev End Date behaviour across the last N sprints")
    ap.add_argument("--out", help="write the report to a text file (also printed)")
    ap.add_argument("--json", help="dump normalized data to a file")
    ap.add_argument("--today", help="simulate the run date (YYYY-MM-DD)")
    a = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("GITHUB_TOKEN is not set.\n"
                 "  export GITHUB_TOKEN=ghp_xxx   (scopes: read:project, repo)")

    today = parse_date(a.today) or date.today()
    title, raw = fetch_items(token, a.org, a.project)

    tee = Tee(a.out) if a.out else None
    if tee:
        sys.stdout = tee
    try:
        items = report(title, raw, today)
        if a.scan_sprints:
            scan_sprints(items, a.scan_sprints, today)
        if a.review:
            dev_it = current_iteration(items, "dev_sprint", today)
            scope = [i for i in items
                     if dev_it and i["dev_sprint"]
                     and i["dev_sprint"]["title"] == dev_it["title"]
                     and in_flow(i) and not i["is_draft"]]
            print("\nPhase 2: fetching reviews for %d items..." % len(scope),
                  file=sys.stderr)
            prmap = fetch_reviews(token, scope)
            report_review(scope, prmap, today)
    finally:
        if tee:
            sys.stdout = sys.__stdout__
            tee.close()

    if a.out:
        print(f"\nWrote report -> {a.out}")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2, default=str)
        print(f"Wrote normalized data -> {a.json}")
    if a.out or a.json:
        print("Note: these files contain ticket titles and usernames. Review before sharing.")


if __name__ == "__main__":
    main()
