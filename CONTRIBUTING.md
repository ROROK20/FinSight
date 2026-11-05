# Contributing to FinSight

Thanks for your interest in improving FinSight! This guide covers everything you
need to get a development environment running, make a change, and open a pull
request. Contributions of all sizes — bug fixes, new analytics, docs, tests —
are welcome.

By participating in this project you agree to abide by our
[Code of Conduct](CODE_OF_CONDUCT.md).

---

## Prerequisites

- **Python 3.11+** (the package targets `python_requires=">=3.11"`; the pinned pandas 3 / numpy 2.4 floors have no 3.10 wheels)
- **Node.js 18+** and npm — only needed for the web frontend
- **git**
- A C/build toolchain is only required for the optional `finbert` extra
  (`transformers` + `torch`).

No API keys are required to run the core library or the test suite. An
`OPENAI_API_KEY` is optional and only enables the LLM path of the
`KPIExtractor.extract_from_text` library function (which falls back to regex
otherwise); the API and dashboard use XBRL only.

---

## Setup

```bash
# 1. Fork & clone
git clone https://github.com/<your-username>/finsight.git
cd FinSight

# 2. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate

# 3. Install the package in editable mode with dev dependencies
pip install -e ".[dev]"

# (optional) LLM KPI extraction
pip install -e ".[llm]"

# (optional) FinBERT deep-learning sentiment (heavy: transformers + torch)
pip install -e ".[finbert]"
```

Copy the example environment file and fill in values as needed:

```bash
cp .env.example .env
# Edit .env — set SEC_EDGAR_USER_AGENT (required by SEC for live requests)
```

> `.env` is git-ignored. **Never commit real secrets.**

---

## Running the Test Suite

The backend is tested with `pytest`. All tests must pass before a PR is merged.

```bash
pytest tests/                       # run everything (quiet)
pytest tests/ -v                    # verbose
pytest tests/ --cov=finsight        # with coverage
pytest tests/test_dcf.py -v         # a single module
```

The full suite runs offline and deterministically — it does not hit the network.

---

Two tests guard bundled data. If one fails after a deliberate change, regenerate it and
read the diff before committing:

- **The dashboard's recording drifted** (`tests/test_recorded_demo.py`): the engine's
  answers changed. Run `python scripts/record_demo.py`.
- **A snapshot does not match `SHA256SUMS`** (`tests/test_demo_data.py`): a file in
  `finsight/data/companyfacts/` changed. If you meant it, run
  `cd finsight/data/companyfacts && shasum -a 256 *.json > SHA256SUMS`.

## Linting & Type Checking

The project follows standard PEP 8 style. If you have the optional tooling
installed, run:

```bash
python -m pyflakes finsight tests   # quick correctness lint (optional)
python -m compileall finsight       # syntax check, no extra deps required
```

For the frontend, lint with Next.js' built-in linter (run inside `frontend/`):

```bash
cd frontend
npm run lint                        # next lint
```

Keep imports tidy, prefer type hints (the codebase uses
`from __future__ import annotations`), and write small, focused functions.

---

## Running the Frontend (Web Demo)

The frontend is a Next.js 15.5 / React 19 app that serves an interactive demo on
port **3002**.

```bash
cd frontend
npm install
npm run dev
# → open http://localhost:3002
```

Build a production bundle to verify your changes compile:

```bash
npm run build
```

---

## Running the REST API

```bash
uvicorn finsight.api.app:app --reload --port 8001
# Interactive docs: http://localhost:8001/docs
```

Or via the installed console script:

```bash
finsight                            # launches the API on port 8001
```

---

## Code Style

- **Python:** PEP 8, 4-space indent, type hints, docstrings (NumPy style) on
  public functions. Keep modules cohesive — ingestion / extraction / models /
  sentiment / report / api are separate layers; respect those boundaries.
- **TypeScript / React:** functional components, Tailwind for styling, follow
  the existing conventions in `frontend/app`.
- Add or update tests for any behavior change.
- Update docs (`README.md`, `docs/`) when you change public behavior.

---

## Branch & PR Workflow

1. Create a feature branch off `main`:
   ```bash
   git checkout -b feat/short-description
   ```
2. Make your change, with tests.
3. Run `pytest tests/` (and `npm run build` if you touched the frontend) and
   make sure everything passes.
4. Push your branch and open a Pull Request against `main`.
5. Fill in the PR template, link any related issue, and ensure CI is green.
6. A maintainer will review; address feedback and keep the branch up to date.

---

## Commit Message Convention

This project uses [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(optional scope): <short summary>
```

Common types:

| Type       | Use for                                            |
| ---------- | -------------------------------------------------- |
| `feat`     | a new feature                                      |
| `fix`      | a bug fix                                           |
| `docs`     | documentation only                                 |
| `test`     | adding or fixing tests                             |
| `refactor` | code change that neither fixes a bug nor adds feat |
| `chore`    | tooling, build, deps                               |
| `ci`       | CI configuration                                   |

Examples:

```
feat(dcf): add WACC sensitivity table to DCFResult
fix(edgar): zero-pad CIK before submissions lookup
docs(readme): correct quick-start import path
```

---

## Reporting Issues

Use the [issue templates](.github/ISSUE_TEMPLATE) to file a bug report or
feature request. For **security vulnerabilities**, do **not** open a public
issue — follow [SECURITY.md](SECURITY.md) instead.

---

Thank you for helping make FinSight better! 🚀
