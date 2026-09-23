#!/usr/bin/env python3
"""Unit tests for the never-started rule (ab.has_started / ab.start_slip).

No network and no token: GITHUB_TOKEN is only read inside main(), so importing
autoboost_diagnose is free. Plain asserts, no pytest.

    python3 test_start_slip.py
"""

from datetime import date

import autoboost_diagnose as ab

# Pinned so weekday arithmetic is unambiguous.
WED = date(2026, 9, 9)
MON = date(2026, 9, 7)
assert WED.weekday() == 2 and MON.weekday() == 0, "test dates drifted"

FRI = date(2026, 9, 4)     # working day before MON
SAT = date(2026, 9, 5)
SUN = date(2026, 9, 6)
TUE = date(2026, 9, 8)
THU = date(2026, 9, 10)


def mk(status="Ready", dev_start=None, prs=None, state="OPEN", updated=None):
    return {"status": status, "dev_start": dev_start, "prs": prs or [],
            "state": state, "updated": updated, "dev_end": None,
            "assignees": ["someone"], "key": 1, "number": 1}


DRAFT_PR = [{"number": 9, "state": "OPEN", "isDraft": True,
             "updatedAt": "2026-08-01T00:00:00Z"}]
OPEN_PR = [{"number": 9, "state": "OPEN", "isDraft": False,
            "updatedAt": "2026-09-08T00:00:00Z"}]

CASES = [
    # (label, item, today, expected slip)
    ("start in the future",        mk(dev_start=THU), WED, None),
    ("start is today",             mk(dev_start=WED), WED, None),
    ("start was yesterday",        mk(dev_start=TUE), WED, 1),
    ("last Friday, run Monday",    mk(dev_start=FRI), MON, 1),
    ("Saturday, run Monday",       mk(dev_start=SAT), MON, None),
    ("Sunday, run Monday",         mk(dev_start=SUN), MON, None),
    ("3 working days back",        mk(dev_start=FRI), WED, 3),
    ("3 back + updatedAt fresh",   mk(dev_start=FRI, updated=TUE), WED, 3),
    ("3 back + stale draft PR",    mk(dev_start=FRI, prs=DRAFT_PR), WED, None),
    ("3 back + open PR",           mk(dev_start=FRI, prs=OPEN_PR), WED, None),
    ("3 back + issue CLOSED",      mk(dev_start=FRI, state="CLOSED"), WED, None),
    ("In progress",                mk("In progress", FRI), WED, None),
    ("In review",                  mk("In review", FRI), WED, None),
    ("emoji status",               mk("\U0001f440 Ready", FRI), WED, 3),
    ("no Dev Start Date",          mk(dev_start=None), WED, None),
    # Tier boundary: START_SLIP_P0 = 2, so 2 is P1 and 3 is P0.
    ("boundary slip 2",            mk(dev_start=MON), WED, 2),
]

fails = 0
for label, item, today, want in CASES:
    got = ab.start_slip(item, today)
    ok = got == want
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {label:<26} want={want!s:<5} got={got}")

# Tier split
assert ab.START_SLIP_P0 == 2
assert ab.start_slip(mk(dev_start=MON), WED) <= ab.START_SLIP_P0, "slip 2 is P1"
assert ab.start_slip(mk(dev_start=FRI), WED) > ab.START_SLIP_P0, "slip 3 is P0"

# has_started: updatedAt must never count, however fresh
assert ab.has_started(mk(updated=WED))[0] is False, "updatedAt must not count"
assert ab.has_started(mk(prs=DRAFT_PR))[0] is True, "a draft PR means started"
assert ab.has_started(mk(state="CLOSED"))[0] is True

print("\ntier split + has_started: PASS")

# not_due_to_start: the mirror rule (README 4.11). Only 'Ready', only with a
# FUTURE Dev Start Date, and only when nothing shows work already began.
NDS = [
    ("future start",               mk(dev_start=THU), WED, True),
    ("start is today",             mk(dev_start=WED), WED, False),
    ("start passed",               mk(dev_start=TUE), WED, False),
    ("no Dev Start Date",          mk(dev_start=None), WED, False),
    ("future start + draft PR",    mk(dev_start=THU, prs=DRAFT_PR), WED, False),
    ("future start + open PR",     mk(dev_start=THU, prs=OPEN_PR), WED, False),
    ("future start + CLOSED",      mk(dev_start=THU, state="CLOSED"), WED, False),
    ("In progress, future start",  mk("In progress", THU), WED, False),
    ("emoji Ready, future start",  mk("\U0001f4cb Ready", THU), WED, True),
]
for label, item, today, want in NDS:
    got = ab.not_due_to_start(item, today)
    ok = got == want
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {label:<26} want={want!s:<5} got={got}")

# build(): an upcoming item is routed to nobody and never reaches d["tasks"],
# so --commit cannot write it to state and carryover cannot start.
import daily_todo as dt
SPRINT = {"title": "S", "start": MON, "duration": 14, "end": MON + (THU - MON) * 3}
def full(n, status, dev_start):
    return dict(mk(status, dev_start), key=n, number=n, title=f"t{n}",
                url=None, repo="o/r", is_draft=False, type="Issue",
                dev_sprint=SPRINT, assignees=["dev"])
items = [full(1, "Ready", THU), full(2, "Ready", TUE), full(3, "In progress", THU)]
d = dt.build(items, {}, [], WED, [])
assert [t["key"] for t in d["upcoming"]] == [1], d["upcoming"]
assert sorted(t["key"] for t in d["tasks"]) == [2, 3], d["tasks"]
assert d["tasks"][0]["person"] == "dev"
print("build(): upcoming split PASS")
print(f"\n{len(CASES) - fails}/{len(CASES)} cases passed")
raise SystemExit(1 if fails else 0)
