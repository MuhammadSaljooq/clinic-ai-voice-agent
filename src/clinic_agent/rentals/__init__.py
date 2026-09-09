"""Trailer-rental domain: pricing, availability, signed tokens, and transactional booking.

Mirrors `scheduling/` for a separate domain. Rentals span whole days (inclusive date
ranges), and a physical trailer unit cannot be double-booked -- enforced by a Postgres
exclusion constraint, the same guarantee the clinic's appointment core relies on.
"""
