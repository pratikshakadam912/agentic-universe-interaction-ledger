# Apply these files

Copy the `solution/cli.py`, `solution/__init__.py`, `tests/test_core.py`,
`SUBMISSION.md`, and `AI_USAGE.md` into the matching locations in your project.

The CLI expects the existing package modules:

- `solution.database`
- `solution.lead_ingestion`
- `solution.event_ingestion`
- `solution.reporting`
- `solution.normalizers`

Run these commands from the repository root:

```bash
python -m solution.cli ingest data/drop_1
python -m solution.cli ingest data/drop_2
python -m solution.cli report --out reports/
pytest -q
```

If your project uses a different package folder than `solution/`, adjust the
imports and run command to match that folder.
