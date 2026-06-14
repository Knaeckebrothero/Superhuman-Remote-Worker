# Third-Party Licenses

This product (the "Software", MIT-licensed — see `LICENSE.txt`) bundles third-party
open-source components. Their licenses are permissive or weak-copyleft and require
that we **reproduce their copyright and license notices** when we distribute the
Software. This file discharges that obligation for every bundled dependency.

> **This file is generated** from the locked dependency tree — do not hand-edit the
> inventory sections. Regenerate it whenever dependencies change (see
> [How to (re)generate](#how-to-regenerate)). The curated callouts at the top flag
> the components with extra obligations and must be kept in sync by review.

---

## Scope — what is and isn't covered here

**Covered** (libraries we *convey* — i.e. ship inside our distributed artifacts):

- **Backend:** Python packages installed into the orchestrator container image
  (`requirements.txt`, `orchestrator/requirements.txt`, `orchestrator/mcp/requirements.txt`).
- **Frontend:** npm **production** dependencies bundled into the cockpit browser/SSR
  build (`cockpit/package.json` → `dependencies`; `devDependencies` are build-time
  only and are not shipped).

**NOT covered, by design** — server software we deploy but do **not** redistribute:

- **Neo4j, PostgreSQL, MongoDB server images** are pulled by the customer's cluster
  **directly from their official public registries** via our Helm chart. We reference
  them; we never make or transfer a copy, so we are not "conveying" them under their
  licenses (this is why Neo4j Community Edition's GPLv3 does not reach us). Their
  notices travel with the official images.
- We **do** bundle their **client drivers** (`neo4j`, `psycopg`/`psycopg2-binary`,
  `pymongo`/`motor`) into our image — those are libraries we convey, so they **are**
  listed below.
- ⚠️ This exemption holds **only** while the customer pulls from the public registry.
  If you ever mirror, re-tag, cache, or ship those server images yourself (e.g. an
  **air-gapped install**), you become a distributor of that server and must add its
  source offer + notices for that delivery.

---

## Components with NOTICE-file obligations (Apache-2.0 §4(d))

Apache-2.0 requires that, where a dependency provides a `NOTICE` file, its contents be
reproduced in our distribution. The generator surfaces the authoritative set via
`--with-notice-file`; paste each one verbatim under [Backend NOTICE files](#backend-notice-files).
Known NOTICE-bearing dependencies we ship include:

- `neo4j` (Neo4j Python driver) — Apache-2.0
- `pymongo`, `motor` — Apache-2.0
- `boto3` (and `botocore`) — Apache-2.0
- `kubernetes` (Python client) — Apache-2.0
- `aiohttp`, `opentelemetry-api`, `tenacity`, `sentence-transformers` — Apache-2.0

> Treat this list as a review aid, not the source of truth — `--with-notice-file`
> output is authoritative.

---

## Weak-copyleft (LGPL) dependencies — know these

These are **not** permissive. They are fine to bundle in MIT/proprietary software
**because we use them as unmodified, dynamically-importable libraries** (the LGPL's
relink/replace condition is satisfied by Python's import model). Do **not** vendor a
*modified* copy without releasing those modifications. Flag both to counsel before
the on-prem launch:

| Component | License | Notes |
|---|---|---|
| `psycopg` (psycopg3) / `psycopg2-binary` | LGPL-3.0 | PostgreSQL driver. `psycopg2-binary` also bundles `libpq` (PostgreSQL License, permissive). Used via public API only. |
| `paramiko` | LGPL-2.1-or-later | SSH/SFTP client for the remote workspace backend. Used via public API only. |

---

## How to (re)generate

CI keeps this file current automatically — see [`.github/workflows`](.github/workflows):
the `dependency-audit` job runs the policy **gate** (fails the build on a denied
license), and the develop `license-inventory` job **regenerates and commits** the
sections below. Both call one script, the single source of truth for policy:

```bash
# Gate only (what the dependency-audit job runs):
python scripts/check_licenses.py --check

# Regenerate the sections below in place (what license-inventory commits):
python scripts/check_licenses.py --write
```

To run it locally, install the inventory tooling and the dependency tree first, so
the tools see the **actual installed versions** (ideally inside the built orchestrator
image to match what ships):

```bash
pip install pip-licenses -r requirements.txt -r orchestrator/requirements.txt
( cd cockpit && npm ci )
python scripts/check_licenses.py --write   # gate + regenerate
```

> Policy lives in `scripts/check_licenses.py` (ALLOW / WEAK / DENY token lists +
> per-package `OVERRIDES`). A new dependency under GPL/AGPL/SSPL/BSL — or any
> UNKNOWN license under `--strict` — fails the gate.

---

## Backend (Python) — full inventory

<!-- BEGIN: backend-inventory -->
| Package | Version | License | Category |
|---|---|---|---|
<!-- END: backend-inventory -->

### Backend NOTICE files

<!-- BEGIN: backend-notices -->
_No bundled dependency ships a NOTICE file._
<!-- END: backend-notices -->

---

## Frontend (JavaScript/TypeScript) — full inventory

<!-- BEGIN: frontend-inventory -->
| Package | Version | License | Category |
|---|---|---|---|
| [@algolia/abtesting](https://github.com/algolia/algoliasearch-client-javascript) | 1.14.1 | MIT | ALLOW |
| [@algolia/client-abtesting](https://github.com/algolia/algoliasearch-client-javascript) | 5.48.1 | MIT | ALLOW |
| [@algolia/client-analytics](https://github.com/algolia/algoliasearch-client-javascript) | 5.48.1 | MIT | ALLOW |
| [@algolia/client-common](https://github.com/algolia/algoliasearch-client-javascript) | 5.48.1 | MIT | ALLOW |
| [@algolia/client-insights](https://github.com/algolia/algoliasearch-client-javascript) | 5.48.1 | MIT | ALLOW |
| [@algolia/client-personalization](https://github.com/algolia/algoliasearch-client-javascript) | 5.48.1 | MIT | ALLOW |
| [@algolia/client-query-suggestions](https://github.com/algolia/algoliasearch-client-javascript) | 5.48.1 | MIT | ALLOW |
| [@algolia/client-search](https://github.com/algolia/algoliasearch-client-javascript) | 5.48.1 | MIT | ALLOW |
| [@algolia/ingestion](https://github.com/algolia/algoliasearch-client-javascript) | 1.48.1 | MIT | ALLOW |
| [@algolia/monitoring](https://github.com/algolia/algoliasearch-client-javascript) | 1.48.1 | MIT | ALLOW |
| [@algolia/recommend](https://github.com/algolia/algoliasearch-client-javascript) | 5.48.1 | MIT | ALLOW |
| [@algolia/requester-browser-xhr](https://github.com/algolia/algoliasearch-client-javascript) | 5.48.1 | MIT | ALLOW |
| [@algolia/requester-fetch](https://github.com/algolia/algoliasearch-client-javascript) | 5.48.1 | MIT | ALLOW |
| [@algolia/requester-node-http](https://github.com/algolia/algoliasearch-client-javascript) | 5.48.1 | MIT | ALLOW |
| [@angular-devkit/architect](https://github.com/angular/angular-cli) | 0.2102.11 | MIT | ALLOW |
| [@angular-devkit/core](https://github.com/angular/angular-cli) | 21.2.11 | MIT | ALLOW |
| [@angular-devkit/schematics](https://github.com/angular/angular-cli) | 21.2.11 | MIT | ALLOW |
| [@angular/animations](https://github.com/angular/angular) | 21.2.13 | MIT | ALLOW |
| [@angular/cdk](https://github.com/angular/components) | 21.2.11 | MIT | ALLOW |
| [@angular/cli](https://github.com/angular/angular-cli) | 21.2.11 | MIT | ALLOW |
| [@angular/common](https://github.com/angular/angular) | 21.2.13 | MIT | ALLOW |
| [@angular/compiler](https://github.com/angular/angular) | 21.2.13 | MIT | ALLOW |
| [@angular/core](https://github.com/angular/angular) | 21.2.13 | MIT | ALLOW |
| [@angular/forms](https://github.com/angular/angular) | 21.2.13 | MIT | ALLOW |
| [@angular/platform-browser](https://github.com/angular/angular) | 21.2.13 | MIT | ALLOW |
| [@angular/platform-server](https://github.com/angular/angular) | 21.2.13 | MIT | ALLOW |
| [@angular/pwa](https://github.com/angular/angular-cli) | 21.2.11 | MIT | ALLOW |
| [@angular/router](https://github.com/angular/angular) | 21.2.13 | MIT | ALLOW |
| [@angular/service-worker](https://github.com/angular/angular) | 21.2.13 | MIT | ALLOW |
| [@angular/ssr](https://github.com/angular/angular-cli) | 21.2.11 | MIT | ALLOW |
| [@babel/code-frame](https://github.com/babel/babel) | 7.29.0 | MIT | ALLOW |
| [@babel/helper-validator-identifier](https://github.com/babel/babel) | 7.28.5 | MIT | ALLOW |
| [@gar/promise-retry](https://github.com/wraithgar/node-promise-retry) | 1.0.3 | MIT | ALLOW |
| [@hono/node-server](https://github.com/honojs/node-server) | 1.19.14 | MIT | ALLOW |
| [@inquirer/ansi](https://github.com/SBoudrias/Inquirer.js) | 1.0.2 | MIT | ALLOW |
| [@inquirer/checkbox](https://github.com/SBoudrias/Inquirer.js) | 4.3.2 | MIT | ALLOW |
| [@inquirer/confirm](https://github.com/SBoudrias/Inquirer.js) | 5.1.21 | MIT | ALLOW |
| [@inquirer/core](https://github.com/SBoudrias/Inquirer.js) | 10.3.2 | MIT | ALLOW |
| [@inquirer/editor](https://github.com/SBoudrias/Inquirer.js) | 4.2.23 | MIT | ALLOW |
| [@inquirer/expand](https://github.com/SBoudrias/Inquirer.js) | 4.0.23 | MIT | ALLOW |
| [@inquirer/external-editor](https://github.com/SBoudrias/Inquirer.js) | 1.0.3 | MIT | ALLOW |
| [@inquirer/figures](https://github.com/SBoudrias/Inquirer.js) | 1.0.15 | MIT | ALLOW |
| [@inquirer/input](https://github.com/SBoudrias/Inquirer.js) | 4.3.1 | MIT | ALLOW |
| [@inquirer/number](https://github.com/SBoudrias/Inquirer.js) | 3.0.23 | MIT | ALLOW |
| [@inquirer/password](https://github.com/SBoudrias/Inquirer.js) | 4.0.23 | MIT | ALLOW |
| [@inquirer/prompts](https://github.com/SBoudrias/Inquirer.js) | 7.10.1 | MIT | ALLOW |
| [@inquirer/rawlist](https://github.com/SBoudrias/Inquirer.js) | 4.1.11 | MIT | ALLOW |
| [@inquirer/search](https://github.com/SBoudrias/Inquirer.js) | 3.2.2 | MIT | ALLOW |
| [@inquirer/select](https://github.com/SBoudrias/Inquirer.js) | 4.4.2 | MIT | ALLOW |
| [@inquirer/type](https://github.com/SBoudrias/Inquirer.js) | 3.0.10 | MIT | ALLOW |
| [@isaacs/fs-minipass](https://github.com/npm/fs-minipass) | 4.0.1 | ISC | ALLOW |
| [@jridgewell/sourcemap-codec](https://github.com/jridgewell/sourcemaps) | 1.5.5 | MIT | ALLOW |
| [@jsverse/transloco](https://github.com/jsverse/transloco) | 8.3.0 | MIT | ALLOW |
| [@jsverse/transloco-locale](https://github.com/jsverse/transloco) | 8.3.0 | MIT | ALLOW |
| [@jsverse/transloco-utils](https://github.com/jsverse/transloco) | 8.3.0 | MIT | ALLOW |
| [@jsverse/utils](https://github.com/jsverse/utils) | 1.0.0-beta.5 | MIT | ALLOW |
| [@listr2/prompt-adapter-inquirer](https://github.com/listr2/listr2) | 3.0.5 | MIT | ALLOW |
| [@modelcontextprotocol/sdk](https://github.com/modelcontextprotocol/typescript-sdk) | 1.26.0 | MIT | ALLOW |
| [@monaco-editor/loader](https://github.com/suren-atoyan/monaco-loader) | 1.7.0 | MIT | ALLOW |
| [@npmcli/agent](https://github.com/npm/agent) | 4.0.0 | ISC | ALLOW |
| [@npmcli/fs](https://github.com/npm/fs) | 5.0.0 | ISC | ALLOW |
| [@npmcli/git](https://github.com/npm/git) | 7.0.2 | ISC | ALLOW |
| [@npmcli/installed-package-contents](https://github.com/npm/installed-package-contents) | 4.0.0 | ISC | ALLOW |
| [@npmcli/node-gyp](https://github.com/npm/node-gyp) | 5.0.0 | ISC | ALLOW |
| [@npmcli/package-json](https://github.com/npm/package-json) | 7.0.5 | ISC | ALLOW |
| [@npmcli/promise-spawn](https://github.com/npm/promise-spawn) | 9.0.1 | ISC | ALLOW |
| [@npmcli/redact](https://github.com/npm/redact) | 4.0.0 | ISC | ALLOW |
| [@npmcli/run-script](https://github.com/npm/run-script) | 10.0.4 | ISC | ALLOW |
| [@schematics/angular](https://github.com/angular/angular-cli) | 21.2.11 | MIT | ALLOW |
| [@sigstore/bundle](https://github.com/sigstore/sigstore-js) | 4.0.0 | Apache-2.0 | ALLOW |
| [@sigstore/core](https://github.com/sigstore/sigstore-js) | 3.2.0 | Apache-2.0 | ALLOW |
| [@sigstore/protobuf-specs](https://github.com/sigstore/protobuf-specs) | 0.5.1 | Apache-2.0 | ALLOW |
| [@sigstore/sign](https://github.com/sigstore/sigstore-js) | 4.1.1 | Apache-2.0 | ALLOW |
| [@sigstore/tuf](https://github.com/sigstore/sigstore-js) | 4.0.2 | Apache-2.0 | ALLOW |
| [@sigstore/verify](https://github.com/sigstore/sigstore-js) | 3.1.0 | Apache-2.0 | ALLOW |
| [@standard-schema/spec](https://github.com/standard-schema/standard-schema) | 1.1.0 | MIT | ALLOW |
| [@tufjs/canonical-json](https://github.com/theupdateframework/tuf-js) | 2.0.0 | MIT | ALLOW |
| [@tufjs/models](https://github.com/theupdateframework/tuf-js) | 4.1.0 | MIT | ALLOW |
| [@types/node](https://github.com/DefinitelyTyped/DefinitelyTyped) | 20.19.39 | MIT | ALLOW |
| [@types/trusted-types](https://github.com/DefinitelyTyped/DefinitelyTyped) | 2.0.7 | MIT | ALLOW |
| [@yarnpkg/lockfile](https://github.com/yarnpkg/yarn/blob/master/packages/lockfile) | 1.1.0 | BSD-2-Clause | ALLOW |
| [abbrev](https://github.com/npm/abbrev-js) | 4.0.0 | ISC | ALLOW |
| [accepts](https://github.com/jshttp/accepts) | 2.0.0 | MIT | ALLOW |
| [agent-base](https://github.com/TooTallNate/proxy-agents) | 7.1.4 | MIT | ALLOW |
| [ajv](https://github.com/ajv-validator/ajv) | 8.18.0 | MIT | ALLOW |
| [ajv-formats](https://github.com/ajv-validator/ajv-formats) | 3.0.1 | MIT | ALLOW |
| [algoliasearch](https://github.com/algolia/algoliasearch-client-javascript) | 5.48.1 | MIT | ALLOW |
| [angular-split](https://github.com/angular-split/angular-split) | 20.0.0 | Apache-2.0 | ALLOW |
| [ansi-escapes](https://github.com/sindresorhus/ansi-escapes) | 7.3.0 | MIT | ALLOW |
| [ansi-regex](https://github.com/chalk/ansi-regex) | 5.0.1 | MIT | ALLOW |
| [ansi-regex](https://github.com/chalk/ansi-regex) | 6.2.2 | MIT | ALLOW |
| [ansi-styles](https://github.com/chalk/ansi-styles) | 4.3.0 | MIT | ALLOW |
| [ansi-styles](https://github.com/chalk/ansi-styles) | 6.2.3 | MIT | ALLOW |
| [argparse](https://github.com/nodeca/argparse) | 2.0.1 | Python-2.0 | UNKNOWN |
| [balanced-match](https://github.com/juliangruber/balanced-match) | 4.0.4 | MIT | ALLOW |
| [body-parser](https://github.com/expressjs/body-parser) | 2.2.2 | MIT | ALLOW |
| [brace-expansion](https://github.com/juliangruber/brace-expansion) | 5.0.5 | MIT | ALLOW |
| [bytes](https://github.com/visionmedia/bytes.js) | 3.1.2 | MIT | ALLOW |
| [cacache](https://github.com/npm/cacache) | 20.0.4 | ISC | ALLOW |
| [call-bind-apply-helpers](https://github.com/ljharb/call-bind-apply-helpers) | 1.0.2 | MIT | ALLOW |
| [call-bound](https://github.com/ljharb/call-bound) | 1.0.4 | MIT | ALLOW |
| [callsites](https://github.com/sindresorhus/callsites) | 3.1.0 | MIT | ALLOW |
| [chalk](https://github.com/chalk/chalk) | 5.6.2 | MIT | ALLOW |
| [chardet](https://github.com/runk/node-chardet) | 2.1.1 | MIT | ALLOW |
| [chokidar](https://github.com/paulmillr/chokidar) | 5.0.0 | MIT | ALLOW |
| [chownr](https://github.com/isaacs/chownr) | 3.0.0 | BlueOak-1.0.0 | ALLOW |
| [cli-cursor](https://github.com/sindresorhus/cli-cursor) | 5.0.0 | MIT | ALLOW |
| [cli-spinners](https://github.com/sindresorhus/cli-spinners) | 3.4.0 | MIT | ALLOW |
| [cli-truncate](https://github.com/sindresorhus/cli-truncate) | 5.2.0 | MIT | ALLOW |
| [cli-width](https://github.com/knownasilya/cli-width) | 4.1.0 | ISC | ALLOW |
| [clipboard](https://github.com/zenorocha/clipboard.js) | 2.0.11 | MIT | ALLOW |
| [cliui](https://github.com/yargs/cliui) | 9.0.1 | ISC | ALLOW |
| [color-convert](https://github.com/Qix-/color-convert) | 2.0.1 | MIT | ALLOW |
| [color-name](https://github.com/colorjs/color-name) | 1.1.4 | MIT | ALLOW |
| [colorette](https://github.com/jorgebucaran/colorette) | 2.0.20 | MIT | ALLOW |
| [content-disposition](https://github.com/jshttp/content-disposition) | 1.1.0 | MIT | ALLOW |
| [content-type](https://github.com/jshttp/content-type) | 1.0.5 | MIT | ALLOW |
| [cookie](https://github.com/jshttp/cookie) | 0.7.2 | MIT | ALLOW |
| [cookie-signature](https://github.com/visionmedia/node-cookie-signature) | 1.2.2 | MIT | ALLOW |
| [cors](https://github.com/expressjs/cors) | 2.8.6 | MIT | ALLOW |
| [cose-base](https://github.com/iVis-at-Bilkent/cose-base) | 2.2.0 | MIT | ALLOW |
| [cosmiconfig](https://github.com/cosmiconfig/cosmiconfig) | 8.3.6 | MIT | ALLOW |
| [cron-parser](https://github.com/harrisiirak/cron-parser) | 4.9.0 | MIT | ALLOW |
| [cronstrue](https://github.com/bradymholt/cronstrue) | 2.59.0 | MIT | ALLOW |
| [cross-spawn](https://github.com/moxystudio/node-cross-spawn) | 7.0.6 | MIT | ALLOW |
| [cytoscape](https://github.com/cytoscape/cytoscape.js) | 3.33.2 | MIT | ALLOW |
| [cytoscape-fcose](https://github.com/iVis-at-Bilkent/cytoscape.js-fcose) | 2.2.0 | MIT | ALLOW |
| [debug](https://github.com/debug-js/debug) | 4.4.3 | MIT | ALLOW |
| [delegate](https://github.com/zenorocha/delegate) | 3.2.0 | MIT | ALLOW |
| [depd](https://github.com/dougwilson/nodejs-depd) | 2.0.0 | MIT | ALLOW |
| [dexie](https://github.com/dexie/Dexie.js) | 4.4.2 | Apache-2.0 | ALLOW |
| [dompurify](https://github.com/cure53/DOMPurify) | 3.2.7 | (MPL-2.0 OR Apache-2.0) | WEAK |
| [dunder-proto](https://github.com/es-shims/dunder-proto) | 1.0.1 | MIT | ALLOW |
| [ee-first](https://github.com/jonathanong/ee-first) | 1.1.1 | MIT | ALLOW |
| [emoji-regex](https://github.com/mathiasbynens/emoji-regex) | 10.6.0 | MIT | ALLOW |
| [emoji-regex](https://github.com/mathiasbynens/emoji-regex) | 8.0.0 | MIT | ALLOW |
| [encodeurl](https://github.com/pillarjs/encodeurl) | 2.0.0 | MIT | ALLOW |
| [entities](https://github.com/fb55/entities) | 6.0.1 | BSD-2-Clause | ALLOW |
| [entities](https://github.com/fb55/entities) | 8.0.0 | BSD-2-Clause | ALLOW |
| [env-paths](https://github.com/sindresorhus/env-paths) | 2.2.1 | MIT | ALLOW |
| [environment](https://github.com/sindresorhus/environment) | 1.1.0 | MIT | ALLOW |
| [err-code](https://github.com/IndigoUnited/js-err-code) | 2.0.3 | MIT | ALLOW |
| [error-ex](https://github.com/qix-/node-error-ex) | 1.3.4 | MIT | ALLOW |
| [es-define-property](https://github.com/ljharb/es-define-property) | 1.0.1 | MIT | ALLOW |
| [es-errors](https://github.com/ljharb/es-errors) | 1.3.0 | MIT | ALLOW |
| [es-object-atoms](https://github.com/ljharb/es-object-atoms) | 1.1.1 | MIT | ALLOW |
| [escalade](https://github.com/lukeed/escalade) | 3.2.0 | MIT | ALLOW |
| [escape-html](https://github.com/component/escape-html) | 1.0.3 | MIT | ALLOW |
| [etag](https://github.com/jshttp/etag) | 1.8.1 | MIT | ALLOW |
| [eventemitter3](https://github.com/primus/eventemitter3) | 5.0.4 | MIT | ALLOW |
| eventsource | 3.0.7 | MIT | ALLOW |
| [eventsource-parser](https://github.com/rexxars/eventsource-parser) | 3.0.8 | MIT | ALLOW |
| [exponential-backoff](https://github.com/coveooss/exponential-backoff) | 3.1.3 | Apache-2.0 | ALLOW |
| [express](https://github.com/expressjs/express) | 5.2.1 | MIT | ALLOW |
| [express-rate-limit](https://github.com/express-rate-limit/express-rate-limit) | 8.5.1 | MIT | ALLOW |
| [fast-deep-equal](https://github.com/epoberezkin/fast-deep-equal) | 3.1.3 | MIT | ALLOW |
| [fast-uri](https://github.com/fastify/fast-uri) | 3.1.2 | BSD-3-Clause | ALLOW |
| [fdir](https://github.com/thecodrr/fdir) | 6.5.0 | MIT | ALLOW |
| [finalhandler](https://github.com/pillarjs/finalhandler) | 2.1.1 | MIT | ALLOW |
| [forwarded](https://github.com/jshttp/forwarded) | 0.2.0 | MIT | ALLOW |
| [fresh](https://github.com/jshttp/fresh) | 2.0.0 | MIT | ALLOW |
| [fs-minipass](https://github.com/npm/fs-minipass) | 3.0.3 | ISC | ALLOW |
| [function-bind](https://github.com/Raynos/function-bind) | 1.1.2 | MIT | ALLOW |
| [get-caller-file](https://github.com/stefanpenner/get-caller-file) | 2.0.5 | ISC | ALLOW |
| [get-east-asian-width](https://github.com/sindresorhus/get-east-asian-width) | 1.5.0 | MIT | ALLOW |
| [get-intrinsic](https://github.com/ljharb/get-intrinsic) | 1.3.0 | MIT | ALLOW |
| [get-proto](https://github.com/ljharb/get-proto) | 1.0.1 | MIT | ALLOW |
| [glob](https://github.com/isaacs/node-glob) | 13.0.6 | BlueOak-1.0.0 | ALLOW |
| [good-listener](https://github.com/zenorocha/good-listener) | 1.2.2 | MIT | ALLOW |
| [gopd](https://github.com/ljharb/gopd) | 1.2.0 | MIT | ALLOW |
| [graceful-fs](https://github.com/isaacs/node-graceful-fs) | 4.2.11 | ISC | ALLOW |
| [has-symbols](https://github.com/inspect-js/has-symbols) | 1.1.0 | MIT | ALLOW |
| [hasown](https://github.com/inspect-js/hasOwn) | 2.0.3 | MIT | ALLOW |
| [hono](https://github.com/honojs/hono) | 4.12.18 | MIT | ALLOW |
| [hosted-git-info](https://github.com/npm/hosted-git-info) | 9.0.2 | ISC | ALLOW |
| [http-cache-semantics](https://github.com/kornelski/http-cache-semantics) | 4.2.0 | BSD-2-Clause | ALLOW |
| [http-errors](https://github.com/jshttp/http-errors) | 2.0.1 | MIT | ALLOW |
| [http-proxy-agent](https://github.com/TooTallNate/proxy-agents) | 7.0.2 | MIT | ALLOW |
| [https-proxy-agent](https://github.com/TooTallNate/proxy-agents) | 7.0.6 | MIT | ALLOW |
| [iconv-lite](https://github.com/pillarjs/iconv-lite) | 0.7.2 | MIT | ALLOW |
| [ignore-walk](https://github.com/npm/ignore-walk) | 8.0.0 | ISC | ALLOW |
| [import-fresh](https://github.com/sindresorhus/import-fresh) | 3.3.1 | MIT | ALLOW |
| [inherits](https://github.com/isaacs/inherits) | 2.0.4 | ISC | ALLOW |
| [ini](https://github.com/npm/ini) | 6.0.0 | ISC | ALLOW |
| [ip-address](https://github.com/beaugunderson/ip-address) | 10.2.0 | MIT | ALLOW |
| [ipaddr.js](https://github.com/whitequark/ipaddr.js) | 1.9.1 | MIT | ALLOW |
| [is-arrayish](https://github.com/qix-/node-is-arrayish) | 0.2.1 | MIT | ALLOW |
| [is-fullwidth-code-point](https://github.com/sindresorhus/is-fullwidth-code-point) | 3.0.0 | MIT | ALLOW |
| [is-fullwidth-code-point](https://github.com/sindresorhus/is-fullwidth-code-point) | 5.1.0 | MIT | ALLOW |
| [is-interactive](https://github.com/sindresorhus/is-interactive) | 2.0.0 | MIT | ALLOW |
| [is-promise](https://github.com/then/is-promise) | 4.0.0 | MIT | ALLOW |
| [is-unicode-supported](https://github.com/sindresorhus/is-unicode-supported) | 2.1.0 | MIT | ALLOW |
| [isexe](https://github.com/isaacs/isexe) | 2.0.0 | ISC | ALLOW |
| [isexe](https://github.com/isaacs/isexe) | 4.0.0 | BlueOak-1.0.0 | ALLOW |
| [jose](https://github.com/panva/jose) | 6.2.2 | MIT | ALLOW |
| [js-tokens](https://github.com/lydell/js-tokens) | 4.0.0 | MIT | ALLOW |
| [js-yaml](https://github.com/nodeca/js-yaml) | 4.1.1 | MIT | ALLOW |
| [json-parse-even-better-errors](https://github.com/npm/json-parse-even-better-errors) | 2.3.1 | MIT | ALLOW |
| [json-parse-even-better-errors](https://github.com/npm/json-parse-even-better-errors) | 5.0.0 | MIT | ALLOW |
| [json-schema-traverse](https://github.com/epoberezkin/json-schema-traverse) | 1.0.0 | MIT | ALLOW |
| [json-schema-typed](https://github.com/RemyRylan/json-schema-typed) | 8.0.2 | BSD-2-Clause | ALLOW |
| [jsonc-parser](https://github.com/microsoft/node-jsonc-parser) | 3.3.1 | MIT | ALLOW |
| [jsonparse](https://github.com/creationix/jsonparse) | 1.3.1 | MIT | ALLOW |
| [layout-base](https://github.com/iVis-at-Bilkent/layout-base) | 2.0.1 | MIT | ALLOW |
| [lines-and-columns](https://github.com/eventualbuddha/lines-and-columns) | 1.2.4 | MIT | ALLOW |
| [listr2](https://github.com/listr2/listr2) | 9.0.5 | MIT | ALLOW |
| [log-symbols](https://github.com/sindresorhus/log-symbols) | 7.0.1 | MIT | ALLOW |
| [log-update](https://github.com/sindresorhus/log-update) | 6.1.0 | MIT | ALLOW |
| [lru-cache](https://github.com/isaacs/node-lru-cache) | 11.3.5 | BlueOak-1.0.0 | ALLOW |
| [luxon](https://github.com/moment/luxon) | 3.7.2 | MIT | ALLOW |
| [magic-string](https://github.com/Rich-Harris/magic-string) | 0.30.21 | MIT | ALLOW |
| [make-fetch-happen](https://github.com/npm/make-fetch-happen) | 15.0.5 | ISC | ALLOW |
| [marked](https://github.com/markedjs/marked) | 14.0.0 | MIT | ALLOW |
| [marked](https://github.com/markedjs/marked) | 17.0.6 | MIT | ALLOW |
| [math-intrinsics](https://github.com/es-shims/math-intrinsics) | 1.1.0 | MIT | ALLOW |
| [media-typer](https://github.com/jshttp/media-typer) | 1.1.0 | MIT | ALLOW |
| [merge-descriptors](https://github.com/sindresorhus/merge-descriptors) | 2.0.0 | MIT | ALLOW |
| [mime-db](https://github.com/jshttp/mime-db) | 1.54.0 | MIT | ALLOW |
| [mime-types](https://github.com/jshttp/mime-types) | 3.0.2 | MIT | ALLOW |
| [mimic-function](https://github.com/sindresorhus/mimic-function) | 5.0.1 | MIT | ALLOW |
| [minimatch](https://github.com/isaacs/minimatch) | 10.2.5 | BlueOak-1.0.0 | ALLOW |
| [minipass](https://github.com/isaacs/minipass) | 3.3.6 | ISC | ALLOW |
| [minipass](https://github.com/isaacs/minipass) | 7.1.3 | BlueOak-1.0.0 | ALLOW |
| [minipass-collect](https://github.com/isaacs/minipass-collect) | 2.0.1 | ISC | ALLOW |
| [minipass-fetch](https://github.com/npm/minipass-fetch) | 5.0.2 | MIT | ALLOW |
| [minipass-flush](https://github.com/isaacs/minipass-flush) | 1.0.7 | BlueOak-1.0.0 | ALLOW |
| minipass-pipeline | 1.2.4 | ISC | ALLOW |
| [minipass-sized](https://github.com/isaacs/minipass-sized) | 2.0.0 | ISC | ALLOW |
| [minizlib](https://github.com/isaacs/minizlib) | 3.1.0 | MIT | ALLOW |
| [monaco-editor](https://github.com/microsoft/monaco-editor) | 0.55.1 | MIT | ALLOW |
| [ms](https://github.com/vercel/ms) | 2.1.3 | MIT | ALLOW |
| [mute-stream](https://github.com/npm/mute-stream) | 2.0.0 | ISC | ALLOW |
| [negotiator](https://github.com/jshttp/negotiator) | 1.0.0 | MIT | ALLOW |
| [ngx-markdown](https://github.com/jfcere/ngx-markdown) | 21.2.0 | MIT | ALLOW |
| [node-gyp](https://github.com/nodejs/node-gyp) | 12.3.0 | MIT | ALLOW |
| [nopt](https://github.com/npm/nopt) | 9.0.0 | ISC | ALLOW |
| [npm-bundled](https://github.com/npm/npm-bundled) | 5.0.0 | ISC | ALLOW |
| [npm-install-checks](https://github.com/npm/npm-install-checks) | 8.0.0 | BSD-2-Clause | ALLOW |
| [npm-normalize-package-bin](https://github.com/npm/npm-normalize-package-bin) | 5.0.0 | ISC | ALLOW |
| [npm-package-arg](https://github.com/npm/npm-package-arg) | 13.0.2 | ISC | ALLOW |
| [npm-packlist](https://github.com/npm/npm-packlist) | 10.0.4 | ISC | ALLOW |
| [npm-pick-manifest](https://github.com/npm/npm-pick-manifest) | 11.0.3 | ISC | ALLOW |
| [npm-registry-fetch](https://github.com/npm/npm-registry-fetch) | 19.1.1 | ISC | ALLOW |
| [object-assign](https://github.com/sindresorhus/object-assign) | 4.1.1 | MIT | ALLOW |
| [object-inspect](https://github.com/inspect-js/object-inspect) | 1.13.4 | MIT | ALLOW |
| [on-finished](https://github.com/jshttp/on-finished) | 2.4.1 | MIT | ALLOW |
| [once](https://github.com/isaacs/once) | 1.4.0 | ISC | ALLOW |
| [onetime](https://github.com/sindresorhus/onetime) | 7.0.0 | MIT | ALLOW |
| [ora](https://github.com/sindresorhus/ora) | 9.3.0 | MIT | ALLOW |
| [p-map](https://github.com/sindresorhus/p-map) | 7.0.4 | MIT | ALLOW |
| [pacote](https://github.com/npm/pacote) | 21.3.1 | ISC | ALLOW |
| [parent-module](https://github.com/sindresorhus/parent-module) | 1.0.1 | MIT | ALLOW |
| [parse-json](https://github.com/sindresorhus/parse-json) | 5.2.0 | MIT | ALLOW |
| [parse5](https://github.com/inikulin/parse5) | 8.0.1 | MIT | ALLOW |
| [parse5-html-rewriting-stream](https://github.com/inikulin/parse5) | 8.0.0 | MIT | ALLOW |
| [parse5-sax-parser](https://github.com/inikulin/parse5) | 8.0.0 | MIT | ALLOW |
| [parseurl](https://github.com/pillarjs/parseurl) | 1.3.3 | MIT | ALLOW |
| [path-key](https://github.com/sindresorhus/path-key) | 3.1.1 | MIT | ALLOW |
| [path-scurry](https://github.com/isaacs/path-scurry) | 2.0.2 | BlueOak-1.0.0 | ALLOW |
| [path-to-regexp](https://github.com/pillarjs/path-to-regexp) | 8.4.2 | MIT | ALLOW |
| [path-type](https://github.com/sindresorhus/path-type) | 4.0.0 | MIT | ALLOW |
| [picocolors](https://github.com/alexeyraspopov/picocolors) | 1.1.1 | ISC | ALLOW |
| [picomatch](https://github.com/micromatch/picomatch) | 4.0.4 | MIT | ALLOW |
| [pkce-challenge](https://github.com/crouchcd/pkce-challenge) | 5.0.1 | MIT | ALLOW |
| [prismjs](https://github.com/PrismJS/prism) | 1.30.0 | MIT | ALLOW |
| [proc-log](https://github.com/npm/proc-log) | 6.1.0 | ISC | ALLOW |
| [promise-retry](https://github.com/IndigoUnited/node-promise-retry) | 2.0.1 | MIT | ALLOW |
| [proxy-addr](https://github.com/jshttp/proxy-addr) | 2.0.7 | MIT | ALLOW |
| [qs](https://github.com/ljharb/qs) | 6.15.1 | BSD-3-Clause | ALLOW |
| [range-parser](https://github.com/jshttp/range-parser) | 1.2.1 | MIT | ALLOW |
| [raw-body](https://github.com/stream-utils/raw-body) | 3.0.2 | MIT | ALLOW |
| [readdirp](https://github.com/paulmillr/readdirp) | 5.0.0 | MIT | ALLOW |
| [require-from-string](https://github.com/floatdrop/require-from-string) | 2.0.2 | MIT | ALLOW |
| [resolve-from](https://github.com/sindresorhus/resolve-from) | 4.0.0 | MIT | ALLOW |
| [restore-cursor](https://github.com/sindresorhus/restore-cursor) | 5.1.0 | MIT | ALLOW |
| [retry](https://github.com/tim-kos/node-retry) | 0.12.0 | MIT | ALLOW |
| [rfdc](https://github.com/davidmarkclements/rfdc) | 1.4.1 | MIT | ALLOW |
| [router](https://github.com/pillarjs/router) | 2.2.0 | MIT | ALLOW |
| [rxjs](https://github.com/reactivex/rxjs) | 7.8.2 | Apache-2.0 | ALLOW |
| [safer-buffer](https://github.com/ChALkeR/safer-buffer) | 2.1.2 | MIT | ALLOW |
| [select](https://github.com/zenorocha/select) | 1.1.2 | MIT | ALLOW |
| [semver](https://github.com/npm/node-semver) | 7.7.4 | ISC | ALLOW |
| [send](https://github.com/pillarjs/send) | 1.2.1 | MIT | ALLOW |
| [serve-static](https://github.com/expressjs/serve-static) | 2.2.1 | MIT | ALLOW |
| [setprototypeof](https://github.com/wesleytodd/setprototypeof) | 1.2.0 | ISC | ALLOW |
| [shebang-command](https://github.com/kevva/shebang-command) | 2.0.0 | MIT | ALLOW |
| [shebang-regex](https://github.com/sindresorhus/shebang-regex) | 3.0.0 | MIT | ALLOW |
| [side-channel](https://github.com/ljharb/side-channel) | 1.1.0 | MIT | ALLOW |
| [side-channel-list](https://github.com/ljharb/side-channel-list) | 1.0.1 | MIT | ALLOW |
| [side-channel-map](https://github.com/ljharb/side-channel-map) | 1.0.1 | MIT | ALLOW |
| [side-channel-weakmap](https://github.com/ljharb/side-channel-weakmap) | 1.0.2 | MIT | ALLOW |
| [signal-exit](https://github.com/tapjs/signal-exit) | 4.1.0 | ISC | ALLOW |
| [sigstore](https://github.com/sigstore/sigstore-js) | 4.1.0 | Apache-2.0 | ALLOW |
| [slice-ansi](https://github.com/chalk/slice-ansi) | 7.1.2 | MIT | ALLOW |
| [slice-ansi](https://github.com/chalk/slice-ansi) | 8.0.0 | MIT | ALLOW |
| [smart-buffer](https://github.com/JoshGlazebrook/smart-buffer) | 4.2.0 | MIT | ALLOW |
| [socks](https://github.com/JoshGlazebrook/socks) | 2.8.7 | MIT | ALLOW |
| [socks-proxy-agent](https://github.com/TooTallNate/proxy-agents) | 8.0.5 | MIT | ALLOW |
| [source-map](https://github.com/mozilla/source-map) | 0.7.6 | BSD-3-Clause | ALLOW |
| [spdx-exceptions](https://github.com/kemitchell/spdx-exceptions.json) | 2.5.0 | CC-BY-3.0 | UNKNOWN |
| [spdx-expression-parse](https://github.com/jslicense/spdx-expression-parse.js) | 4.0.0 | MIT | ALLOW |
| [spdx-license-ids](https://github.com/jslicense/spdx-license-ids) | 3.0.23 | CC0-1.0 | ALLOW |
| [ssri](https://github.com/npm/ssri) | 13.0.1 | ISC | ALLOW |
| [state-local](https://github.com/suren-atoyan/state-local) | 1.0.7 | MIT | ALLOW |
| [statuses](https://github.com/jshttp/statuses) | 2.0.2 | MIT | ALLOW |
| [stdin-discarder](https://github.com/sindresorhus/stdin-discarder) | 0.3.2 | MIT | ALLOW |
| [string-width](https://github.com/sindresorhus/string-width) | 4.2.3 | MIT | ALLOW |
| [string-width](https://github.com/sindresorhus/string-width) | 7.2.0 | MIT | ALLOW |
| [string-width](https://github.com/sindresorhus/string-width) | 8.2.1 | MIT | ALLOW |
| [strip-ansi](https://github.com/chalk/strip-ansi) | 6.0.1 | MIT | ALLOW |
| [strip-ansi](https://github.com/chalk/strip-ansi) | 7.2.0 | MIT | ALLOW |
| [tar](https://github.com/isaacs/node-tar) | 7.5.13 | BlueOak-1.0.0 | ALLOW |
| [tiny-emitter](https://github.com/scottcorgan/tiny-emitter) | 2.1.0 | MIT | ALLOW |
| [tinyglobby](https://github.com/SuperchupuDev/tinyglobby) | 0.2.15 | MIT | ALLOW |
| [toidentifier](https://github.com/component/toidentifier) | 1.0.1 | MIT | ALLOW |
| [tslib](https://github.com/Microsoft/tslib) | 2.8.1 | 0BSD | ALLOW |
| [tuf-js](https://github.com/theupdateframework/tuf-js) | 4.1.0 | MIT | ALLOW |
| [type-is](https://github.com/jshttp/type-is) | 2.0.1 | MIT | ALLOW |
| [typescript](https://github.com/microsoft/TypeScript) | 5.9.3 | Apache-2.0 | ALLOW |
| [undici](https://github.com/nodejs/undici) | 7.25.0 | MIT | ALLOW |
| [undici-types](https://github.com/nodejs/undici) | 6.21.0 | MIT | ALLOW |
| [unpipe](https://github.com/stream-utils/unpipe) | 1.0.0 | MIT | ALLOW |
| [validate-npm-package-name](https://github.com/npm/validate-npm-package-name) | 7.0.2 | ISC | ALLOW |
| [vary](https://github.com/jshttp/vary) | 1.1.2 | MIT | ALLOW |
| [which](https://github.com/isaacs/node-which) | 2.0.2 | ISC | ALLOW |
| [which](https://github.com/npm/node-which) | 6.0.1 | ISC | ALLOW |
| [wrap-ansi](https://github.com/chalk/wrap-ansi) | 6.2.0 | MIT | ALLOW |
| [wrap-ansi](https://github.com/chalk/wrap-ansi) | 9.0.2 | MIT | ALLOW |
| [wrappy](https://github.com/npm/wrappy) | 1.0.2 | ISC | ALLOW |
| [xhr2](https://github.com/pwnall/node-xhr2) | 0.2.1 | MIT | ALLOW |
| [y18n](https://github.com/yargs/y18n) | 5.0.8 | ISC | ALLOW |
| [yallist](https://github.com/isaacs/yallist) | 4.0.0 | ISC | ALLOW |
| [yallist](https://github.com/isaacs/yallist) | 5.0.0 | BlueOak-1.0.0 | ALLOW |
| [yargs](https://github.com/yargs/yargs) | 18.0.0 | MIT | ALLOW |
| [yargs-parser](https://github.com/yargs/yargs-parser) | 22.0.0 | ISC | ALLOW |
| [yoctocolors](https://github.com/sindresorhus/yoctocolors) | 2.1.2 | MIT | ALLOW |
| [yoctocolors-cjs](https://github.com/sindresorhus/yoctocolors) | 2.1.3 | MIT | ALLOW |
| [zod](https://github.com/colinhacks/zod) | 4.3.6 | MIT | ALLOW |
| [zod-to-json-schema](https://github.com/StefanTerdell/zod-to-json-schema) | 3.25.2 | ISC | ALLOW |
| [zone.js](https://github.com/angular/angular) | 0.16.1 | MIT | ALLOW |
<!-- END: frontend-inventory -->

---

## This project's own license

The Software itself is licensed under the **MIT License** — see `LICENSE.txt`.
This file concerns only the third-party components distributed alongside it.
