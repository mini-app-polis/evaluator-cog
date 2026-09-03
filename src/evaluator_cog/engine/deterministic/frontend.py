"""Frontend framework rule checks (Astro, Vite-React, Tailwind, shadcn, CF Pages)."""

from __future__ import annotations

import re
from contextlib import suppress
from pathlib import Path
from typing import Any

from evaluator_cog.engine.deterministic._shared import (
    Finding,
    _finding,
)


def check_astro_framework(repo_path: Path) -> list[Finding]:
    """FE-001: Astro for all static sites."""
    CHECK_ID = "FE-001"
    findings = []
    pkg = repo_path / "package.json"
    pkg_text = pkg.read_text().lower() if pkg.exists() else ""
    has_config = (repo_path / "astro.config.mjs").exists() or (
        repo_path / "astro.config.ts"
    ).exists()
    if '"astro"' not in pkg_text or not has_config:
        findings.append(
            _finding(
                "FE-001",
                "WARN",
                "structural_conformance",
                "Astro framework signals are missing for frontend site.",
                "Use Astro with package dependency and astro.config.* file.",
            )
        )
    return findings


def check_vite_react_ts(repo_path: Path) -> list[Finding]:
    """FE-002: Vite + React + TypeScript for web apps."""
    CHECK_ID = "FE-002"
    findings = []
    pkg = repo_path / "package.json"
    pkg_text = pkg.read_text().lower() if pkg.exists() else ""
    tsconfig_exists = (repo_path / "tsconfig.json").exists()
    if '"typescript"' not in pkg_text or not tsconfig_exists:
        findings.append(
            _finding(
                "FE-002",
                "ERROR",
                "structural_conformance",
                "TypeScript setup missing for React web app.",
                "Add TypeScript dependency and tsconfig.json to satisfy FE-002.",
            )
        )
    for forbidden in ("webpack", "create-react-app", '"next"'):
        if forbidden in pkg_text:
            findings.append(
                _finding(
                    "FE-002",
                    "ERROR",
                    "structural_conformance",
                    f"Forbidden frontend stack signal found: {forbidden}.",
                    "Use Vite + React + TypeScript baseline for web apps.",
                )
            )
            break
    return findings


def check_tailwind(repo_path: Path) -> list[Finding]:
    """FE-003: Tailwind CSS for styling."""
    CHECK_ID = "FE-003"
    findings = []
    pkg = repo_path / "package.json"
    pkg_text = pkg.read_text().lower() if pkg.exists() else ""
    astro_mjs = repo_path / "astro.config.mjs"
    astro_ts = repo_path / "astro.config.ts"
    astro_cfg_text = ""
    if astro_mjs.exists():
        astro_cfg_text += "\n" + astro_mjs.read_text().lower()
    if astro_ts.exists():
        astro_cfg_text += "\n" + astro_ts.read_text().lower()

    has_astro_tailwind = "@astrojs/tailwind" in astro_cfg_text
    has_cfg = (
        (repo_path / "tailwind.config.js").exists()
        or (repo_path / "tailwind.config.ts").exists()
        or (repo_path / "tailwind.config.mjs").exists()
        or has_astro_tailwind
    )
    has_tailwind_signal = '"tailwindcss"' in pkg_text or has_astro_tailwind
    if not has_tailwind_signal or not has_cfg:
        findings.append(
            _finding(
                "FE-003",
                "WARN",
                "structural_conformance",
                "Tailwind CSS setup is incomplete or absent.",
                "Add tailwindcss dependency and tailwind.config.*.",
            )
        )
    if "styled-components" in pkg_text or "emotion" in pkg_text:
        findings.append(
            _finding(
                "FE-003",
                "WARN",
                "structural_conformance",
                "Alternative CSS-in-JS stack detected alongside/instead of Tailwind.",
                "Prefer Tailwind CSS as the primary styling approach.",
            )
        )
    return findings


def check_shadcn(repo_path: Path) -> list[Finding]:
    """FE-004: shadcn/ui for components."""
    CHECK_ID = "FE-004"
    findings = []
    pkg = repo_path / "package.json"
    pkg_text = pkg.read_text().lower() if pkg.exists() else ""
    has_radix = "@radix-ui/" in pkg_text
    has_ui_dir = (repo_path / "src" / "components" / "ui").is_dir()
    if not has_radix and not has_ui_dir:
        findings.append(
            _finding(
                "FE-004",
                "WARN",
                "structural_conformance",
                "shadcn/ui signals not detected (no Radix deps and no src/components/ui).",
                "Adopt shadcn/ui component structure for frontend consistency.",
            )
        )
    return findings


