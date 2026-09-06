"""Single source of truth for this distribution's version.

Written by semantic-release on every release (see .releaserc.json). Never edit
by hand. Lives here rather than in pyproject.toml's [project] version because
uv.lock records the project's own version, which made every release dirty the
lockfile and turned every subsequent pull --rebase into a manual conflict on a
file git is forbidden to merge.
"""

__version__ = "3.21.4"
