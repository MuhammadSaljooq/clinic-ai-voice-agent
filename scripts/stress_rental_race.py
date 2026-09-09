"""Stress the trailer-rental booking race: many concurrent callers try to grab the same
free unit for overlapping dates. Correct outcome every round is exactly one winner and
the rest UnitTaken, with no deadlocks or other exceptions.
"""
import asyncio
import collections
import pathlib
import sys
from datetime import UTC, date, datetime

import asyncpg

sys.path.insert(0, "src")
from clinic_agent.rentals.booking import Booked, UnitTaken, book
from clinic_agent.rentals.models import RentalSlot
from clinic_agent.rentals.tokens import issue_rental_token

ADMIN = "postgresql://clinic:clinic@localhost:55432/postgres"
DSN = "postgresql://clinic:clinic@localhost:55432/rental_stress"
MIGRATION = pathlib.Path("src/clinic_agent/db/migrations/005_trailer_rentals.sql").read_text()
SECRET = "stress"
NOW = datetime(2026, 9, 14, 12, tzinfo=UTC)


async def setup():
    admin = await asyncpg.connect(ADMIN)
    if not await admin.fetchval("SELECT 1 FROM pg_database WHERE datname='rental_stress'"):
        await admin.execute("CREATE DATABASE rental_stress")
    await admin.close()
    pool = await asyncpg.create_pool(DSN, min_size=10, max_size=25)
    async with pool.acquire() as c:
        await c.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
        await c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await c.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
        await c.execute(MIGRATION)
        await c.execute(
            "INSERT INTO trailer_types (id,name,daily_rate,deposit) VALUES (1,'U',45,150)"
        )
        await c.execute("INSERT INTO trailer_units (trailer_type_id,label) VALUES (1,'A')")
    return pool


def tok(unit_id):
    slot = RentalSlot(trailer_unit_id=unit_id, trailer_type_id=1,
                      pickup=date(2026, 10, 5), return_=date(2026, 10, 7))
    return issue_rental_token(slot, total_cents=13500, deposit_cents=15000, secret=SECRET, now=NOW)


async def race(pool, unit_id, n):
    return await asyncio.gather(*[
        book(pool, rental_token=tok(unit_id), secret=SECRET,
             customer_name=f"C{k}", customer_phone=f"+1555{k:07d}", now=NOW)
        for k in range(n)
    ], return_exceptions=True)


async def main():
    pool = await setup()
    unit_id = await pool.fetchval("SELECT id FROM trailer_units LIMIT 1")
    outcomes = collections.Counter()
    anomalies = collections.Counter()
    rounds, n = 200, 5
    for _ in range(rounds):
        # Clear bookings so each round races for the same free unit + dates.
        await pool.execute("DELETE FROM rentals")
        res = await race(pool, unit_id, n)
        w = sum(isinstance(r, Booked) for r in res)
        lost = sum(isinstance(r, UnitTaken) for r in res)
        odd = [r for r in res if not isinstance(r, (Booked, UnitTaken))]
        outcomes[(w, lost, len(odd))] += 1
        for e in odd:
            anomalies[f"{type(e).__name__}: {str(e)[:110]}"] += 1
    ok = outcomes.get((1, n - 1, 0), 0)
    print(f"n={n} rounds={rounds}: correct (1 winner, {n-1} UnitTaken, 0 other) = {ok}/{rounds}")
    for k, v in sorted(outcomes.items()):
        if k != (1, n - 1, 0):
            print(f"  ANOMALY winners={k[0]} taken={k[1]} other={k[2]}  x{v}")
    for k, v in anomalies.most_common():
        print(f"  EXCEPTION {k}  x{v}")
    await pool.close()
    print("RESULT:", "clean" if ok == rounds else "ANOMALIES FOUND")


asyncio.run(main())
