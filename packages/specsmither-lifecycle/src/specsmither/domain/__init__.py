"""Domain layer: enums, status transitions, and runtime DB-row records.

Pure (no SQLAlchemy, no I/O). Defines the type vocabulary the whole engine
shares. ``records.py`` rebuilds crucible's ``Specification`` shape from DB rows
(``SpecFull``) so the planning gate can score a persisted spec.
"""
