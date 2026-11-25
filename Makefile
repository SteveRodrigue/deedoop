# Local CI. `make ci` runs what GitHub runs on a pull request.
.PHONY: ci check lint test image smoke

ci:     ## lint + tests in Docker, image build, smoke test
	bash scripts/ci.sh full

check:  ## lint + tests with this machine's Python (fast, no Docker)
	bash scripts/ci.sh quick

lint:
	ruff check .

test:
	pytest -q

image:
	docker build -t deedup:dev .

smoke: image  ## start the image against an empty /mnt and load every page
	IMAGE=deedup:dev bash scripts/ci.sh smoke