def check_react_hook_form_zod(repo_path: Path) -> list[Finding]:
    """FE-005: React Hook Form + Zod for forms and validation."""
    CHECK_ID = "FE-005"
    findings = []
    pkg = repo_path / "package.json"
    pkg_text = pkg.read_text().lower() if pkg.exists() else ""
    src = repo_path / "src"
    if not src.is_dir():
        return findings
    form_exists = False
    for tsx in src.rglob("*.tsx"):
        text = tsx.read_text()
        if "<form" in text or "<Form" in text:
            form_exists = True
            break
    if form_exists and ('"react-hook-form"' not in pkg_text or '"zod"' not in pkg_text):
        findings.append(
            _finding(
                "FE-005",
                "WARN",
                "structural_conformance",
                "Form components exist but react-hook-form and/or zod is missing.",
                "Use React Hook Form + Zod for form handling and validation.",
            )
        )
    return findings


def _fe008_version_is_pinned_exact(version: str) -> bool:
    v = str(version).strip().strip('"').strip("'")
    if not v or v in ("*", "latest"):
        return False
    if v.startswith(("^", "~", ">", "<")):
        return False
    return not re.search(r"\d+\.[xX](?:\D|$)", v)


def check_astro_pinned_versions(repo_path: Path) -> list[Finding]:
    """FE-008: Astro-related npm dependencies use exact semver pins.

    Flags range markers (^, ~, >=, …), ``latest``, wildcards, and ``1.x``-style
    placeholders in dependency strings for packages whose names contain
    ``astro``.
    """
    CHECK_ID = "FE-008"
    findings: list[Finding] = []
    pkg = repo_path / "package.json"
    if not pkg.exists():
        return findings
    try:
        import json as _json

        data = _json.loads(pkg.read_text())
    except Exception:
        findings.append(
            _finding(
                "FE-008",
                "WARN",
                "structural_conformance",
                "package.json is not valid JSON — cannot validate Astro pin policy.",
                "Repair package.json syntax.",
            )
        )
        return findings

    for section in ("dependencies", "devDependencies"):
        block = data.get(section)
        if not isinstance(block, dict):
            continue
        for name, raw_ver in block.items():
            if "astro" not in str(name).lower():
                continue
            if not isinstance(raw_ver, str):
                findings.append(
                    _finding(
                        "FE-008",
                        "WARN",
                        "structural_conformance",
                        f"{section}: {name} version must be a string semver for FE-008 scanning.",
                        "Use explicit string versions for Astro-related packages.",
                    )
                )
                continue
            if not _fe008_version_is_pinned_exact(raw_ver):
                findings.append(
                    _finding(
                        "FE-008",
                        "WARN",
                        "structural_conformance",
                        f"{section}: {name} is not pinned to an exact version ({raw_ver!r}).",
                        "Pin Astro-related packages to exact versions (no ^, ~, >=, *, latest, or x-range placeholders).",
                    )
                )
    return findings


def _parse_astro_file(path: Path) -> dict[str, Any]:
    """Split an Astro file into frontmatter, <script> bodies, and client flags."""
    try:
        text = path.read_text()
    except OSError:
        return {"frontmatter": "", "scripts": [], "has_client": False, "body": ""}
    frontmatter = ""
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            frontmatter = parts[1]
            body = parts[2]
    scripts = re.findall(r"<script[^>]*>([\s\S]*?)</script>", body, flags=re.IGNORECASE)
    has_client = bool(re.search(r"\bclient:[^\s=]+", body))
    return {
        "frontmatter": frontmatter,
        "scripts": scripts,
        "has_client": has_client,
        "body": body,
    }


def _extract_fetch_urls(chunk: str) -> list[str]:
    return re.findall(r"""fetch\s*\(\s*['"]([^'"]+)['"]""", chunk)


