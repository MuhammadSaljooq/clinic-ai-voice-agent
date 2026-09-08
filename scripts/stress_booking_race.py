"""Stress the booking race in-process, many iterations, reporting the exact
distribution of outcomes and every unexpected exception type.

Bisection logic: if this shows 0 anomalies over hundreds of races, the flake is
NOT in the booking code -- it is in the pytest fixture.
"""
import asyncio, collections, pathlib, sys
from datetime import UTC, datetime, timedelta

import asyncpg

sys.path.insert(0, "src")
from clinic_agent.scheduling.booking import Booked, SlotTaken, book
from clinic_agent.scheduling.models import Slot
from clinic_agent.scheduling.tokens import issue_slot_token

ADMIN = "postgresql://clinic:clinic@localhost:55432/postgres"
DSN = "postgresql://clinic:clinic@localhost:55432/clinic_stress"
MIGRATION = pathlib.Path("src/clinic_agent/db/migrations/001_init.sql").read_text()
SECRET = "stress"
NOW = datetime(2026, 9, 14, 12, tzinfo=UTC)
BASE = datetime(2026, 10, 1, 0, tzinfo=UTC)


async def setup():
    admin = await asyncpg.connect(ADMIN)
    if not await admin.fetchval("SELECT 1 FROM pg_database WHERE datname='clinic_stress'"):
        await admin.execute("CREATE DATABASE clinic_stress")
    await admin.close()
    pool = await asyncpg.create_pool(DSN, min_size=10, max_size=25)
    async with pool.acquire() as c:
        await c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await c.execute(MIGRATION)
        await c.execute("INSERT INTO providers (id,name) VALUES (1,'A'),(2,'B')")
        await c.execute(
            "INSERT INTO appointment_types (id,name,duration_min,buffer_after_min)"
            " VALUES (1,'F',30,15),(2,'N',60,10)"
        )
        await c.execute("INSERT INTO appointment_type_providers VALUES (1,1),(2,1),(1,2)")
    return pool


def tok(start):
    return issue_slot_token(
        Slot(provider_id=1, appointment_type_id=1, start=start, end=start + timedelta(minutes=30)),
        SECRET, now=NOW,
    )


async def race(pool, start, n, shared_phone):
    return await asyncio.gather(*[
        book(pool, slot_token=tok(start), secret=SECRET,
             patient_name=f"C{k}",
             patient_phone="+15550000" if shared_phone else f"+1555{k:07d}",
             now=NOW)
        for k in range(n)
    ], return_exceptions=True)


async def run_config(pool, label, n, shared_phone, rounds, offset):
    outcomes = collections.Counter()
    anomalies = collections.Counter()
    for i in range(rounds):
        # A distinct slot per round, so rounds never interfere.
        start = BASE + timedelta(hours=offset + i)
        res = await race(pool, start, n, shared_phone)
        w = sum(isinstance(r, Booked) for r in res)
        lost = sum(isinstance(r, SlotTaken) for r in res)
        odd = [r for r in res if not isinstance(r, (Booked, SlotTaken))]
        outcomes[(w, lost, len(odd))] += 1
        for e in odd:
            anomalies[f"{type(e).__module__}.{type(e).__name__}: {str(e)[:110]}"] += 1
    ok = outcomes.get((1, n - 1, 0), 0)
    print(f"\n[{label}]  n={n} shared_phone={shared_phone} rounds={rounds}")
    print(f"  correct (1 winner, {n-1} SlotTaken, 0 other): {ok}/{rounds}")
    for k, v in sorted(outcomes.items()):
        if k != (1, n - 1, 0):
            print(f"  ANOMALY winners={k[0]} slottaken={k[1]} other={k[2]}  x{v}")
    for k, v in anomalies.most_common():
        print(f"  EXCEPTION {k}  x{v}")
    return ok == rounds


async def main():
    pool = await setup()
    a = await run_config(pool, "A: 2 callers, distinct phones", 2, False, 200, 0)
    b = await run_config(pool, "B: 5 callers, distinct phones", 5, False, 150, 500)
    c = await run_config(pool, "C: 2 callers, SAME phone (patient row contention)", 2, True, 200, 900)
    await pool.close()
    print("\nRESULT:", "all configs clean" if (a and b and c) else "ANOMALIES FOUND")


asyncio.run(main())
