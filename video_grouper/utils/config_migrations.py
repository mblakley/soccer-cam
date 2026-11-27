"""Versioned, one-shot migrations for ``config.ini``.

A schema change used to mean one of two bad options: leave a permanent
back-compat shim in ``load_config`` that reads the old shape on every load
forever (what ``[BALL_TRACKING]`` -> ``[PIPELINE]`` did), or break existing
installs silently — pydantic ignores unknown sections, so an upgraded config
simply loses whatever moved, with no error anywhere.

This is the third option. Each schema change is a numbered migration; the file
records the version it is at; on startup every migration above that version is
applied in order and the result written back once. Afterwards the file *is* the
new shape and no legacy-reading code needs to survive.

Not in the installer: NSIS is Windows-only and never touches ``config.ini``
(auto-upgrade returns early and only writes to ``$INSTDIR``), while soccer-cam
also ships Docker/Linux and supports hand-copied configs. Startup is the only
place that covers all of them.

Comments are not preserved — ``ConfigParser`` cannot round-trip them. The
original is kept as ``config.ini.bak``. (The ``/config`` editor already
destroys comments on every save, so this is not a new class of loss.)
"""

from __future__ import annotations

import configparser
import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Section and option holding the schema version. Machine-owned; operators
# should not edit it.
VERSION_SECTION = "SCHEMA"
VERSION_OPTION = "version"

# A config written before versioning existed. Absence means 0, never "current"
# — assuming current would skip every migration on exactly the files that need
# them.
UNVERSIONED = 0


@dataclass(frozen=True)
class Migration:
    """One schema change.

    ``apply`` takes the raw ``{section: {option: value}}`` mapping straight
    from ConfigParser and returns the same shape. Pure dict-to-dict, like
    :func:`video_grouper.pipeline.config.migrate_ball_tracking_to_pipeline`, so
    it can be unit-tested without touching a file.
    """

    version: int  # the version this migration produces
    description: str
    apply: Callable[[dict[str, dict[str, str]]], dict[str, dict[str, str]]]


def _v1_ball_tracking_to_pipeline(
    raw: dict[str, dict[str, str]],
) -> dict[str, dict[str, str]]:
    """``[BALL_TRACKING]`` -> ``[PIPELINE]``.

    This ran on every single load as a permanent shim in ``load_config``. It is
    the same transform, moved here so it runs once and the shim can go.

    Triggered by the *absence of a ``[PIPELINE]`` section*, not by it being
    empty: ``save_config`` always emits ``[PIPELINE]``, so an explicit
    ``enabled = false`` is a deliberate "pipeline off" and must not be
    re-migrated over.
    """
    from video_grouper.pipeline.config import migrate_ball_tracking_to_pipeline

    legacy: dict[str, object] = {}
    for section in list(raw):
        if section == "BALL_TRACKING":
            legacy.update(raw.pop(section))
        elif section.startswith("BALL_TRACKING."):
            legacy[section.split(".", 1)[1]] = raw.pop(section)

    has_pipeline = any(s == "PIPELINE" or s.startswith("PIPELINE.") for s in raw)
    if has_pipeline or not legacy:
        return raw  # nothing to do; BALL_TRACKING (if any) is now dropped

    migrated = migrate_ball_tracking_to_pipeline(legacy)
    if not migrated:
        return raw

    # Flatten back to INI sections: [PIPELINE] carries enabled + the step
    # order, and each step gets its own [PIPELINE.<step_id>].
    pipeline: dict[str, str] = {}
    if "enabled" in migrated:
        pipeline["enabled"] = str(migrated["enabled"])
    steps = migrated.get("steps") or []
    if steps:
        pipeline["steps"] = ", ".join(steps)
    raw["PIPELINE"] = pipeline
    for step_id, spec in (migrated.get("step_specs") or {}).items():
        step_section: dict[str, str] = {"type": str(spec.get("type", ""))}
        step_section.update({k: str(v) for k, v in (spec.get("config") or {}).items()})
        raw[f"PIPELINE.{step_id}"] = step_section
    return raw


