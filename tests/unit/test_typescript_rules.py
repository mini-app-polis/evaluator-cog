"""The TypeScript branches of the react-app rules, and CD-020's Node clause,
against a TypeScript fixture."""

from __future__ import annotations

from pathlib import Path

import pytest

from evaluator_cog.engine.deterministic import run_all_checks
from evaluator_cog.engine.deterministic.config import check_logger_misuse
from evaluator_cog.engine.deterministic.delivery import (
    check_cd_010_react_app,
    check_no_console_calls,
    check_structured_logging,
)
from evaluator_cog.engine.deterministic.docs import check_public_docstrings
from evaluator_cog.engine.deterministic.packaging import check_cd_020
from evaluator_cog.engine.evaluator_config import EvaluatorConfig

_LOGGER = """\
/** Browser-side structured logger. */
export function createLogger(service: string) {
  const emit = (level: string, e: { event: string; category: string; context?: object }) => {
    const payload = {
      timestamp: new Date().toISOString(),
      service,
      level,
      category: e.category,
      event: e.event,
      ...(e.context ? { context: e.context } : {}),
    };
    console.info(payload);
  };
  return { info: (e) => emit("INFO", e), error: (e) => emit("ERROR", e) };
}
"""

_APP = {
    "package.json": '{"name": "app", "private": true}\n',
    "pnpm-lock.yaml": "lockfileVersion: '9.0'\n",
    "index.html": '<script type="module" src="/src/main.tsx"></script>\n',
    ".env.example": "VITE_SENTRY_DSN_APP=\n",
    "src/instrument.ts": (
        'import * as Sentry from "@sentry/react";\n'
        "Sentry.init({ dsn: import.meta.env.VITE_SENTRY_DSN_APP });\n"
        "export { Sentry };\n"
    ),
    "src/main.tsx": (
        'import { Sentry } from "./instrument";\n'
        'import { createRoot } from "react-dom/client";\n'
        'createRoot(document.getElementById("root")!).render(<App />);\n'
    ),
    "src/lib/logger.ts": _LOGGER,
    "src/lib/logger.test.ts": "console.log('fixture');\n",
    "src/components/Panel.tsx": (
        'import { createLogger } from "@/lib/logger";\n'
        'const logger = createLogger("app");\n'
        'export function Panel() { logger.info({ event: "panel_shown", category: "ui" }); }\n'
    ),
    ".github/workflows/ci.yml": (
        "on: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - run: pnpm install --frozen-lockfile\n      - run: pnpm test\n"
    ),
}


def _repo(tmp_path: Path, **overrides: str | None) -> Path:
    files = {**_APP, **overrides}
    for rel, text in files.items():
        if text is None:
            continue
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return tmp_path


# ── CD-003 ─────────────────────────────────────────────────────────────────


def test_cd003_the_logger_module_may_call_console(tmp_path: Path) -> None:
    assert check_no_console_calls(_repo(tmp_path)) == []


def test_cd003_flags_console_in_tsx(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        **{"src/components/Bad.tsx": "export const Bad = () => console.warn('x');\n"},
    )
    (finding,) = check_no_console_calls(repo)
    assert finding["rule_id"] == "CD-003"
    assert finding["file"] == "src/components/Bad.tsx"


def test_cd003_cli_and_test_files_are_out_of_scope(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        **{
            "src/cli/run.ts": "console.log('hi');\n",
            "src/components/Panel.spec.tsx": "console.log('hi');\n",
        },
    )
    assert check_no_console_calls(repo) == []


# ── CD-008 ─────────────────────────────────────────────────────────────────


def test_cd008_flags_an_expected_outcome_logged_as_an_error(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        **{
            "src/lib/load.ts": (
                'import { createLogger } from "./logger";\n'
                'const logger = createLogger("app");\n'
                'logger.error({ event: "file_not_found", category: "data" });\n'
                'logger.error({ event: "fetch_failed", category: "api" });\n'
                'logger.info({ event: "skipping_item", category: "data" });\n'
            )
        },
    )
    (finding,) = check_logger_misuse(repo)
    assert finding["rule_id"] == "CD-008"
    assert "file_not_found" in finding["finding"]


def test_cd008_without_a_logger_module_has_no_subject(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        **{
            "src/lib/logger.ts": None,
            "src/lib/load.ts": 'logger.error({ event: "not_found" });\n',
        },
    )
    assert check_logger_misuse(repo) == []


# ── CD-009 ─────────────────────────────────────────────────────────────────


def test_cd009_a_logger_module_emitting_the_shape_passes(tmp_path: Path) -> None:
    assert check_structured_logging(_repo(tmp_path)) == []


def test_cd009_flags_a_logger_module_missing_fields(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        **{
            "src/lib/logger.ts": "export const log = (event) => console.info({ event });\n"
        },
    )
    (finding,) = check_structured_logging(repo)
    assert finding["file"] == "src/lib/logger.ts"
    assert "timestamp" in finding["finding"]
    assert "event," not in finding["finding"].split("missing")[1]


def test_cd009_flags_logging_with_no_logger_module(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        **{"src/lib/logger.ts": None, "src/a.ts": "console.log('x');\n"},
    )
    (finding,) = check_structured_logging(repo)
    assert "no logger module" in finding["finding"]


def test_cd009_a_repo_that_does_not_log_has_no_subject(tmp_path: Path) -> None:
    repo = _repo(tmp_path, **{"src/lib/logger.ts": None})
    assert check_structured_logging(repo) == []


