# Contributing

Thanks for taking a look. This is a small reference project, so the bar is
"clear, tested, honest about its numbers".

## Setup

```bash
docker compose up -d
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Before you open a PR

```bash
ruff check . && ruff format --check .
pytest -m "not integration"
PGHADR_TEST_DSN=postgresql://postgres:postgres@localhost:5432/pghadr_test \
PGHADR_TEST_ADMIN_DSN=postgresql://postgres:postgres@localhost:5432/postgres \
  pytest -m integration
```

CI runs the same checks on Python 3.10 to 3.12, the integration tests against
`postgres:16`, a CLI drill that restarts the Postgres container for real, and a
`bicep build` of `infra/main.bicep`.

## Guidelines

- **Never trust a number you didn't measure.** Results in `docs/runs/` are the
  unedited `.md` + `.json` that `pghadr drill --out-dir` writes, with the
  region, SKU and date. Don't round them into a nicer story.
- **A failure is data.** The probe and the drills must never crash on a
  database error; they record it. A drill that can't tell what happened says so
  in its notes and exits non-zero.
- **Keep the definitions stable.** Write gap, switch, lost and in-doubt are
  defined at the top of `src/pghadr/analysis.py`. If you change one, change the
  report text and the README with it.
- **No passwords in DSNs or command lines.** Use `PGPASSWORD` or a `.pgpass`.
- **Prices** in `src/pghadr/cost.py` are reference constants. If you update
  them, say where they came from and when.
- **Tests** for new behaviour: unit tests with the fake connection in
  `tests/unit/test_probe.py` if no database is needed, otherwise an integration
  test marked `@pytest.mark.integration`.