def _v2_per_team_sections_to_team(
    raw: dict[str, dict[str, str]],
) -> dict[str, dict[str, str]]:
    """Scattered per-team settings -> one ``[TEAM.<key>]`` each.

    Sources, in the spellings real configs use:

    * ``[TEAMSNAP.<anything>]``  — ``team_name`` in the body, or the section
      suffix as a fallback (installs in the wild use ``[TEAMSNAP.TEAM.0]``)
    * ``[PLAYMETRICS.TEAM.<n>]`` — ``team_name`` in the body only
    * ``[YOUTUBE.PLAYLIST_MAP]`` — team -> playlist, keys LOWERCASED by
      configparser and matched by substring, so ``guzzetta`` legitimately maps
      ``BU14 - Guzzetta``. Carried across as an alias so that keeps working.
    * ``[PIPELINE.PER_TEAM]``    — team -> preset

    Teams are keyed by a slug of their name. A playlist-map entry that matches
    an existing team by substring is folded into it rather than creating a
    duplicate; one that matches nothing becomes its own team, since it names a
    team the user clearly has.
    """
    teams: dict[str, dict[str, str]] = {}

    def slug(name: str) -> str:
        cleaned = "".join(c if c.isalnum() else "_" for c in name.strip().casefold())
        return "_".join(p for p in cleaned.split("_") if p) or "team"

    def upsert(name: str, **fields: str) -> dict[str, str]:
        key = slug(name)
        team = teams.setdefault(key, {"name": name.strip()})
        for field, value in fields.items():
            if value:
                team[field] = value
        return team

    for section in list(raw):
        if section.startswith("TEAMSNAP.") and (body := raw.pop(section)):
            name = body.get("team_name") or section.split(".", 1)[1]
            upsert(name, teamsnap_team_id=body.get("team_id", ""))
        elif section.startswith("PLAYMETRICS.TEAM.") and (body := raw.pop(section)):
            name = body.get("team_name", "")
            if name:
                upsert(name, playmetrics_team_id=body.get("team_id", ""))

    for key, playlist in (raw.pop("YOUTUBE.PLAYLIST_MAP", None) or {}).items():
        probe = key.strip().casefold()
        for team in teams.values():
            if probe == team["name"].casefold() or probe in team["name"].casefold():
                team["youtube_playlist"] = playlist
                # Keep the short spelling working — it is what the operator
                # wrote, and substring matching is why it resolved at all.
                if probe != team["name"].casefold():
                    existing = [
                        a for a in team.get("aliases", "").split(",") if a.strip()
                    ]
                    team["aliases"] = ", ".join([*existing, key.strip()])
                break
        else:
            upsert(key, youtube_playlist=playlist)

    for key, preset in (raw.pop("PIPELINE.PER_TEAM", None) or {}).items():
        probe = key.strip().casefold()
        for team in teams.values():
            if probe == team["name"].casefold() or probe in team["name"].casefold():
                team["pipeline"] = preset
                break
        else:
            upsert(key, pipeline=preset)

    # Legacy single-team scalars, folded into `teams` by the models today.
    # Drop them so a migrated file does not carry two competing definitions.
    for legacy_section, legacy_keys in (
        ("TEAMSNAP", ("team_id", "team_name", "my_team_name")),
        ("PLAYMETRICS", ("team_id", "team_name")),
    ):
        legacy_body = raw.get(legacy_section)
        if not legacy_body:
            continue
        legacy_name = legacy_body.get("team_name") or legacy_body.get("my_team_name")
        already_known = any(
            t["name"].casefold() == (legacy_name or "").casefold()
            for t in teams.values()
        )
        if legacy_name and not already_known:
            field = (
                "teamsnap_team_id"
                if legacy_section == "TEAMSNAP"
                else "playmetrics_team_id"
            )
            upsert(legacy_name, **{field: legacy_body.get("team_id", "")})
        for legacy_key in legacy_keys:
            legacy_body.pop(legacy_key, None)

    for key, team in teams.items():
        raw[f"TEAM.{key}"] = team
    return raw


