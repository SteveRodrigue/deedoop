# Local CI. `make ci` runs what GitHub runs on a pull request.
.PHONY: ci check lint test audit lock image smoke

ci:     ## lint + tests in Docker, pip-audit, image build, Trivy, smoke test
	bash scripts/ci.sh full

check:  ## lint + tests with this machine's Python (fast, no Docker)
	bash scripts/ci.sh quick

lint:
	ruff check .

test:
	pytest -q

audit:  ## known vulnerabilities in the pinned dependencies (SC9)
	bash scripts/ci.sh audit

lock:   ## regenerate requirements.lock with hashes after changing dependencies in pyproject.toml
	uv pip compile pyproject.toml --generate-hashes --python-version 3.12 \
	  --python-platform x86_64-manylinux_2_28 --no-header -o requirements.lock

image:
	docker build -t deedup:dev .

smoke: image  ## start the image against an empty /mnt and load every page
	IMAGE=deedup:dev bash scripts/ci.sh smoke
