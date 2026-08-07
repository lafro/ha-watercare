"""Shared fixtures for the Watercare test suite.

Uses pytest-homeassistant-custom-component, the standard harness for testing
Home Assistant custom integrations. It supplies the `hass` fixture (a real,
in-memory Home Assistant core instance) plus helpers such as
`MockConfigEntry`. None of these tests contact the network or the real
Watercare API; a couple of the config-flow tests do use an in-memory SQLite
recorder (via the `recorder_mock` fixture), since manifest.json declares
"recorder" as a dependency.
"""

from __future__ import annotations

import sys
from pathlib import Path

# pytest-homeassistant-custom-component ships its own dummy `custom_components`
# package (with an __init__.py) under its testing_config/ directory, used for
# HA core's own tests. The first time *any* `hass` fixture is created in this
# process, Home Assistant's loader does a bare `import custom_components` with
# that directory temporarily on sys.path (homeassistant/loader.py's
# `_async_mount_config_dir`) to discover custom integrations. Because that
# package has an __init__.py, it resolves as a regular (non-namespace)
# package and gets cached in sys.modules under the bare name
# "custom_components" -- permanently, for the rest of the process. Since our
# own custom_components/ (repo root) has no __init__.py, it can only ever
# contribute to a *namespace* package, and namespace packages never override
# an already-cached regular package. Left alone, this means
# `custom_components.watercare` never becomes importable, no matter what
# sys.path contains.
#
# The fix: win that race by importing our own custom_components.watercare
# here, at conftest module-load time -- before the first hass fixture (and
# therefore before HA's own import) ever runs. That caches "custom_components"
# as a namespace package rooted at the repo, which every subsequent
# `import custom_components.<domain>` (ours or HA's) then resolves against.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
import custom_components.watercare  # noqa: E402, F401

pytest_plugins = "pytest_homeassistant_custom_component"

# Deliberately *not* autouse, and deliberately not wrapped in a fixture of
# our own. Tests that drive the integration through Home Assistant's own
# machinery (config flow, full component setup) need two upstream fixtures:
#
#   - `enable_custom_integrations` -- without it, HA's loader only looks at
#     built-in integrations and "watercare" is never found.
#   - `recorder_mock` -- manifest.json declares a dependency on "recorder",
#     so HA sets up a real recorder before it will even hand out a config
#     flow for "watercare". This gives it an in-memory one instead of
#     needing a live database.
#
# `recorder_mock`'s own setup asserts that `hass` has not been created yet,
# so any test using it MUST list it before `hass` (and before
# `enable_custom_integrations`, which itself pulls in `hass`) in its
# parameter list -- pytest resolves fixtures in first-requested order where
# there's no dependency forcing otherwise. See
# https://github.com/MatthewFlamm/pytest-homeassistant-custom-component/issues/132.
#
# Tests that only import and call our functions/classes directly (the
# cost/period/account/timestamp/statistics tests) don't touch HA's
# integration loader at all, so they need neither fixture -- just `hass`.
