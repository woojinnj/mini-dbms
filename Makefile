.PHONY: test demo serve benchmark api-loadtest
PYTHON ?= python3

test:
	$(PYTHON) -m unittest discover -s tests -v

demo:
	$(PYTHON) -m minidb exec --file examples/demo.sql

serve:
	$(PYTHON) -m minidb serve

benchmark:
	$(PYTHON) scripts/benchmark.py --rows 1000000 --output benchmarks/latest.json

api-loadtest:
	$(PYTHON) scripts/api_loadtest.py --pairs 500 --clients 16 --workers 8
