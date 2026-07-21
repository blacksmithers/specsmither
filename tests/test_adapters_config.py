"""Acceptance for the 0.1.0 config seam: the ``config`` table + ``ConfigStoreSqlite``
+ the pure resolvers (:mod:`specsmither.lifecycle.config`).

Drives a real on-disk SQLite database (``tmp_path`` via :func:`init_db` +
:func:`make_session_factory`) so the upsert / round-trip / migration behaviour is
exercised end to end, plus pure unit cases for :func:`deep_merge`.

Coverage:

* ``ConfigStoreSqlite`` set/get round-trip per domain and per scope; ``set_*`` is an
  idempotent upsert (second write overwrites in place); ``list_*`` projects rows.
* ``resolve_validator_config`` == crucible defaults with no overrides, and reflects a
  stored threshold override when set.
* ``resolve_lifecycle_config`` returns (a deep copy of) the defaults with no overrides,
  and deep-merges a spec snapshot while preserving sibling defaults.
* ``deep_merge`` unit cases: nested merge, array replace, ``None`` skip, non-mapping
  override, no mutation of the base.
* the v2 migration: ``init_db`` reaches ``CURRENT_VERSION``; ``apply_migrations`` is
  re-runnable; the v2 step creates ``config`` idempotently on a pre-config v1 database.
"""

from __future__ import annotations

from pathlib import Path

import crucible
import sqlalchemy as sa
from sqlalchemy import Engine, insert, select
from sqlalchemy.orm import Session, sessionmaker

from specsmither.db.base import make_engine, make_session_factory, now_iso
from specsmither.db.migrations import (
    BASELINE_VERSION,
    CURRENT_VERSION,
    apply_migrations,
    current_version,
    init_db,
    schema_migrations,
)
from specsmither.db.models import Base, PlanningConfig
from specsmither.db.repositories.config_store import ConfigStoreSqlite
from specsmither.lifecycle.config import (
    PLANNING_DOMAIN,
    PLANNING_LIFECYCLE_CONFIG_SCHEMA_VERSION,
    PLANNING_LIFECYCLE_DEFAULTS,
    PLANNING_LIFECYCLE_DOMAIN,
    deep_merge,
    resolve_lifecycle_config,
    resolve_validator_config,
)
from specsmither.lifecycle.ports import ProjectConfigEntry, SpecConfigEntry


def _store_db(tmp_path: Path) -> tuple[Engine, sessionmaker[Session]]:
    engine = init_db(tmp_path / "config.sqlite")
    return engine, make_session_factory(engine)


# --------------------------------------------------------------------------- #
# ConfigStoreSqlite — round-trip, upsert, list                                #
# --------------------------------------------------------------------------- #


def test_project_overrides_round_trip_per_domain(tmp_path: Path) -> None:
    _, sf = _store_db(tmp_path)
    planning_blob = {"thresholds": {"ticket": 55}}
    lifecycle_blob = {"guidance": {"maxNextEntitiesToShow": 9}}

    with sf() as session, session.begin():
        store = ConfigStoreSqlite(session)
        store.set_project_overrides("proj-1", PLANNING_DOMAIN, planning_blob, 1)
        store.set_project_overrides("proj-1", PLANNING_LIFECYCLE_DOMAIN, lifecycle_blob, 2)

    with sf() as session:
        store = ConfigStoreSqlite(session)
        # Each domain round-trips independently for the same owner.
        assert store.get_project_overrides("proj-1", PLANNING_DOMAIN) == planning_blob
        assert (
            store.get_project_overrides("proj-1", PLANNING_LIFECYCLE_DOMAIN)
            == lifecycle_blob
        )
        # Unset owner/domain → None.
        assert store.get_project_overrides("proj-2", PLANNING_DOMAIN) is None
        assert store.get_spec_snapshot("proj-1", PLANNING_DOMAIN) is None


