FROM python:3.12-slim@sha256:7a8b475003c4fe15a2cd4e55e5cfc2f3560bdc9333d624f24cdd6d4340fd7a17 AS runtime

WORKDIR /app

ENV PIP_DISABLE_PIP_VERSION_CHECK=1

COPY pyproject.toml constraints.txt ./
COPY src ./src

RUN if grep -vE '^[[:space:]]*(#|$)' constraints.txt >/dev/null; then \
      python -m pip install --no-cache-dir --require-hashes -r constraints.txt; \
    fi \
 && python -m pip install --no-cache-dir --no-deps .

ENTRYPOINT ["scenario-runner"]

FROM runtime AS test

COPY tests ./tests
COPY constraints-dev.txt ./

RUN if grep -vE '^[[:space:]]*(#|$)' constraints-dev.txt >/dev/null; then \
      python -m pip install --no-cache-dir --require-hashes -r constraints-dev.txt; \
    fi \
 && python -m pip install --no-cache-dir --no-deps .

ENTRYPOINT []
CMD ["pytest", "-q"]
