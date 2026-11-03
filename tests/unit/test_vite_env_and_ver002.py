"""XSTACK-004's `define` mapping and envPrefix, and VER-002 having no check."""

from __future__ import annotations

from pathlib import Path

import evaluator_cog.engine.deterministic as deterministic
from evaluator_cog.engine.deterministic import run_all_checks
from evaluator_cog.engine.deterministic.config import check_env_var_prefix

_DEFINE_CONFIG = """\
import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  return {
    define: {
      "import.meta.env.SENTRY_DSN_WEB": JSON.stringify(env.SENTRY_DSN_WEB),
      "import.meta.env.API_URL": JSON.stringify(process.env["API_URL"]),
    },
  };
});
"""


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return root


def test_xstack004_accepts_unprefixed_names_mapped_under_define(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        {
            "vite.config.ts": _DEFINE_CONFIG,
            "src/main.ts": (
                "const dsn = import.meta.env.SENTRY_DSN_WEB;\n"
                "const api = import.meta.env.API_URL;\n"
                "const mode = import.meta.env.MODE;\n"
                "const key = import.meta.env.VITE_CLERK_KEY;\n"
            ),
        },
    )
    assert check_env_var_prefix(tmp_path) == []


def test_xstack004_flags_an_unprefixed_name_not_mapped_under_define(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        {
            "vite.config.ts": _DEFINE_CONFIG,
            "src/main.ts": "const secret = import.meta.env.DATABASE_URL;\n",
        },
    )
    findings = check_env_var_prefix(tmp_path)
    assert len(findings) == 1
    assert "DATABASE_URL" in findings[0]["finding"]


def test_xstack004_flags_an_env_prefix_beyond_vite(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "vite.config.ts": (
                'import { defineConfig } from "vite";\n'
                'export default defineConfig({ envPrefix: ["VITE_", "SENTRY_"] });\n'
            ),
            "src/main.ts": "const key = import.meta.env.VITE_CLERK_KEY;\n",
        },
    )
    findings = check_env_var_prefix(tmp_path)
    assert len(findings) == 1
    assert "'SENTRY_'" in findings[0]["finding"]
    assert findings[0]["file"] == "vite.config.ts"


def test_xstack004_accepts_an_env_prefix_of_vite_alone(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "vite.config.ts": 'export default { envPrefix: "VITE_" };\n',
            "src/main.ts": "const key = import.meta.env.VITE_CLERK_KEY;\n",
        },
    )
    assert check_env_var_prefix(tmp_path) == []


def test_ver002_has_no_deterministic_check(tmp_path: Path) -> None:
    """VER-002 is not checkable, so the engine neither runs nor claims it."""
    _write(tmp_path, {"README.md": "# x\n", "CHANGELOG.md": "## [2.0.0]\n"})
    result = run_all_checks(tmp_path, dod_type="new_cog")
    assert "VER-002" not in result.checked_rule_ids
    assert not any(f["rule_id"] == "VER-002" for f in result.findings)
    assert not hasattr(deterministic, "check_breaking_change_footer")