def test_spec_snapshot_round_trip_and_scope_isolation(tmp_path: Path) -> None:
    _, sf = _store_db(tmp_path)
    snapshot = {"thresholds": {"epic": 88}}

    with sf() as session, session.begin():
        store = ConfigStoreSqlite(session)
        # Same id used as both a project and a spec owner — scopes must not collide.
        store.set_project_overrides("shared-id", PLANNING_DOMAIN, {"x": 1}, 1)
        store.set_spec_snapshot("shared-id", PLANNING_DOMAIN, snapshot, 1)

    with sf() as session:
        store = ConfigStoreSqlite(session)
        assert store.get_spec_snapshot("shared-id", PLANNING_DOMAIN) == snapshot
        assert store.get_project_overrides("shared-id", PLANNING_DOMAIN) == {"x": 1}


def test_set_is_idempotent_upsert(tmp_path: Path) -> None:
    engine, sf = _store_db(tmp_path)

    with sf() as session, session.begin():
        store = ConfigStoreSqlite(session)
        store.set_project_overrides("proj-1", PLANNING_DOMAIN, {"thresholds": {"ticket": 50}}, 1)
    with sf() as session, session.begin():
        store = ConfigStoreSqlite(session)
        store.set_project_overrides("proj-1", PLANNING_DOMAIN, {"thresholds": {"ticket": 65}}, 3)

    # Second write overwrote the first in place — exactly one row, latest value/version.
    with sf() as session:
        store = ConfigStoreSqlite(session)
        assert store.get_project_overrides("proj-1", PLANNING_DOMAIN) == {
            "thresholds": {"ticket": 65}
        }
    with engine.connect() as conn:
        count = conn.execute(
            select(sa.func.count()).select_from(PlanningConfig.__table__)
        ).scalar_one()
    assert count == 1


def test_list_project_and_spec_configs(tmp_path: Path) -> None:
    _, sf = _store_db(tmp_path)

    with sf() as session, session.begin():
        store = ConfigStoreSqlite(session)
        store.set_project_overrides("proj-1", PLANNING_DOMAIN, {"a": 1}, 1)
        store.set_project_overrides("proj-1", PLANNING_LIFECYCLE_DOMAIN, {"b": 2}, 2)
        store.set_spec_snapshot("spec-1", PLANNING_DOMAIN, {"c": 3}, 1)

    with sf() as session:
        store = ConfigStoreSqlite(session)
        proj = {e.domain: e for e in store.list_project_configs("proj-1")}
        specs = store.list_spec_configs("spec-1")

    assert set(proj) == {PLANNING_DOMAIN, PLANNING_LIFECYCLE_DOMAIN}
    assert isinstance(proj[PLANNING_DOMAIN], ProjectConfigEntry)
    assert proj[PLANNING_DOMAIN].overrides == {"a": 1}
    assert proj[PLANNING_LIFECYCLE_DOMAIN].schema_version == 2

    assert len(specs) == 1
    assert isinstance(specs[0], SpecConfigEntry)
    assert specs[0].snapshot == {"c": 3}
    assert specs[0].domain == PLANNING_DOMAIN


# --------------------------------------------------------------------------- #
# resolve_validator_config (domain 'planning', crucible.merge_config)         #
# --------------------------------------------------------------------------- #


def test_resolve_validator_config_defaults_with_no_overrides(tmp_path: Path) -> None:
    _, sf = _store_db(tmp_path)
    with sf() as session:
        cfg = resolve_validator_config(ConfigStoreSqlite(session), "proj-empty")
    assert cfg == crucible.load_defaults()


def test_resolve_validator_config_reflects_project_override(tmp_path: Path) -> None:
    _, sf = _store_db(tmp_path)
    with sf() as session, session.begin():
        ConfigStoreSqlite(session).set_project_overrides(
            "proj-1",
            PLANNING_DOMAIN,
            {"thresholds": {"ticket": 55}},
            crucible.PLANNING_CONFIG_SCHEMA_VERSION,
        )

    with sf() as session:
        cfg = resolve_validator_config(ConfigStoreSqlite(session), "proj-1")

    defaults = crucible.load_defaults()
    assert cfg["thresholds"]["ticket"] == 55  # overridden
    assert cfg["thresholds"]["epic"] == defaults["thresholds"]["epic"]  # sibling kept