# Ordered, contiguous from 1. Append only — never renumber or edit a shipped
# migration, or configs part-way through the sequence will skip it.
MIGRATIONS: list[Migration] = [
    Migration(
        version=1,
        description="[BALL_TRACKING] -> [PIPELINE] (was a permanent load-time shim)",
        apply=_v1_ball_tracking_to_pipeline,
    ),
    Migration(
        version=2,
        description="per-team settings -> one [TEAM.<key>] section each",
        apply=_v2_per_team_sections_to_team,
    ),
]


def current_schema_version() -> int:
    """The version a freshly written config is at."""
    return MIGRATIONS[-1].version if MIGRATIONS else UNVERSIONED


def read_version(raw: dict[str, dict[str, str]]) -> int:
    """Schema version recorded in *raw*, or ``UNVERSIONED``."""
    section = raw.get(VERSION_SECTION) or {}
    value = section.get(VERSION_OPTION)
    if value is None:
        return UNVERSIONED
    try:
        return int(str(value).strip())
    except ValueError:
        logger.warning(
            "CONFIG_MIGRATION: unreadable %s.%s (%r); treating as unversioned.",
            VERSION_SECTION,
            VERSION_OPTION,
            value,
        )
        return UNVERSIONED


def pending(raw: dict[str, dict[str, str]]) -> list[Migration]:
    """Migrations that still need to run against *raw*, in order.

    Raises when the file is NEWER than this build understands. That is a
    downgrade: an older binary cannot know what a later migration did, and
    would happily write the file back out in a shape the newer build wrote,
    minus whatever it does not model. Refusing is the only safe answer.
    """
    version = read_version(raw)
    latest = current_schema_version()
    if version > latest:
        raise ValueError(
            f"config.ini is at schema version {version}, but this build only "
            f"understands {latest}. It was written by a newer soccer-cam; "
            "upgrade rather than run this version against it."
        )
    return [m for m in MIGRATIONS if m.version > version]


def migrate(
    raw: dict[str, dict[str, str]],
) -> tuple[dict[str, dict[str, str]], list[Migration]]:
    """Apply every pending migration to *raw*, returning it and what ran."""
    applied: list[Migration] = []
    for migration in pending(raw):
        raw = migration.apply(raw)
        applied.append(migration)
    if applied:
        raw.setdefault(VERSION_SECTION, {})[VERSION_OPTION] = str(
            current_schema_version()
        )
    return raw, applied


def _read_raw(config_path: Path) -> dict[str, dict[str, str]]:
    parser = configparser.ConfigParser()
    parser.read(config_path, encoding="utf-8")
    return {s: dict(parser.items(s)) for s in parser.sections()}


def _write_raw(config_path: Path, raw: dict[str, dict[str, str]]) -> None:
    parser = configparser.ConfigParser()
    for section, options in raw.items():
        parser[section] = {k: str(v) for k, v in options.items()}
    with config_path.open("w", encoding="utf-8") as handle:
        parser.write(handle)


def migrate_config_file(config_path: Path) -> list[Migration]:
    """Bring ``config.ini`` up to the current schema. Returns what ran.

    Idempotent: a file already at the current version is not touched at all,
    so this is safe to call on every startup. The caller is expected to hold
    the config FileLock.
    """
    if not config_path.exists():
        return []

    raw = _read_raw(config_path)
    if not raw:
        # No sections at all: the file is empty, or configparser could not read
        # it (it ignores unreadable paths silently rather than raising). Either
        # way there is nothing to migrate, and stamping a version into a file
        # we failed to understand would replace it with a bare stub. Leave it
        # for load_config to report on.
        return []

    todo = pending(raw)
    if not todo:
        return []

    backup = config_path.with_suffix(config_path.suffix + ".bak")
    shutil.copy2(config_path, backup)
    logger.info(
        "CONFIG_MIGRATION: %s is at schema v%d; applying %d migration(s). "
        "Original saved as %s",
        config_path.name,
        read_version(raw),
        len(todo),
        backup.name,
    )

    migrated, applied = migrate(raw)
    for migration in applied:
        logger.info(
            "CONFIG_MIGRATION: v%d applied — %s",
            migration.version,
            migration.description,
        )
    _write_raw(config_path, migrated)
    logger.info(
        "CONFIG_MIGRATION: %s is now at schema v%d.",
        config_path.name,
        current_schema_version(),
    )
    return applied
