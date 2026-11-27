"""WSGI entry-point for Vercel (and any other WSGI host).

Vercel's @vercel/python builder looks for a module-level variable named `app`
(or the name configured in vercel.json) that is a WSGI callable.

Importing app.py triggers _ensure_db() which initialises the SQLite database
in /tmp on a cold start, so no extra setup is required here.
"""
from app import app  # noqa: F401  – 're-exported' as the WSGI callable

# Vercel expects the callable to be named `app` at module level, which it is.
