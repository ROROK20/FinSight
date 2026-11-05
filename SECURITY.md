# Security Policy

## Supported versions

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |
| < 0.1   | :x:                |

## Reporting a vulnerability

**Please do not report security vulnerabilities through public GitHub issues, discussions or
pull requests.** Email **rohan.s.khiani.vora@gmail.com** with what you found, how to reproduce
it, and the affected version, module or endpoint, with any secrets redacted.

We aim to acknowledge within 48 hours and triage within 7 days, then agree a fix and a disclosure
plan with you. We ask for up to 90 days before public disclosure, and credit reporters who want it.

## Dependency audits

Advisory databases move faster than this file: re-run these rather than trusting it to stay current.

```bash
cd frontend && npm ci
npm audit
npm audit --omit=dev
cd .. && pip-audit -r requirements.txt
pip-audit -r requirements-api.txt
```

- **Frontend, re-audited 2026-11-04** (measured 2026-11-04, Node 23.9; Next.js 15.5.28, React 19;
  committed `package-lock.json`). `npm audit`: **0 critical / 0 high / 6 moderate**.
  `npm audit --omit=dev`: **0**. No accepted exception remains. The 6 are one advisory,
  GHSA-g3hv-75wg-m249 (`minimatch` denial of service), reached through the lint chain: `minimatch`
  and five `@typescript-eslint/*` packages. It is not in the production install or the shipped
  bundle and takes no outside input. `npm audit fix` leaves all 6 in place. An `overrides` entry
  in `frontend/package.json` gives Next the build's own `postcss`, and a test fails if it goes.
- **Python, re-verified 2026-11-04.** `pip-audit` 2.10.1 resolved 69 packages for
  `requirements.txt` and 36 for `requirements-api.txt`: 0 vulnerable, none skipped.
  `requirements-api.txt` is the fully pinned runtime, and what the Dockerfile installs.
  `requirements.txt` adds the test tools and leaves `openai`, `transformers` and `torch` as floors:
  the test suite never exercises them, so pin them yourself against a version you have verified.
  `pip install -e .` resolves the `setup.py` floors, which is what CI tests, so it can pick newer
  versions than the audited pins.
- **`starlette` is pinned at 1.3.1, with a matching floor in `setup.py`.** It is a FastAPI
  transitive, versions up to 1.2.1 carry PYSEC-2026-248 and PYSEC-2026-249, and `fastapi` alone
  permits a vulnerable resolve. Never lower it.

## Notes for deployers

FinSight is built for an open-source demo posture: a stateless app, run locally or deployed by
you, that stores no user data.

- **No authentication and no rate limiting.** The endpoints are read-only. CORS is `*` for the
  credential-free demo. Exposed at scale, restrict `allow_origins` and add both.
- **The console script binds `127.0.0.1`** without auto-reload. `FINSIGHT_HOST` and
  `FINSIGHT_RELOAD=1` are explicit opt-ins. If you deploy the frontend, keep `next` on the newest
  15.5.x patch.
- **SEC identity.** Live SEC requests go out under `SEC_EDGAR_USER_AGENT`: your own name and an
  email that reaches you. There is no fallback identity, in the code or `docker-compose.yml`. An
  unset, placeholder or email-less value, or one whose only addresses cannot receive mail
  (example.com/.net/.org, the .example/.invalid/.localhost/.test domains, no-reply addresses),
  makes live SEC requests fail with a `503`. `tests/test_edgar_client.py` checks that every address
  in a shipped file would fail that validator, with one exception: the project's own contact, in
  the documents that name it (`README.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, `setup.py` and the
  issue templates). No code reads it. Requests are spaced by one process-wide limiter (at most
  ~8.3/s, under the SEC's 10/s ceiling); the ticker map is cached for a day and companyfacts for 10
  minutes.
- **Demo mode (`FINSIGHT_DEMO=1`) makes no network request for any ticker** and writes nothing.
  The five bundled tickers are served offline, and any other is refused with a `404`.
- **Input.** A ticker path parameter must be 1–10 letters or digits with an optional `-`/`.`
  share-class separator, or the request is a `422` before any lookup, cache access or third-party
  request. `/meta`, `/report` and `/sentiment` resolve it in SEC EDGAR's ticker map before asking
  Yahoo Finance or fool.com. Query bounds are enforced in `finsight/api/app.py`: `/filings` `limit`
  1–20 and `form_type` held to an SEC form type's characters, not echoed on refusal; `/dcf` `wacc`
  0.05–0.30, `terminal_growth` 0.01–0.05, `n_years` 3–10, and `wacc − terminal_growth ≥ 0.02`;
  `/sentiment` `n_transcripts` 1–4.
- **The transcript scraper is off by default.** Without `FINSIGHT_LIVE_TRANSCRIPTS=1` it requests
  nothing. When on, its outbound requests stay on fool.com: a link is followed, a cached URL
  fetched and a redirect hop taken only if it is `https` on `www.fool.com` / `fool.com` on the
  default port (`is_fool_url`). Its User-Agent names the tool and carries no contact address, and
  the SEC identity is never sent to a third party. Whether that site's terms allow it is yours to check.
- **What is written to disk.** In live mode with transcripts on, `/sentiment` and `/report` cache
  the links to the real earnings-call transcripts they found (ticker, quarter, URL; no text) to
  `~/.finsight/transcripts/<TICKER>_transcripts.json`, reused for 24 hours. The directory is
  created on the first such write, and the file name keeps only `A–Z 0–9 . -` from the ticker, so
  a ticker cannot name a path outside it. Placeholder transcripts are never cached. FinSight
  persists nothing else. The `yfinance` library does, on the live path: its own cache in
  `py-yfinance/` under the user's cache directory, holding `tkr-tz.db` (each ticker's exchange
  timezone) and `cookies.db` (Yahoo's session cookie, no credential of yours).
- **Bundled data.** `finsight/data/companyfacts/SHA256SUMS` pins the SEC snapshots, and a test
  fails if a file changes. A test regenerates `frontend/lib/recordedDemo.json` from the engine and
  fails on drift. Both catch an accidental edit; neither is a signature, since anyone who can
  change a file can change its manifest.
- **Secrets.** Keep `OPENAI_API_KEY` and any credentials in a local `.env`, which is git-ignored.
  Only the placeholders in `.env.example` are tracked.
- **Reports are informational only.** FinSight is not investment advice and provides no warranty
  (see the [LICENSE](LICENSE)).