def check_astro_build_time_data(repo_path: Path) -> list[Finding]:
    """FE-009: Runtime fetch URLs must not duplicate build-time fetches."""
    CHECK_ID = "FE-009"
    findings: list[Finding] = []
    build_urls: set[str] = set()
    astro_files = list(repo_path.rglob("*.astro"))
    if not astro_files:
        return findings

    for path in astro_files:
        parsed = _parse_astro_file(path)
        for url in _extract_fetch_urls(parsed["frontmatter"]):
            build_urls.add(url)

    for path in astro_files:
        parsed = _parse_astro_file(path)
        if parsed["has_client"]:
            continue
        combined = "\n".join(parsed["scripts"])
        for url in _extract_fetch_urls(combined):
            if url in build_urls:
                findings.append(
                    _finding(
                        "FE-009",
                        "WARN",
                        "structural_conformance",
                        f"Astro component performs runtime fetch of URL also used in frontmatter ({path.relative_to(repo_path)}).",
                        "Move data to build-time fetch or isolate client-only access with client:* directives.",
                    )
                )
                break
    return findings


def check_astro_runtime_queries(repo_path: Path) -> list[Finding]:
    """FE-010: Undocumented runtime fetches in Astro islands."""
    CHECK_ID = "FE-010"
    findings: list[Finding] = []
    docs_blob = ""
    readme = repo_path / "README.md"
    if readme.exists():
        with suppress(OSError):
            docs_blob += readme.read_text().lower()
    for md in (
        (repo_path / "docs").rglob("*.md") if (repo_path / "docs").is_dir() else []
    ):
        with suppress(OSError):
            docs_blob += md.read_text().lower()

    for path in repo_path.rglob("*.astro"):
        parsed = _parse_astro_file(path)
        if parsed["has_client"]:
            continue
        combined = "\n".join(parsed["scripts"])
        if "fetch(" not in combined:
            continue
        for url in _extract_fetch_urls(combined):
            if url not in docs_blob:
                findings.append(
                    _finding(
                        "FE-010",
                        "WARN",
                        "structural_conformance",
                        f"Runtime fetch URL not documented in README/docs ({path.relative_to(repo_path)}: {url}).",
                        "Document external endpoints or mark the island as client:* when intentional.",
                    )
                )
                break
    return findings


def check_cloudflare_pages_deploy(repo_path: Path) -> list[Finding]:
    """CD-014: Static site deployed via Cloudflare Pages."""
    CHECK_ID = "CD-014"
    findings: list[Finding] = []
    ci = repo_path / ".github" / "workflows" / "ci.yml"
    readme = repo_path / "README.md"

    ci_text = ci.read_text() if ci.exists() else ""
    readme_text = readme.read_text() if readme.exists() else ""

    has_cf_pages = (
        "cloudflare/pages-action" in ci_text
        or "wrangler pages" in ci_text
        or "pages.dev" in readme_text
        or "Cloudflare Pages" in readme_text
    )

    # Check for competing deploy targets
    has_netlify = (repo_path / "netlify.toml").exists()
    has_vercel = (repo_path / "vercel.json").exists()
    has_gh_pages = "gh-pages" in ci_text or "peaceiris/actions-gh-pages" in ci_text

    if has_netlify:
        findings.append(
            _finding(
                "CD-014",
                "WARN",
                "cd_readiness",
                "Static site has netlify.toml — expected Cloudflare Pages deployment.",
                "Remove netlify.toml and configure Cloudflare Pages deploy instead.",
            )
        )
    if has_vercel:
        findings.append(
            _finding(
                "CD-014",
                "WARN",
                "cd_readiness",
                "Static site has vercel.json — expected Cloudflare Pages deployment.",
                "Remove vercel.json and configure Cloudflare Pages deploy instead.",
            )
        )
    if has_gh_pages:
        findings.append(
            _finding(
                "CD-014",
                "WARN",
                "cd_readiness",
                "Static site uses GitHub Pages deploy — expected Cloudflare Pages.",
                "Switch to Cloudflare Pages deploy.",
            )
        )

    if not has_cf_pages and not (has_netlify or has_vercel or has_gh_pages):
        findings.append(
            _finding(
                "CD-014",
                "WARN",
                "cd_readiness",
                "No deployment target detected (no Cloudflare Pages markers in ci.yml or README).",
                "Document the Cloudflare Pages deploy in ci.yml or README.",
            )
        )
    return findings
