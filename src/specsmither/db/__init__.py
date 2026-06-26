"""Persistence layer: SQLAlchemy 2.0 ORM over one SQLite file.

``base`` (engine/session/PRAGMAs/BEGIN IMMEDIATE), ``models`` (the schema),
``migrations`` (versioned baseline DDL), ``repositories`` (the ``*StoreSqlite``
implementations of the store interfaces).
"""
