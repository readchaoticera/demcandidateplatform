.PHONY: install test doctor calendar run site serve clean

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

site:
	python scripts/build_site.py

serve: site
	@echo "http://localhost:8000 - the dashboard fetches data.json, so it must be served"
	python3 -m http.server -d docs 8000

clean:
	rm -rf data/cache data/out .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
