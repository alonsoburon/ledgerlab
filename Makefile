PORT ?= 8080
BASE ?= http://127.0.0.1:$(PORT)

.PHONY: demo bench figures clean

demo:              ## Run the evaluation manager + dashboard
	LEDGER_PORT=$(PORT) python3 -m ledgerlab

bench:             ## Run the full benchmark suite against a running demo
	python3 scripts/run_benchmarks.py --base-url $(BASE) --output docs/sample-results.json

figures:           ## Regenerate README charts from the committed sample results
	uv run --with matplotlib --with seaborn --with pandas \
		scripts/plot_results.py --input docs/sample-results.json --output docs/figures

clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} +