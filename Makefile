.PHONY: install test doctor calendar run clean

install:
	pip install -e ".[dev]"

test:
	python -m pytest -q

doctor:
	dcp doctor

calendar:
	dcp calendar

run:
	dcp run --as-of $$(date +%Y-%m-%d)

clean:
	rm -rf data/cache data/out .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
