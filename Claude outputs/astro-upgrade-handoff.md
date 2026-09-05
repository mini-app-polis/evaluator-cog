# Task: get every Astro site onto current Astro with a clean security audit

I have four Astro sites. Two are done or trivial, one needs a hosting
migration, one is untouched. I need all of them on current Astro with
`npm audit` clean, without taking a site down.

Repos are on this machine under `~/git/`. Push with my own credentials —
do not assume a sandbox can push.

## Current state (verified, not remembered)

| repo | astro | @astrojs/cloudflare | output | host | status |
|---|---|---|---|---|---|
| `website-astro-software` | 7.3.1 | none | static | Pages | **done**, audit 0 |
| `website-astro-wcs` | 4.16.19 | 11.2.0 | **server** | Pages | 5 advisories, mitigated |
| `kwalla-dance` | ^4.16.19 | none | static | Pages | not started |
| `website-astro-wcsmn` | ^5.6.1 | none | static | no wrangler | not started |

`kwalla-dance` and `website-astro-wcsmn` are not in `ecosystem-standards/ecosystem.yaml`.

## The constraint that drives all of this

`@astrojs/cloudflare` is chained to Astro by peer deps, and the adapter
version decides the hosting product:

| adapter | targets | astro peer |
|---|---|---|
| 11.2.0 | Cloudflare **Pages** (emits `dist/_worker.js` + `_routes.json`) | ^4 |
| 12.6.13 | **Workers** | ^5.7 |
| 13.7.0 | **Workers** | ^6.3 |
| 14.3.0 | **Workers** (emits `dist/server/entry.mjs`) | ^7.2 |

v11 is the last release supporting Pages. So **any site that needs an
adapter cannot upgrade Astro and stay on Pages.** A site with no adapter
has no such constraint.

## Two recipes

### Recipe A — static site, no adapter (proven on `website-astro-software`)

Applies to `kwalla-dance` and `website-astro-wcsmn`. Verify first that
the site is genuinely static: no `prerender = false`, no API routes under
`src/pages/**/*.ts`, no `astro:assets` / `<Image>`, no session or KV use.
If all zero, it needs no adapter.

1. `astro` → latest (7.3.x). Drop `@astrojs/cloudflare` if present.
2. `@astrojs/tailwind` is **abandoned** — 6.0.2 is its last release and it
   peers on `astro ^3 || ^4 || ^5`. Replace with `@tailwindcss/vite` and
   `tailwindcss@^4`; remove `autoprefixer` and `postcss` (Tailwind 4's
   Vite plugin does both). Delete `tailwind.config.mjs` and
   `postcss.config.cjs`.
3. Move `theme.extend` into `@theme { }` in the global stylesheet, using
   v4 namespaces: `--color-*` feeds `bg-/text-/border-`, `--font-*` feeds
   `font-`, `--shadow-*` feeds `shadow-`. Replace `darkMode: "class"` with
   `@custom-variant dark (&:where(.dark, .dark *));`. Replace the three
   `@tailwind` directives with `@import "tailwindcss";`.
4. `output: "hybrid"` was removed in Astro 5 — use `output: "static"`.
   With no `prerender = false` anywhere, the built output is identical.
5. `wrangler.toml` keeps `pages_build_output_dir = "dist"`.

### Recipe B — SSR site (`website-astro-wcs`)

This site is `output: "server"`. Clerk verifies sessions server-side in
`src/middleware.ts`, `src/lib/auth.ts` and `src/components/Nav.astro`,
and four admin pages gate on it. Removing the adapter fails the build:

    [@clerk/astro/integration] Missing adapter, please update your Astro config to use one.
    [NoAdapterInstalled] Cannot use server-rendered pages without an adapter.

So the adapter stays, which forces the move to Workers.

