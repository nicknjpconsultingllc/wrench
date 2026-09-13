# One image for every Python service (gateway, worker, loadgen, probe, chaos,
# pghog); the compose `command` picks the entrypoint. One build instead of six.
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN pip install redis==5.2.1 "psycopg[binary]==3.2.6" docker==7.1.0 requests==2.32.3
WORKDIR /app
COPY wrench_compose/ wrench_compose/
COPY gateway/ gateway/
COPY worker/ worker/
COPY loadgen/ loadgen/
COPY probe/ probe/
COPY chaos/ chaos/
COPY pghog/ pghog/
