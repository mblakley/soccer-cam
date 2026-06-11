"""Loader for community (unsigned, local) pipeline plugins.

A community plugin is a Python *package* (a sub-directory of the community
plugins dir containing ``__init__.py``) whose top-level import calls
:func:`video_grouper.pipeline.register_step` — loading the package makes its
steps available by name in the pipeline config, exactly like a built-in or a
TTT premium plugin. All three tiers feed the SAME registry; the load order is
built-in -> community -> TTT (last writer wins on a name collision).

Trust: community plugins are the user's OWN local code, run in-process with the
orchestrator's full privileges, and are strictly **opt-in** — nothing loads
unless ``[PIPELINE].community_plugins_enabled`` is set. Enabling them is
equivalent to running arbitrary local Python.

Each plugin is imported under a UNIQUE namespaced synthetic module name
(``vg_community_<dirname>``) via :func:`importlib.util.spec_from_file_location`
so any number of community plugins can coexist — unlike the premium loader's
single hardcoded ``"plugin"`` module name + ``sys.path`` dance. This loader
NEVER mutates ``sys.path``. Stdlib-only imports so the lightweight tray bundle
can use it.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType

logger = logging.getLogger(__name__)


class CommunityPluginLoader:
    """Load unsigned, local community plugins from *plugins_dir*.

    Community plugins run unsigned, in-process, with the orchestrator's full
    privileges, and are opt-in (gated on
    ``[PIPELINE].community_plugins_enabled``). See the module docstring for the
    full trust model.
    """

    def __init__(self, plugins_dir: Path):
        self.plugins_dir = Path(plugins_dir)
        self._loaded: dict[str, ModuleType] = {}

    def load_plugins(self) -> None:
        """Import every community plugin package under ``plugins_dir``.

        For each immediate sub-directory containing an ``__init__.py``, load it
        under the synthetic module name ``vg_community_<dirname>`` and execute
        it so its ``register_step(...)`` side effects run. A failure in one
        plugin is logged and skipped (one bad plugin must not break the others),
        with its partial ``sys.modules`` entry cleaned up. ``sys.path`` is never
        mutated.
        """
        if not self.plugins_dir.is_dir():
            logger.debug(
                "Community plugins dir %s does not exist; nothing to load",
                self.plugins_dir,
            )
            return

        for entry in sorted(self.plugins_dir.iterdir()):
            if not entry.is_dir():
                # Loose files are not packages — ignore.
                continue
            init_file = entry / "__init__.py"
            if not init_file.is_file():
                logger.debug(
                    "Community plugin candidate %s has no __init__.py; ignoring",
                    entry.name,
                )
                continue
            self._load_one(entry, init_file)

    def _load_one(self, entry: Path, init_file: Path) -> None:
        module_name = f"vg_community_{entry.name}"
        try:
            spec = importlib.util.spec_from_file_location(
                module_name,
                init_file,
                submodule_search_locations=[str(entry)],
            )
            if spec is None or spec.loader is None:
                logger.warning(
                    "Could not build import spec for community plugin %s; skipping",
                    entry.name,
                )
                return
            module = importlib.util.module_from_spec(spec)
            # Register before exec so intra-package relative imports resolve.
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except Exception:
            logger.warning(
                "Failed to load community plugin %s; skipping",
                entry.name,
                exc_info=True,
            )
            # Drop the partially-initialized package AND any submodules it
            # imported before failing, so a later fixed reload isn't shadowed.
            for name in [
                n
                for n in sys.modules
                if n == module_name or n.startswith(module_name + ".")
            ]:
                del sys.modules[name]
            return

        self._loaded[entry.name] = module
        logger.info("Loaded community plugin: %s", entry.name)

    def get_loaded(self) -> dict[str, ModuleType]:
        """Return a copy of the ``{dir_name: module}`` map of loaded plugins."""
        return dict(self._loaded)