def test_resolve_validator_config_layers_spec_snapshot(tmp_path: Path) -> None:
    _, sf = _store_db(tmp_path)
    with sf() as session, session.begin():
        store = ConfigStoreSqlite(session)
        store.set_project_overrides("proj-1", PLANNING_DOMAIN, {"thresholds": {"ticket": 55}}, 1)
        store.set_spec_snapshot("spec-1", PLANNING_DOMAIN, {"thresholds": {"epic": 60}}, 1)

    with sf() as session:
        cfg = resolve_validator_config(ConfigStoreSqlite(session), "proj-1", spec_id="spec-1")

    # Project override + spec snapshot both apply (snapshot layered on top).
    assert cfg["thresholds"]["ticket"] == 55
    assert cfg["thresholds"]["epic"] == 60


# --------------------------------------------------------------------------- #
# resolve_lifecycle_config (domain 'planning-lifecycle', deep_merge)          #
# --------------------------------------------------------------------------- #


def test_resolve_lifecycle_config_defaults_no_overrides(tmp_path: Path) -> None:
    _, sf = _store_db(tmp_path)
    with sf() as session:
        cfg = resolve_lifecycle_config(ConfigStoreSqlite(session), "proj-empty")
    assert cfg == PLANNING_LIFECYCLE_DEFAULTS
    # Defaults are deep-copied — the module-level constant is never aliased out.
    assert cfg["guidance"] is not PLANNING_LIFECYCLE_DEFAULTS["guidance"]


def test_resolve_lifecycle_config_snapshot_override(tmp_path: Path) -> None:
    _, sf = _store_db(tmp_path)
    with sf() as session, session.begin():
        ConfigStoreSqlite(session).set_spec_snapshot(
            "spec-1",
            PLANNING_LIFECYCLE_DOMAIN,
            {"guidance": {"maxNextEntitiesToShow": 7}},
            PLANNING_LIFECYCLE_CONFIG_SCHEMA_VERSION,
        )

    with sf() as session:
        cfg = resolve_lifecycle_config(ConfigStoreSqlite(session), "proj-1", spec_id="spec-1")

    # Overridden key changes; the sibling default survives the nested merge.
    assert cfg["guidance"]["maxNextEntitiesToShow"] == 7
    assert cfg["guidance"]["fieldRenderDetail"] == "rich"


def test_default_language_seeds_the_baseline_guidance_language(tmp_path: Path) -> None:
    # A standalone embed's ambient default (normalized) with no stored config.
    _, sf = _store_db(tmp_path)
    with sf() as session:
        cfg = resolve_lifecycle_config(
            ConfigStoreSqlite(session), "proj-empty", default_language="pt_BR"
        )
    assert cfg["guidance"]["language"] == "pt-br"

    # An unsupported tag degrades to English rather than poisoning the config.
    with sf() as session:
        cfg = resolve_lifecycle_config(
            ConfigStoreSqlite(session), "proj-empty", default_language="klingon"
        )
    assert cfg["guidance"]["language"] == "en"


def test_explicit_config_language_wins_over_default_language(tmp_path: Path) -> None:
    _, sf = _store_db(tmp_path)
    with sf() as session, session.begin():
        ConfigStoreSqlite(session).set_project_overrides(
            "proj-1", PLANNING_LIFECYCLE_DOMAIN, {"guidance": {"language": "en"}}, 3
        )

    # default_language=pt-br is only the baseline; an explicit project override wins.
    with sf() as session:
        cfg = resolve_lifecycle_config(
            ConfigStoreSqlite(session), "proj-1", default_language="pt-br"
        )
    assert cfg["guidance"]["language"] == "en"


