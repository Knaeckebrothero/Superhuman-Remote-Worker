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

To run it locally: backend licenses are read from installed package metadata, so
install the Python deps first (ideally inside the built orchestrator image to match
what ships). The frontend inventory is read directly from `cockpit/package-lock.json`
— no `npm install` needed.

```bash
pip install pip-licenses -r requirements.txt -r orchestrator/requirements.txt
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
| [@angular-devkit/core](https://www.npmjs.com/package/@angular-devkit/core) | 21.2.11 | MIT | ALLOW |
| [@angular-devkit/schematics](https://www.npmjs.com/package/@angular-devkit/schematics) | 21.2.11 | MIT | ALLOW |
| [@angular/cdk](https://www.npmjs.com/package/@angular/cdk) | 21.2.11 | MIT | ALLOW |
| [@angular/common](https://www.npmjs.com/package/@angular/common) | 21.2.17 | MIT | ALLOW |
| [@angular/compiler](https://www.npmjs.com/package/@angular/compiler) | 21.2.17 | MIT | ALLOW |
| [@angular/core](https://www.npmjs.com/package/@angular/core) | 21.2.17 | MIT | ALLOW |
| [@angular/forms](https://www.npmjs.com/package/@angular/forms) | 21.2.17 | MIT | ALLOW |
| [@angular/platform-browser](https://www.npmjs.com/package/@angular/platform-browser) | 21.2.17 | MIT | ALLOW |
| [@angular/platform-server](https://www.npmjs.com/package/@angular/platform-server) | 21.2.17 | MIT | ALLOW |
| [@angular/pwa](https://www.npmjs.com/package/@angular/pwa) | 21.2.11 | MIT | ALLOW |
| [@angular/router](https://www.npmjs.com/package/@angular/router) | 21.2.17 | MIT | ALLOW |
| [@angular/service-worker](https://www.npmjs.com/package/@angular/service-worker) | 21.2.17 | MIT | ALLOW |
| [@angular/ssr](https://www.npmjs.com/package/@angular/ssr) | 21.2.11 | MIT | ALLOW |
| [@babel/code-frame](https://www.npmjs.com/package/@babel/code-frame) | 7.29.7 | MIT | ALLOW |
| [@babel/helper-validator-identifier](https://www.npmjs.com/package/@babel/helper-validator-identifier) | 7.29.7 | MIT | ALLOW |
| [@jridgewell/sourcemap-codec](https://www.npmjs.com/package/@jridgewell/sourcemap-codec) | 1.5.5 | MIT | ALLOW |
| [@jsverse/transloco](https://www.npmjs.com/package/@jsverse/transloco) | 8.3.0 | MIT | ALLOW |
| [@jsverse/transloco-locale](https://www.npmjs.com/package/@jsverse/transloco-locale) | 8.3.0 | MIT | ALLOW |
| [@jsverse/transloco-utils](https://www.npmjs.com/package/@jsverse/transloco-utils) | 8.3.0 | MIT | ALLOW |
| [@jsverse/utils](https://www.npmjs.com/package/@jsverse/utils) | 1.0.0-beta.5 | MIT | ALLOW |
| [@monaco-editor/loader](https://www.npmjs.com/package/@monaco-editor/loader) | 1.7.0 | MIT | ALLOW |
| [@schematics/angular](https://www.npmjs.com/package/@schematics/angular) | 21.2.11 | MIT | ALLOW |
| [@standard-schema/spec](https://www.npmjs.com/package/@standard-schema/spec) | 1.1.0 | MIT | ALLOW |
| [@types/trusted-types](https://www.npmjs.com/package/@types/trusted-types) | 2.0.7 | MIT | ALLOW |
| [accepts](https://www.npmjs.com/package/accepts) | 2.0.0 | MIT | ALLOW |
| [ajv](https://www.npmjs.com/package/ajv) | 8.18.0 | MIT | ALLOW |
| [ajv-formats](https://www.npmjs.com/package/ajv-formats) | 3.0.1 | MIT | ALLOW |
| [angular-split](https://www.npmjs.com/package/angular-split) | 20.0.0 | Apache-2.0 | ALLOW |
| [ansi-regex](https://www.npmjs.com/package/ansi-regex) | 6.2.2 | MIT | ALLOW |
| [argparse](https://www.npmjs.com/package/argparse) | 2.0.1 | Python-2.0 | ALLOW |
| [body-parser](https://www.npmjs.com/package/body-parser) | 2.2.2 | MIT | ALLOW |
| [bytes](https://www.npmjs.com/package/bytes) | 3.1.2 | MIT | ALLOW |
| [call-bind-apply-helpers](https://www.npmjs.com/package/call-bind-apply-helpers) | 1.0.2 | MIT | ALLOW |
| [call-bound](https://www.npmjs.com/package/call-bound) | 1.0.4 | MIT | ALLOW |
| [callsites](https://www.npmjs.com/package/callsites) | 3.1.0 | MIT | ALLOW |
| [chalk](https://www.npmjs.com/package/chalk) | 5.6.2 | MIT | ALLOW |
| [cli-cursor](https://www.npmjs.com/package/cli-cursor) | 5.0.0 | MIT | ALLOW |
| [cli-spinners](https://www.npmjs.com/package/cli-spinners) | 3.4.0 | MIT | ALLOW |
| [clipboard](https://www.npmjs.com/package/clipboard) | 2.0.11 | MIT | ALLOW |
| [content-disposition](https://www.npmjs.com/package/content-disposition) | 1.1.0 | MIT | ALLOW |
| [content-type](https://www.npmjs.com/package/content-type) | 1.0.5 | MIT | ALLOW |
| [cookie](https://www.npmjs.com/package/cookie) | 0.7.2 | MIT | ALLOW |
| [cookie-signature](https://www.npmjs.com/package/cookie-signature) | 1.2.2 | MIT | ALLOW |
| [cose-base](https://www.npmjs.com/package/cose-base) | 2.2.0 | MIT | ALLOW |
| [cosmiconfig](https://www.npmjs.com/package/cosmiconfig) | 8.3.6 | MIT | ALLOW |
| [cron-parser](https://www.npmjs.com/package/cron-parser) | 4.9.0 | MIT | ALLOW |
| [cronstrue](https://www.npmjs.com/package/cronstrue) | 2.59.0 | MIT | ALLOW |
| [cytoscape](https://www.npmjs.com/package/cytoscape) | 3.33.2 | MIT | ALLOW |
| [cytoscape-fcose](https://www.npmjs.com/package/cytoscape-fcose) | 2.2.0 | MIT | ALLOW |
| [debug](https://www.npmjs.com/package/debug) | 4.4.3 | MIT | ALLOW |
| [delegate](https://www.npmjs.com/package/delegate) | 3.2.0 | MIT | ALLOW |
| [depd](https://www.npmjs.com/package/depd) | 2.0.0 | MIT | ALLOW |
| [dexie](https://www.npmjs.com/package/dexie) | 4.4.2 | Apache-2.0 | ALLOW |
| [dompurify](https://www.npmjs.com/package/dompurify) | 3.2.7 | (MPL-2.0 OR Apache-2.0) | ALLOW |
| [dunder-proto](https://www.npmjs.com/package/dunder-proto) | 1.0.1 | MIT | ALLOW |
| [ee-first](https://www.npmjs.com/package/ee-first) | 1.1.1 | MIT | ALLOW |
| [encodeurl](https://www.npmjs.com/package/encodeurl) | 2.0.0 | MIT | ALLOW |
| [entities](https://www.npmjs.com/package/entities) | 6.0.1 | BSD-2-Clause | ALLOW |
| [entities](https://www.npmjs.com/package/entities) | 8.0.0 | BSD-2-Clause | ALLOW |
| [error-ex](https://www.npmjs.com/package/error-ex) | 1.3.4 | MIT | ALLOW |
| [es-define-property](https://www.npmjs.com/package/es-define-property) | 1.0.1 | MIT | ALLOW |
| [es-errors](https://www.npmjs.com/package/es-errors) | 1.3.0 | MIT | ALLOW |
| [es-object-atoms](https://www.npmjs.com/package/es-object-atoms) | 1.1.1 | MIT | ALLOW |
| [escape-html](https://www.npmjs.com/package/escape-html) | 1.0.3 | MIT | ALLOW |
| [etag](https://www.npmjs.com/package/etag) | 1.8.1 | MIT | ALLOW |
| [express](https://www.npmjs.com/package/express) | 5.2.1 | MIT | ALLOW |
| [fast-deep-equal](https://www.npmjs.com/package/fast-deep-equal) | 3.1.3 | MIT | ALLOW |
| [fast-uri](https://www.npmjs.com/package/fast-uri) | 3.1.2 | BSD-3-Clause | ALLOW |
| [finalhandler](https://www.npmjs.com/package/finalhandler) | 2.1.1 | MIT | ALLOW |
| [forwarded](https://www.npmjs.com/package/forwarded) | 0.2.0 | MIT | ALLOW |
| [fresh](https://www.npmjs.com/package/fresh) | 2.0.0 | MIT | ALLOW |
| [function-bind](https://www.npmjs.com/package/function-bind) | 1.1.2 | MIT | ALLOW |
| [get-east-asian-width](https://www.npmjs.com/package/get-east-asian-width) | 1.5.0 | MIT | ALLOW |
| [get-intrinsic](https://www.npmjs.com/package/get-intrinsic) | 1.3.0 | MIT | ALLOW |
| [get-proto](https://www.npmjs.com/package/get-proto) | 1.0.1 | MIT | ALLOW |
| [good-listener](https://www.npmjs.com/package/good-listener) | 1.2.2 | MIT | ALLOW |
| [gopd](https://www.npmjs.com/package/gopd) | 1.2.0 | MIT | ALLOW |
| [has-symbols](https://www.npmjs.com/package/has-symbols) | 1.1.0 | MIT | ALLOW |
| [hasown](https://www.npmjs.com/package/hasown) | 2.0.3 | MIT | ALLOW |
| [http-errors](https://www.npmjs.com/package/http-errors) | 2.0.1 | MIT | ALLOW |
| [iconv-lite](https://www.npmjs.com/package/iconv-lite) | 0.7.2 | MIT | ALLOW |
| [import-fresh](https://www.npmjs.com/package/import-fresh) | 3.3.1 | MIT | ALLOW |
| [inherits](https://www.npmjs.com/package/inherits) | 2.0.4 | ISC | ALLOW |
| [ipaddr.js](https://www.npmjs.com/package/ipaddr.js) | 1.9.1 | MIT | ALLOW |
| [is-arrayish](https://www.npmjs.com/package/is-arrayish) | 0.2.1 | MIT | ALLOW |
| [is-interactive](https://www.npmjs.com/package/is-interactive) | 2.0.0 | MIT | ALLOW |
| [is-promise](https://www.npmjs.com/package/is-promise) | 4.0.0 | MIT | ALLOW |
| [is-unicode-supported](https://www.npmjs.com/package/is-unicode-supported) | 2.1.0 | MIT | ALLOW |
| [js-tokens](https://www.npmjs.com/package/js-tokens) | 4.0.0 | MIT | ALLOW |
| [js-yaml](https://www.npmjs.com/package/js-yaml) | 4.1.1 | MIT | ALLOW |
| [json-parse-even-better-errors](https://www.npmjs.com/package/json-parse-even-better-errors) | 2.3.1 | MIT | ALLOW |
| [json-schema-traverse](https://www.npmjs.com/package/json-schema-traverse) | 1.0.0 | MIT | ALLOW |
| [jsonc-parser](https://www.npmjs.com/package/jsonc-parser) | 3.3.1 | MIT | ALLOW |
| [layout-base](https://www.npmjs.com/package/layout-base) | 2.0.1 | MIT | ALLOW |
| [lines-and-columns](https://www.npmjs.com/package/lines-and-columns) | 1.2.4 | MIT | ALLOW |
| [log-symbols](https://www.npmjs.com/package/log-symbols) | 7.0.1 | MIT | ALLOW |
| [luxon](https://www.npmjs.com/package/luxon) | 3.7.2 | MIT | ALLOW |
| [magic-string](https://www.npmjs.com/package/magic-string) | 0.30.21 | MIT | ALLOW |
| [marked](https://www.npmjs.com/package/marked) | 17.0.6 | MIT | ALLOW |
| [marked](https://www.npmjs.com/package/marked) | 14.0.0 | MIT | ALLOW |
| [math-intrinsics](https://www.npmjs.com/package/math-intrinsics) | 1.1.0 | MIT | ALLOW |
| [media-typer](https://www.npmjs.com/package/media-typer) | 1.1.0 | MIT | ALLOW |
| [merge-descriptors](https://www.npmjs.com/package/merge-descriptors) | 2.0.0 | MIT | ALLOW |
| [mime-db](https://www.npmjs.com/package/mime-db) | 1.54.0 | MIT | ALLOW |
| [mime-types](https://www.npmjs.com/package/mime-types) | 3.0.2 | MIT | ALLOW |
| [mimic-function](https://www.npmjs.com/package/mimic-function) | 5.0.1 | MIT | ALLOW |
| [monaco-editor](https://www.npmjs.com/package/monaco-editor) | 0.55.1 | MIT | ALLOW |
| [ms](https://www.npmjs.com/package/ms) | 2.1.3 | MIT | ALLOW |
| [negotiator](https://www.npmjs.com/package/negotiator) | 1.0.0 | MIT | ALLOW |
| [ngx-markdown](https://www.npmjs.com/package/ngx-markdown) | 21.2.0 | MIT | ALLOW |
| [object-inspect](https://www.npmjs.com/package/object-inspect) | 1.13.4 | MIT | ALLOW |
| [on-finished](https://www.npmjs.com/package/on-finished) | 2.4.1 | MIT | ALLOW |
| [once](https://www.npmjs.com/package/once) | 1.4.0 | ISC | ALLOW |
| [onetime](https://www.npmjs.com/package/onetime) | 7.0.0 | MIT | ALLOW |
| [ora](https://www.npmjs.com/package/ora) | 9.3.0 | MIT | ALLOW |
| [parent-module](https://www.npmjs.com/package/parent-module) | 1.0.1 | MIT | ALLOW |
| [parse-json](https://www.npmjs.com/package/parse-json) | 5.2.0 | MIT | ALLOW |
| [parse5](https://www.npmjs.com/package/parse5) | 8.0.1 | MIT | ALLOW |
| [parse5-html-rewriting-stream](https://www.npmjs.com/package/parse5-html-rewriting-stream) | 8.0.0 | MIT | ALLOW |
| [parse5-sax-parser](https://www.npmjs.com/package/parse5-sax-parser) | 8.0.0 | MIT | ALLOW |
| [parseurl](https://www.npmjs.com/package/parseurl) | 1.3.3 | MIT | ALLOW |
| [path-to-regexp](https://www.npmjs.com/package/path-to-regexp) | 8.4.2 | MIT | ALLOW |
| [path-type](https://www.npmjs.com/package/path-type) | 4.0.0 | MIT | ALLOW |
| [picocolors](https://www.npmjs.com/package/picocolors) | 1.1.1 | ISC | ALLOW |
| [picomatch](https://www.npmjs.com/package/picomatch) | 4.0.4 | MIT | ALLOW |
| [prismjs](https://www.npmjs.com/package/prismjs) | 1.30.0 | MIT | ALLOW |
| [proxy-addr](https://www.npmjs.com/package/proxy-addr) | 2.0.7 | MIT | ALLOW |
| [qs](https://www.npmjs.com/package/qs) | 6.15.1 | BSD-3-Clause | ALLOW |
| [range-parser](https://www.npmjs.com/package/range-parser) | 1.2.1 | MIT | ALLOW |
| [raw-body](https://www.npmjs.com/package/raw-body) | 3.0.2 | MIT | ALLOW |
| [require-from-string](https://www.npmjs.com/package/require-from-string) | 2.0.2 | MIT | ALLOW |
| [resolve-from](https://www.npmjs.com/package/resolve-from) | 4.0.0 | MIT | ALLOW |
| [restore-cursor](https://www.npmjs.com/package/restore-cursor) | 5.1.0 | MIT | ALLOW |
| [router](https://www.npmjs.com/package/router) | 2.2.0 | MIT | ALLOW |
| [rxjs](https://www.npmjs.com/package/rxjs) | 7.8.2 | Apache-2.0 | ALLOW |
| [safer-buffer](https://www.npmjs.com/package/safer-buffer) | 2.1.2 | MIT | ALLOW |
| [select](https://www.npmjs.com/package/select) | 1.1.2 | MIT | ALLOW |
| [send](https://www.npmjs.com/package/send) | 1.2.1 | MIT | ALLOW |
| [serve-static](https://www.npmjs.com/package/serve-static) | 2.2.1 | MIT | ALLOW |
| [setprototypeof](https://www.npmjs.com/package/setprototypeof) | 1.2.0 | ISC | ALLOW |
| [side-channel](https://www.npmjs.com/package/side-channel) | 1.1.0 | MIT | ALLOW |
| [side-channel-list](https://www.npmjs.com/package/side-channel-list) | 1.0.1 | MIT | ALLOW |
| [side-channel-map](https://www.npmjs.com/package/side-channel-map) | 1.0.1 | MIT | ALLOW |
| [side-channel-weakmap](https://www.npmjs.com/package/side-channel-weakmap) | 1.0.2 | MIT | ALLOW |
| [signal-exit](https://www.npmjs.com/package/signal-exit) | 4.1.0 | ISC | ALLOW |
| [source-map](https://www.npmjs.com/package/source-map) | 0.7.6 | BSD-3-Clause | ALLOW |
| [state-local](https://www.npmjs.com/package/state-local) | 1.0.7 | MIT | ALLOW |
| [statuses](https://www.npmjs.com/package/statuses) | 2.0.2 | MIT | ALLOW |
| [stdin-discarder](https://www.npmjs.com/package/stdin-discarder) | 0.3.2 | MIT | ALLOW |
| [string-width](https://www.npmjs.com/package/string-width) | 8.2.1 | MIT | ALLOW |
| [strip-ansi](https://www.npmjs.com/package/strip-ansi) | 7.2.0 | MIT | ALLOW |
| [tiny-emitter](https://www.npmjs.com/package/tiny-emitter) | 2.1.0 | MIT | ALLOW |
| [toidentifier](https://www.npmjs.com/package/toidentifier) | 1.0.1 | MIT | ALLOW |
| [tslib](https://www.npmjs.com/package/tslib) | 2.8.1 | 0BSD | ALLOW |
| [type-is](https://www.npmjs.com/package/type-is) | 2.0.1 | MIT | ALLOW |
| [unpipe](https://www.npmjs.com/package/unpipe) | 1.0.0 | MIT | ALLOW |
| [vary](https://www.npmjs.com/package/vary) | 1.1.2 | MIT | ALLOW |
| [wrappy](https://www.npmjs.com/package/wrappy) | 1.0.2 | ISC | ALLOW |
| [xhr2](https://www.npmjs.com/package/xhr2) | 0.2.1 | MIT | ALLOW |
| [yoctocolors](https://www.npmjs.com/package/yoctocolors) | 2.1.2 | MIT | ALLOW |
| [zone.js](https://www.npmjs.com/package/zone.js) | 0.16.2 | MIT | ALLOW |
<!-- END: frontend-inventory -->

---

## This project's own license

The Software itself is licensed under the **MIT License** — see `LICENSE.txt`.
This file concerns only the third-party components distributed alongside it.
