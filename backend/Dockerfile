# syntax=docker/dockerfile:1.10@sha256:865e5dd094beca432e8c0a1d5e1c465db5f998dca4e439981029b3b81fb39ed5
ARG PYTHON_BASE_IMAGE=docker.io/library/python:3.13.14-slim-trixie@sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91
FROM ${PYTHON_BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH=/opt/venv/bin:$PATH

RUN python -m venv /opt/venv \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app
COPY requirements.lock ./requirements.lock
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
RUN python -m pip uninstall --yes setuptools \
    && /usr/local/bin/python -m pip uninstall --yes setuptools

COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
COPY logging.json ./logging.json
COPY app ./app
COPY evaluation ./evaluation

USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "app.serve"]