**The upgrade is already done and verified** on branch
`chore/astro-7-security-upgrade`, with a runbook at
`docs/WORKERS-MIGRATION.md`. On that branch: astro 7.3.1, adapter 14.3.0,
`@clerk/astro` 4.1.0, `@astrojs/preact` 6.0.5, Tailwind 4,
`@clerk/backend` declared explicitly (it was imported by middleware and
resolved only via npm hoisting). `npm audit`: **0 vulnerabilities**.

What remains is Cloudflare-side only:

1. Create a Worker named `kaiano-wcs-website`, connected to the repo,
   build command `npm run build` (Workers Builds = the Pages-equivalent
   Git integration).
2. Recreate every binding and var from the Pages project: the `[vars]`,
   the KV namespace used for sessions, the Images binding. Clerk's secret
   key must be a Worker **secret**, not a plain var.
3. Deploy and test on the `*.workers.dev` URL **before touching DNS**:
   sign in, confirm the Clerk session handshake in `src/middleware.ts`,
   confirm `/admin/*` still rejects a signed-out visitor. A build that
   compiles proves imports resolve and nothing about auth.
4. Move `wcs.kaianolevine.com` from the Pages project to the Worker.
   Only step with downtime. Rollback = point it back; the Pages project
   still builds from `main`.
5. Delete/disable the Pages project so it cannot redeploy over you.
6. Merge the branch, then add `security` to the `release` job's `needs`
   in `.github/workflows/ci.yml` and delete the comment explaining why
   it is ungated.

## Traps that already cost me a day — do not rediscover these

- **A build that compiles is not a site that resolves.** After every
  change, check *where the output lands* against what the host serves.
  The adapter splits output into `dist/client` + `dist/server`; a Pages
  project pointed at `dist` then serves a directory with no `index.html`
  and 404s the whole site. This took production down twice.
- **Adapter + `pages_build_output_dir` does not build at all.** Wrangler
  rejects it: *"The name 'ASSETS' is reserved in Pages projects."* There
  is no middle configuration.
- **Do not declare `main` in `wrangler.toml`** with the adapter present —
  wrangler validates that path before the build has produced it.
- **`vite` cannot be overridden on Astro 4.** Forcing `^6.4.3` breaks
  `@astrojs/preact` (`Could not resolve "astro:preact:opts"`).
- **Transitive advisories can be pinned without upgrading Astro.** On wcs,
  npm `overrides` for `ws`, `undici`, `nanoid`, `sharp`, `esbuild` took
  the audit from 13 (9 high) to 5 (3 high) with no upgrade and no
  hosting change. Already committed on `main`. Do this first everywhere —
  it removes the urgency from the rest.
- **Lockfiles must never be three-way merged.** Two Dependabot branches
  adding the same transitive package produced two identical
  `[[package]]` blocks in `uv.lock`, which then would not parse. All
  repos now carry `.gitattributes` with `<lockfile> -merge`. Keep it.
- **Dependabot needs `commit-message.prefix`.** Its default subject
  ("Bump x from y to z") is not a Conventional Commit, so on
  semantic-release repos it trips VER-001 *and* cuts no release — the fix
  lands on main and never reaches the registry.

## Verify each site before calling it done

1. `npm run build` succeeds.
2. `ls dist` — confirm the layout matches what the host serves
   (`index.html` at the root for a Pages static site; `dist/_worker.js`
   for Pages SSR; `dist/server/entry.mjs` + `dist/client` for Workers).
3. `npm audit` — record the number, do not assume.
4. Grep the built CSS for every custom theme value from the old
   `tailwind.config.mjs` (colors, font stacks, shadows) and confirm the
   `body` rule still resolves. Tailwind 4 compiling is not Tailwind 4
   producing the same styles.
5. For any site with auth: test sign-in on a preview URL before DNS.

## Do not

- Do not run `npm audit fix --force`.
- Do not suppress advisories to make CI green. The audit job on these
  sites is deliberately non-gating (`release` needs only `build`) so a
  red audit is an honest signal, not a blocker. I would rather see it.
- Do not push anything to a site's `main` until its build output has been
  checked against what the host serves.
