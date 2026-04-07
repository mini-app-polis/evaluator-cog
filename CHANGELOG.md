## [3.1.6](https://github.com/mini-app-polis/evaluator-cog/compare/v3.1.5...v3.1.6) (2026-04-07)


### Bug Fixes

* restore pipeline_eval.py — was overwritten with webhook router content ([b97af73](https://github.com/mini-app-polis/evaluator-cog/commit/b97af7357da739be5eaf2a62bc57361cc69981e4))

## [3.1.5](https://github.com/mini-app-polis/evaluator-cog/compare/v3.1.4...v3.1.5) (2026-04-07)


### Bug Fixes

* adding exception test ([d3aeafc](https://github.com/mini-app-polis/evaluator-cog/commit/d3aeafc07ba694fddec273431ec9557764f2042e))

## [3.1.4](https://github.com/mini-app-polis/evaluator-cog/compare/v3.1.3...v3.1.4) (2026-04-07)


### Bug Fixes

* llm taking yaml for exception context ([4a26662](https://github.com/mini-app-polis/evaluator-cog/commit/4a26662cc1095ee16042b02319caf9190398a6ca))

## [3.1.3](https://github.com/mini-app-polis/evaluator-cog/compare/v3.1.2...v3.1.3) (2026-04-07)


### Bug Fixes

* adding false positives ([22dc2c0](https://github.com/mini-app-polis/evaluator-cog/commit/22dc2c0ecf7c13a7766b0d60a8e9378823b60c6a))

## [3.1.2](https://github.com/mini-app-polis/evaluator-cog/compare/v3.1.1...v3.1.2) (2026-04-07)


### Bug Fixes

* adding exception ([2a7d3cb](https://github.com/mini-app-polis/evaluator-cog/commit/2a7d3cb0a823c8eb91a580daeafa65148d896f8b))

## [3.1.1](https://github.com/mini-app-polis/evaluator-cog/compare/v3.1.0...v3.1.1) (2026-04-06)


### Bug Fixes

* false positive bugs ([89ad949](https://github.com/mini-app-polis/evaluator-cog/commit/89ad9497ec61569891fd078f15c3acab2362e3e4))

# [3.1.0](https://github.com/mini-app-polis/evaluator-cog/compare/v3.0.0...v3.1.0) (2026-04-06)


### Bug Fixes

* formatting ([c29fa08](https://github.com/mini-app-polis/evaluator-cog/commit/c29fa08cdb5015df81b7219e63acb79d1edcd0eb))


### Features

* adding testing for new functionality from 3.0.0 ([a768ee3](https://github.com/mini-app-polis/evaluator-cog/commit/a768ee318bce5e51e8209474ccf927bde305b267))

# [3.0.0](https://github.com/mini-app-polis/evaluator-cog/compare/v2.6.4...v3.0.0) (2026-04-06)


* feat!: read per-repo evaluator.yaml for type/trait/exemption/deferral config (ADR-001, ADR-002) ([9845fa6](https://github.com/mini-app-polis/evaluator-cog/commit/9845fa6b0166b850ec1a3d68171404b5fa797839))


### Bug Fixes

* formatting follow up ([0073502](https://github.com/mini-app-polis/evaluator-cog/commit/007350288081852a37f56524f3dbaca486429486))


### BREAKING CHANGES

* run_all_checks now accepts evaluator_config parameter and derives check routing from repo type taxonomy (v3.0.0) when evaluator.yaml is present. Repos with evaluator.yaml will see significantly reduced false positives as type-scoped auto-exceptions replace ecosystem.yaml check_exceptions. Legacy dod_type path retained for repos without evaluator.yaml during migration period.

## [2.6.4](https://github.com/mini-app-polis/evaluator-cog/compare/v2.6.3...v2.6.4) (2026-04-06)


### Bug Fixes

* logging update ([a030834](https://github.com/mini-app-polis/evaluator-cog/commit/a030834b931ce6d4f7d23a03b473320f6b4787ec))

## [2.6.3](https://github.com/mini-app-polis/evaluator-cog/compare/v2.6.2...v2.6.3) (2026-04-06)


### Bug Fixes

* logging update ([f2cb5d2](https://github.com/mini-app-polis/evaluator-cog/commit/f2cb5d2e9b6894b887c0015550bffd7de0512c95))

## [2.6.2](https://github.com/mini-app-polis/evaluator-cog/compare/v2.6.1...v2.6.2) (2026-04-06)


### Bug Fixes

* logging and tests ([ea90c53](https://github.com/mini-app-polis/evaluator-cog/commit/ea90c5377b411eac2fb7e5b84de51eed6d8f41cb))

## [2.6.1](https://github.com/mini-app-polis/evaluator-cog/compare/v2.6.0...v2.6.1) (2026-04-06)


### Bug Fixes

* flow separation ([c449ccb](https://github.com/mini-app-polis/evaluator-cog/commit/c449ccb1b8225ab3db44a5aa7f5aab484226694a))

# [2.6.0](https://github.com/mini-app-polis/evaluator-cog/compare/v2.5.0...v2.6.0) (2026-04-06)


### Features

* splitting deterministic and non, but keeping one deployment ([4ca6e3c](https://github.com/mini-app-polis/evaluator-cog/commit/4ca6e3c82b7d14c6287271c7604e09ea29953180))

# [2.5.0](https://github.com/mini-app-polis/evaluator-cog/compare/v2.4.0...v2.5.0) (2026-04-06)


### Features

* functional health checks ([a197668](https://github.com/mini-app-polis/evaluator-cog/commit/a19766815e2561aba0baeea7937611951818faf5))

# [2.4.0](https://github.com/mini-app-polis/evaluator-cog/compare/v2.3.3...v2.4.0) (2026-04-06)


### Features

* changes to meet standards findings ([bbbb4c3](https://github.com/mini-app-polis/evaluator-cog/commit/bbbb4c39656d0d8778f230c60b450a090643fd12))

## [2.3.3](https://github.com/mini-app-polis/evaluator-cog/compare/v2.3.2...v2.3.3) (2026-04-06)


### Bug Fixes

* address exception rules ([146be6f](https://github.com/mini-app-polis/evaluator-cog/commit/146be6f77729fa067baa26c6e806b7ccc0c8d4dd))

## [2.3.2](https://github.com/mini-app-polis/evaluator-cog/compare/v2.3.1...v2.3.2) (2026-04-06)


### Bug Fixes

* address exception rules ([8c484cb](https://github.com/mini-app-polis/evaluator-cog/commit/8c484cbe864ddf42ef7521c0b989e497cc31bf09))

## [2.3.1](https://github.com/mini-app-polis/evaluator-cog/compare/v2.3.0...v2.3.1) (2026-04-05)


### Bug Fixes

* findings quality pass — false positives, deduplication, monorepo root fallback ([50712f9](https://github.com/mini-app-polis/evaluator-cog/commit/50712f97630c2b3cc04e5f049f9abdd4487dc301))
* formatting ([ad28cce](https://github.com/mini-app-polis/evaluator-cog/commit/ad28cce39f7b2691b597fb77ed1df2944daf5491))

# [2.3.0](https://github.com/mini-app-polis/evaluator-cog/compare/v2.2.1...v2.3.0) (2026-04-05)


### Features

* monorepo conformance support, CHECK_ID for drift checks, release drift workflow ([b5051e1](https://github.com/mini-app-polis/evaluator-cog/commit/b5051e17430790e8a6d0333d42d375c42c85e897))

## [2.2.1](https://github.com/mini-app-polis/evaluator-cog/compare/v2.2.0...v2.2.1) (2026-04-05)


### Bug Fixes

* correct XSTACK-003 wiring to hono/react only, expand test coverage ([f1c57d4](https://github.com/mini-app-polis/evaluator-cog/commit/f1c57d4147a5a11b7452089e727aa92f4e1a14b4))

# [2.2.0](https://github.com/mini-app-polis/evaluator-cog/compare/v2.1.5...v2.2.0) (2026-04-05)


### Features

* add CD-015, VER-008, XSTACK-003 deterministic checks; expand test coverage to untested functions; bump node to 22 ([b41e846](https://github.com/mini-app-polis/evaluator-cog/commit/b41e84624560443956c01121ecc74ac8962d7e55))

## [2.1.5](https://github.com/mini-app-polis/evaluator-cog/compare/v2.1.4...v2.1.5) (2026-04-05)


### Bug Fixes

* formatting ([7980712](https://github.com/mini-app-polis/evaluator-cog/commit/798071203396945a5dc0ba0444af5e89112097dd))
* normalise astro language to typescript in conformance flow, remove duplicate test ([35e96a8](https://github.com/mini-app-polis/evaluator-cog/commit/35e96a8c835f1cf4bf2507cfbe3bd38f89f77daf))

## [2.1.4](https://github.com/mini-app-polis/evaluator-cog/compare/v2.1.3...v2.1.4) (2026-04-05)


### Bug Fixes

* rename kaiano-ts-utils to common-typescript-utils, honour XSTACK-001 check_exceptions ([2dd8e57](https://github.com/mini-app-polis/evaluator-cog/commit/2dd8e57873bcc10e7c068e3834719e5e3a5ab23b))

## [2.1.3](https://github.com/mini-app-polis/evaluator-cog/compare/v2.1.2...v2.1.3) (2026-04-05)


### Bug Fixes

* addressing false posiive detection ([cf70e72](https://github.com/mini-app-polis/evaluator-cog/commit/cf70e7273c41608937a89a610b3d780f59053ae6))

## [2.1.2](https://github.com/mini-app-polis/evaluator-cog/compare/v2.1.1...v2.1.2) (2026-04-05)


### Bug Fixes

* map flow names to correct repo in webhook handler ([0939e9f](https://github.com/mini-app-polis/evaluator-cog/commit/0939e9fe2e09dcc451c1014d54b2c50fce50f29a))

## [2.1.1](https://github.com/mini-app-polis/evaluator-cog/compare/v2.1.0...v2.1.1) (2026-04-04)


### Bug Fixes

* eliminate LLM false positives — pass checked_rule_ids to gate soft-rule assessment ([3736a35](https://github.com/mini-app-polis/evaluator-cog/commit/3736a3572b55857de7df10e4525a632eb4147ea4))

# [2.1.0](https://github.com/mini-app-polis/evaluator-cog/compare/v2.0.0...v2.1.0) (2026-04-04)


### Bug Fixes

* follow up to last commit ([e092a20](https://github.com/mini-app-polis/evaluator-cog/commit/e092a201e27350a26a205e52da9e71d900f1a53f))


### Features

* implement 24 deterministic check functions — Phase 1 easy checks ([35f63ef](https://github.com/mini-app-polis/evaluator-cog/commit/35f63efde4e91c2e7c9c15575056174cd84b1c39))

# [2.0.0](https://github.com/mini-app-polis/evaluator-cog/compare/v1.8.14...v2.0.0) (2026-04-04)


* feat!: update conformance engine for ecosystem-standards v2.0.0 schema ([82ee534](https://github.com/mini-app-polis/evaluator-cog/commit/82ee534e3e5e19ab2921b17e08e86c2822cf2f22))


### BREAKING CHANGES

* conformance engine now reads dod_type from ecosystem.yaml
for service-type-aware rule filtering via applies_to. check_exceptions
supports both legacy string format and new {rule, reason} object format.
Requires ecosystem-standards >= 2.0.0.

Made-with: Cursor

## [1.8.14](https://github.com/mini-app-polis/evaluator-cog/compare/v1.8.13...v1.8.14) (2026-04-02)


### Bug Fixes

* changing failure detection path handling ([b158e45](https://github.com/mini-app-polis/evaluator-cog/commit/b158e4540cf293d27e6c57e4c45585b6b2b90a3e))

## [1.8.13](https://github.com/mini-app-polis/evaluator-cog/compare/v1.8.12...v1.8.13) (2026-04-02)


### Bug Fixes

* per requirements and health feedback ([297f478](https://github.com/mini-app-polis/evaluator-cog/commit/297f478cf665d7b7eac92f726bac1159554902be))

## [1.8.12](https://github.com/mini-app-polis/evaluator-cog/compare/v1.8.11...v1.8.12) (2026-04-02)


### Bug Fixes

* exception-aware test checks, collapse no-signal LLM findings ([a65d1a2](https://github.com/mini-app-polis/evaluator-cog/commit/a65d1a2095160a79e32f279c54b61bc38adb3acc))

## [1.8.11](https://github.com/mini-app-polis/evaluator-cog/compare/v1.8.10...v1.8.11) (2026-04-02)


### Bug Fixes

* use SENTRY_DSN_EVALUATOR env var for shared Doppler config ([580b15a](https://github.com/mini-app-polis/evaluator-cog/commit/580b15ab61b94e20ace836342b139ce3a20d2009))

## [1.8.10](https://github.com/mini-app-polis/evaluator-cog/compare/v1.8.9...v1.8.10) (2026-04-02)


### Bug Fixes

* fail loudly when standards version fetch fails, remove STANDARDS_VERSION env var ([cd15d97](https://github.com/mini-app-polis/evaluator-cog/commit/cd15d97d944cd8def12a10270c9e1dbffcd04cac))

## [1.8.9](https://github.com/mini-app-polis/evaluator-cog/compare/v1.8.8...v1.8.9) (2026-04-02)


### Bug Fixes

* VER-006 accepts pnpm exec semantic-release, DOC-004 checks monorepo paths ([fd9c7b8](https://github.com/mini-app-polis/evaluator-cog/commit/fd9c7b8642efce7d9946868f41f1644406c5ae81))

## [1.8.8](https://github.com/mini-app-polis/evaluator-cog/compare/v1.8.7...v1.8.8) (2026-03-31)


### Bug Fixes

* **llm:** tighten conformance prompt to reduce hallucinated findings ([cc8d229](https://github.com/mini-app-polis/evaluator-cog/commit/cc8d229e8087f496c119d5ee80a415f014eb6ae2))

## [1.8.7](https://github.com/mini-app-polis/evaluator-cog/compare/v1.8.6...v1.8.7) (2026-03-31)


### Bug Fixes

* uv lock ([a24733b](https://github.com/mini-app-polis/evaluator-cog/commit/a24733b21eb31baefd1224e4f4e3ba7da1ff6a9c))

## [1.8.6](https://github.com/mini-app-polis/evaluator-cog/compare/v1.8.5...v1.8.6) (2026-03-31)


### Bug Fixes

* updating deploy for weekly deploys ([5d59819](https://github.com/mini-app-polis/evaluator-cog/commit/5d5981988b396d202040d917e4ea16bbb2ea561f))

## [1.8.5](https://github.com/mini-app-polis/evaluator-cog/compare/v1.8.4...v1.8.5) (2026-03-31)


### Bug Fixes

* updating deploy for weekly deploys ([654eb3e](https://github.com/mini-app-polis/evaluator-cog/commit/654eb3ecd6f3ac5fe715d20c3601831f605ff31d))

## [1.8.4](https://github.com/mini-app-polis/evaluator-cog/compare/v1.8.3...v1.8.4) (2026-03-31)


### Bug Fixes

* updating deploy for weekly deploys ([41f38c8](https://github.com/mini-app-polis/evaluator-cog/commit/41f38c836fed835bfb3ab10c1d5d32e4ac8762a9))

## [1.8.3](https://github.com/mini-app-polis/evaluator-cog/compare/v1.8.2...v1.8.3) (2026-03-31)


### Bug Fixes

* uv lock ([28c7479](https://github.com/mini-app-polis/evaluator-cog/commit/28c7479c3b8ad96e62223c8c42166d578c8d129f))

## [1.8.2](https://github.com/mini-app-polis/evaluator-cog/compare/v1.8.1...v1.8.2) (2026-03-31)


### Bug Fixes

* updating deploy for weekly deploys ([3a5c4eb](https://github.com/mini-app-polis/evaluator-cog/commit/3a5c4ebfd5c9aa85d91056197e7cf1ee20e12b27))

## [1.8.1](https://github.com/mini-app-polis/evaluator-cog/compare/v1.8.0...v1.8.1) (2026-03-31)


### Bug Fixes

* exception filtering for multi-rule checks, wire cog_subtype to pipeline check gating ([1da609d](https://github.com/mini-app-polis/evaluator-cog/commit/1da609d3d42f3b589aaf8972b5a56c1ca11e8880))
* uv lock ([3e2b20e](https://github.com/mini-app-polis/evaluator-cog/commit/3e2b20edf0e45b636f41071b65a771d660987999))

# [1.8.0](https://github.com/mini-app-polis/evaluator-cog/compare/v1.7.2...v1.8.0) (2026-03-31)


### Features

* service-type-aware deterministic checks and check_exceptions support ([306e429](https://github.com/mini-app-polis/evaluator-cog/commit/306e42902183a4a72169358deb19fdbf0575fa0c))

## [1.7.2](https://github.com/mini-app-polis/evaluator-cog/compare/v1.7.1...v1.7.2) (2026-03-31)


### Bug Fixes

* update uv lock ([7f5d2c0](https://github.com/mini-app-polis/evaluator-cog/commit/7f5d2c0bf824e681cf3e40aa6da3e577c86d2154))
* updating run id for uniqueness and filtering ([7c79e0b](https://github.com/mini-app-polis/evaluator-cog/commit/7c79e0bf2b5cd21ac01fa117cf32caffe19246e7))

## [1.7.1](https://github.com/mini-app-polis/evaluator-cog/compare/v1.7.0...v1.7.1) (2026-03-31)


### Bug Fixes

* prefer service repo field for conformance download ([2ef42da](https://github.com/mini-app-polis/evaluator-cog/commit/2ef42da152486084b32ad6d038eca1963d11d8a8))

# [1.7.0](https://github.com/mini-app-polis/evaluator-cog/compare/v1.6.0...v1.7.0) (2026-03-29)


### Features

* always post status finding on clean conformance check ([6b58b85](https://github.com/mini-app-polis/evaluator-cog/commit/6b58b85a90a8a4a11320fc40a8033d61eedf8621))

# [1.6.0](https://github.com/mini-app-polis/evaluator-cog/compare/v1.5.1...v1.6.0) (2026-03-29)


### Features

* pass service metadata and live standards to conformance LLM ([973f310](https://github.com/mini-app-polis/evaluator-cog/commit/973f310f5659096cd9adee239acd46bac1a71b06))

## [1.5.1](https://github.com/mini-app-polis/evaluator-cog/compare/v1.5.0...v1.5.1) (2026-03-29)


### Bug Fixes

* install git in Railway build image ([f317d86](https://github.com/mini-app-polis/evaluator-cog/commit/f317d8630d2993d7daf7635544ab9a931119c2e4))

# [1.5.0](https://github.com/mini-app-polis/evaluator-cog/compare/v1.4.0...v1.5.0) (2026-03-29)


### Features

* conformance flow, main.py entrypoint, fix mini_app_polis imports ([2ad1646](https://github.com/mini-app-polis/evaluator-cog/commit/2ad1646037e28aa4e6ca2eaff8283b5def0201c7))

# [1.4.0](https://github.com/mini-app-polis/evaluator-cog/compare/v1.3.0...v1.4.0) (2026-03-28)


### Features

* complete migration of evaluator with new spec ([3720cc6](https://github.com/mini-app-polis/evaluator-cog/commit/3720cc6d1669f0643e6abf0cdaca2fca104499cc))

# [1.3.0](https://github.com/mini-app-polis/evaluator-cog/compare/v1.2.0...v1.3.0) (2026-03-28)


### Features

* clean up ([54f9f15](https://github.com/mini-app-polis/evaluator-cog/commit/54f9f15a46b605688540c5cd2df1d678408bb3bd))

# [1.2.0](https://github.com/mini-app-polis/evaluator-cog/compare/v1.1.0...v1.2.0) (2026-03-28)


### Bug Fixes

* uv lock ([a9135b4](https://github.com/mini-app-polis/evaluator-cog/commit/a9135b42d8200890cdcce149089206e1c1dcec3a))


### Features

* clean up ([bad9112](https://github.com/mini-app-polis/evaluator-cog/commit/bad911243c32d8a702f8da2e573ebbb9c24bf885))

# [1.1.0](https://github.com/mini-app-polis/evaluator-cog/compare/v1.0.0...v1.1.0) (2026-03-28)


### Features

* migration part 2 ([342b6ae](https://github.com/mini-app-polis/evaluator-cog/commit/342b6ae5603a53a1bff652f1f82974b86fc7031d))

# 1.0.0 (2026-03-28)


### Bug Fixes

* adding gitignore ([a729082](https://github.com/mini-app-polis/evaluator-cog/commit/a7290827f702d8df1b65dfef3d7cf9761b8ab46c))
* docs update ([af361a6](https://github.com/mini-app-polis/evaluator-cog/commit/af361a6e5cca0839ec53e5fc556c9c963eb0ef68))
* docs update ([e993f2e](https://github.com/mini-app-polis/evaluator-cog/commit/e993f2e66fb968cef8e698c49d6ea79d949751c7))
* migration part 1 of 2 ([3f2e1ea](https://github.com/mini-app-polis/evaluator-cog/commit/3f2e1ea3788fd6fda17c6fb9f80f0ee572025910))
* test config ([ee73a8e](https://github.com/mini-app-polis/evaluator-cog/commit/ee73a8e16b72c6a50a0b2b72aee677d60ea13f7a))
* updating docs ([4b1a52b](https://github.com/mini-app-polis/evaluator-cog/commit/4b1a52bd6c357a9104ebb32bd757e595cb9cec68))


### Features

* adding flow name information ([0e99290](https://github.com/mini-app-polis/evaluator-cog/commit/0e99290801ccf902920de08475b0335b1483302c))
* Initial commit ([4951a50](https://github.com/mini-app-polis/evaluator-cog/commit/4951a50cb5eab1221959c25e92a0e6742bc69852))
* pass source through to the POST payload ([af0f2d7](https://github.com/mini-app-polis/evaluator-cog/commit/af0f2d7c24f7b5ee78a8780e89e16be0fa568ee8))

# Changelog