# ── CD-010 ─────────────────────────────────────────────────────────────────


def test_cd010_react_app_layers_present(tmp_path: Path) -> None:
    assert check_cd_010_react_app(_repo(tmp_path)) == []


def test_cd010_sentry_in_the_entry_module_counts(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        **{
            "src/instrument.ts": None,
            "src/main.tsx": (
                'import * as Sentry from "@sentry/react";\n'
                "Sentry.init({});\n"
                "createRoot(el).render(<App />);\n"
            ),
        },
    )
    assert check_cd_010_react_app(repo) == []


def test_cd010_sentry_after_render_is_layer_3_missing(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        **{
            "src/instrument.ts": None,
            "src/main.tsx": "createRoot(el).render(<App />);\nSentry.init({});\n",
        },
    )
    (finding,) = check_cd_010_react_app(repo)
    assert finding["finding"].startswith("Layer 3 missing")


def test_cd010_no_dsn_in_env_example_is_layer_3_missing(tmp_path: Path) -> None:
    repo = _repo(tmp_path, **{".env.example": "VITE_API_URL=\n"})
    (finding,) = check_cd_010_react_app(repo)
    assert finding["finding"].startswith("Layer 3 missing")


def test_cd010_logging_around_the_module_is_layer_2_missing(tmp_path: Path) -> None:
    repo = _repo(tmp_path, **{"src/a.ts": "console.error('x');\n"})
    (finding,) = check_cd_010_react_app(repo)
    assert finding["finding"].startswith("Layer 2 missing")
    assert "src/a.ts" in finding["finding"]


# ── DOC-006 ────────────────────────────────────────────────────────────────


def test_doc006_flags_undocumented_exports_from_ts(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        **{
            "src/lib/util.ts": (
                "/** Adds. */\nexport function add(a: number, b: number) { return a + b; }\n\n"
                "export function sub(a: number, b: number) { return a - b; }\n"
                "export const mul = (a: number, b: number) => a * b;\n"
                "/** Divides. */\nexport const div = async (a: number) => a;\n"
                "export class Box {}\n"
                "export const LIMIT = 10;\n"
                "// not a doc comment\nexport function neg(a: number) { return -a; }\n"
            ),
            "src/lib/util.test.ts": "export function helper() {}\n",
            "src/lib/types.d.ts": "export function decl(): void;\n",
        },
    )
    names = sorted(
        f["finding"].split("::")[1].split(":")[0]
        for f in check_public_docstrings(repo, language="typescript")
    )
    assert names == ["Box", "mul", "neg", "sub"]


def test_doc006_tsx_components_need_no_doc_comment(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert check_public_docstrings(repo, language="typescript") == []


# ── Scope: TEST-013 is not run on a react-app ──────────────────────────────


@pytest.mark.parametrize("rule_id", ["TEST-013"])
def test_rule_not_run_on_react_app(tmp_path: Path, rule_id: str) -> None:
    repo = _repo(tmp_path)
    legacy = run_all_checks(repo, language="typescript", dod_type="new_react_app")
    assert rule_id not in legacy.checked_rule_ids
    cfg = EvaluatorConfig(repo_type="react-app")
    result = run_all_checks(repo, language="typescript", evaluator_config=cfg)
    assert rule_id not in result.checked_rule_ids
    assert {
        "CD-003",
        "CD-008",
        "CD-009",
        "CD-010",
        "DOC-006",
    } <= result.checked_rule_ids


# ── CD-020 clause 5 ────────────────────────────────────────────────────────


def _workflow(*runs: str) -> str:
    steps = "".join(f"      - run: {r}\n" for r in runs)
    return f"on: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n{steps}"


def test_cd020_frozen_pnpm_install_passes(tmp_path: Path) -> None:
    assert check_cd_020(_repo(tmp_path)) == []


@pytest.mark.parametrize("run", ["pnpm install", "pnpm i", "cd web && pnpm install"])
def test_cd020_flags_pnpm_install_without_frozen_lockfile(
    tmp_path: Path, run: str
) -> None:
    repo = _repo(tmp_path, **{".github/workflows/site.yml": _workflow(run)})
    (finding,) = check_cd_020(repo)
    assert finding["rule_id"] == "CD-020"
    assert finding["file"] == ".github/workflows/site.yml"


@pytest.mark.parametrize(
    ("run", "flagged"),
    [
        ("npm install", True),
        ("npm i --no-audit", True),
        ("npm ci", False),
        ("npm install -g pnpm", False),
    ],
)
def test_cd020_npm_project_installs_use_npm_ci(
    tmp_path: Path, run: str, flagged: bool
) -> None:
    repo = _repo(
        tmp_path,
        **{
            "pnpm-lock.yaml": None,
            "package-lock.json": "{}\n",
            ".github/workflows/ci.yml": _workflow(run),
        },
    )
    assert bool(check_cd_020(repo)) is flagged


def test_cd020_no_node_lockfile_skips_clause_5(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        **{
            "pnpm-lock.yaml": None,
            ".github/workflows/ci.yml": _workflow("pnpm install"),
        },
    )
    assert check_cd_020(repo) == []


def test_cd020_delegating_jobs_are_not_read(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        **{
            ".github/workflows/ci.yml": (
                "on: push\njobs:\n  build:\n"
                "    uses: org/shared/.github/workflows/node.yml@v1\n"
            )
        },
    )
    assert check_cd_020(repo) == []