def test_resolve_lifecycle_config_project_then_spec(tmp_path: Path) -> None:
    _, sf = _store_db(tmp_path)
    with sf() as session, session.begin():
        store = ConfigStoreSqlite(session)
        store.set_project_overrides(
            "proj-1", PLANNING_LIFECYCLE_DOMAIN, {"guidance": {"fieldRenderDetail": "concise"}}, 2
        )
        store.set_spec_snapshot(
            "spec-1", PLANNING_LIFECYCLE_DOMAIN, {"guidance": {"maxNextEntitiesToShow": 4}}, 2
        )

    with sf() as session:
        cfg = resolve_lifecycle_config(ConfigStoreSqlite(session), "proj-1", spec_id="spec-1")

    # Project override (concise) + spec snapshot (count=4) both land via deep merge.
    assert cfg["guidance"]["fieldRenderDetail"] == "concise"
    assert cfg["guidance"]["maxNextEntitiesToShow"] == 4


# --------------------------------------------------------------------------- #
# deep_merge — pure unit cases                                                #
# --------------------------------------------------------------------------- #


def test_deep_merge_nested() -> None:
    base = {"a": {"x": 1, "y": 2}, "b": 3}
    assert deep_merge(base, {"a": {"y": 20}}) == {"a": {"x": 1, "y": 20}, "b": 3}


def test_deep_merge_array_replaces_wholesale() -> None:
    assert deep_merge({"list": [1, 2, 3], "k": 1}, {"list": [9]}) == {"list": [9], "k": 1}


def test_deep_merge_skips_none_values() -> None:
    # A None override never unsets a default.
    assert deep_merge({"a": 1, "b": 2}, {"a": None, "b": 5}) == {"a": 1, "b": 5}


def test_deep_merge_non_mapping_override_returns_base_copy() -> None:
    base = {"a": 1}
    for override in (None, [1, 2], 42, "str"):
        out = deep_merge(base, override)
        assert out == {"a": 1}
        assert out is not base  # always a fresh copy


def test_deep_merge_does_not_mutate_base() -> None:
    base = {"a": {"x": 1}}
    deep_merge(base, {"a": {"x": 2}, "b": 9})
    assert base == {"a": {"x": 1}}


def test_deep_merge_dict_replaces_non_dict_existing() -> None:
    # Existing primitive, override mapping → replace (no recursion into a non-dict).
    assert deep_merge({"a": 1}, {"a": {"x": 2}}) == {"a": {"x": 2}}


# --------------------------------------------------------------------------- #
# v2 migration — reach CURRENT_VERSION, idempotent, upgrade a v1 database      #
# --------------------------------------------------------------------------- #


def test_init_db_reaches_current_version_and_is_re_runnable(tmp_path: Path) -> None:
    engine = init_db(tmp_path / "m.sqlite")
    assert current_version(engine) == CURRENT_VERSION
    assert sa.inspect(engine).has_table("config")

    apply_migrations(engine)  # re-run is a no-op
    assert current_version(engine) == CURRENT_VERSION

    with engine.connect() as conn:
        versions = list(
            conn.execute(
                select(schema_migrations.c.version).order_by(schema_migrations.c.version)
            ).scalars()
        )
    assert versions == [BASELINE_VERSION, CURRENT_VERSION]


def test_v2_step_upgrades_pre_config_v1_database(tmp_path: Path) -> None:
    engine = make_engine(tmp_path / "old.sqlite")
    # Simulate a database stamped at v1 BEFORE the config model existed: build the
    # baseline schema, drop config, and stamp only v1.
    Base.metadata.create_all(engine)
    PlanningConfig.__table__.drop(engine)
    schema_migrations.create(engine, checkfirst=True)
    with Session(engine) as session, session.begin():
        session.execute(
            insert(schema_migrations).values(version=BASELINE_VERSION, applied_at=now_iso())
        )

    assert current_version(engine) == BASELINE_VERSION
    assert not sa.inspect(engine).has_table("config")

    apply_migrations(engine)

    assert current_version(engine) == CURRENT_VERSION
    assert sa.inspect(engine).has_table("config")
