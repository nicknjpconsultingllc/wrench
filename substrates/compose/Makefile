PY := .venv/bin/python

.PHONY: venv build demo jitter floor test test-live clean

venv:
	uv venv --python 3.12 .venv && uv pip install --python $(PY) -e ".[dev]" -e ./wrenchctl

build:
	docker build -t wrench-svc:local -f services/Dockerfile .
	docker build -t wrench-agent:local -f services/Dockerfile.agent .

demo: build
	$(PY) -m wrench_compose demo

jitter: build
	$(PY) -m wrench_compose jitter --n 10 --agent noop
	$(PY) -m wrench_compose jitter --n 3 --agent oracle

floor: build
	$(PY) -m wrench_compose floor

test:
	$(PY) -m pytest -q

test-live: build
	$(PY) -m pytest -q -m compose_live

test-partition: build
	$(PY) -m pytest -q -m compose_live tests/test_partition.py

clean:
	-docker compose -f compose/admin.yml down -v --remove-orphans
	-docker compose -f compose/factory.yml down -v --remove-orphans
