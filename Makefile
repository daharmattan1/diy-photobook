# diy-photobook — convenience targets.
# Windows users without `make` can run the underlying commands directly
# (shown in QUICKSTART.md); nothing here is required.

.PHONY: demo export editor doctor scrub test

demo:            ## build the synthetic demo (fixtures + manifest + book.json)
	python scripts/make_demo.py

export:          ## render export_book/book.pdf + cover.pdf from book.json
	python -m photobook export_book

editor:          ## serve the layout editor at http://127.0.0.1:8765/
	python -m photobook editor

doctor:          ## check the environment (deps, exiftool, dirs)
	python -m photobook doctor

scrub:           ## run the decontamination gate over the tracked tree
	python scripts/scrub_check.py

test:            ## run the test suite
	pytest -q
