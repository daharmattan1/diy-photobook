# Contributing

Thanks for your interest in improving `diy-photobook`.

## License of contributions

This project is licensed under the **PolyForm Noncommercial License 1.0.0** (see
[`LICENSE`](LICENSE)). By submitting a contribution (a pull request, patch, or
suggestion), you agree that your contribution is provided under the **same
license**, and that you have the right to license it that way. Please don't add
code copied from incompatibly-licensed sources.

## Good contributions

- Cross-platform fixes (this toolkit was built and used on Windows; macOS/Linux
  paths, fonts, and encodings are the least-tested surface — see
  [`docs/SETUP.md`](docs/SETUP.md)).
- Support for additional print shops / trim sizes. The exporter currently targets
  **Blurb Large-Square 12×12 in only** and guards loudly against other sizes; the
  math to adapt it lives in [`docs/PRINT_RUNBOOK.md`](docs/PRINT_RUNBOOK.md).
- Better documentation, clearer error messages, more robust ingest.

## Development setup

```bash
python -m venv .venv && . .venv/Scripts/activate   # or .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
playwright install chromium
python -m photobook doctor
```

Build the synthetic demo (no real photos needed) and run the full export to make
sure your environment works end to end:

```bash
python scripts/make_demo.py
python -m photobook export_book
```

## Running the tests

```bash
pip install pytest
pytest -q
```

## Before you open a PR

- Keep changes focused and describe what you changed and why.
- Don't commit any photos, PDFs, databases, or a real `book.json` — they're
  `.gitignore`d for a reason (this is a public repo; keep personal content out).
- Run `python scripts/scrub_check.py` if you touched anything that might carry a
  path or personal string.
