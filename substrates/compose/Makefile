PY := .venv/bin/python

.PHONY: venv build demo jitter floor test test-live clean

venv:
	uv venv --python 3.12 .venv && uv pip install --python $(PY) -e ".[dev]"

build:
	docker build -t wrench-svc:local -f services/Dockerfile .

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

clean:
	-docker compose -f compose/admin.yml down -v --remove-orphans
	-docker compose -f compose/factory.yml down -v --remove-orphans
