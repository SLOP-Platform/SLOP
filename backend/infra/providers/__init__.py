"""Auto-import all infrastructure providers (derived — #993).

Importing this package GLOB-imports every sibling provider module, each of which
self-registers its provider class(es) via the ``@register`` decorator from
``backend.infra.registry``. There is no hand-maintained import list or ``__all__`` to
drift out of sync with the modules on disk — drop a new ``<slot>_<name>.py`` in this
package and it auto-registers (the append-point parallel streams used to collide on, and
which had already drifted: ``auth_authelia`` / ``tunnel_headscale`` were registered by the
registry but were MISSING from the old explicit ``__init__`` list).

Display order is derived from ``(slot, key)`` sorting in ``list_providers()``, NOT import
order, so glob discovery is order-safe.
"""

from __future__ import annotations

import importlib
import pkgutil


def _import_all_provider_modules() -> None:
    """Glob-import every sibling provider module so each self-registers (#993). Private/
    dunder modules (``_*``) are skipped — only ``<slot>_<name>.py`` provider modules."""
    for mod in pkgutil.iter_modules(__path__):
        if not mod.name.startswith("_"):
            importlib.import_module(f"{__name__}.{mod.name}")


_import_all_provider_modules()
