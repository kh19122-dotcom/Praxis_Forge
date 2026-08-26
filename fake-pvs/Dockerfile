FROM python:3.12-slim AS runtime

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

ENV FORGE_SEED=obj-002
EXPOSE 8081

CMD ["uvicorn", "fake_pvs.app:app", "--host", "0.0.0.0", "--port", "8081"]

FROM runtime AS test

COPY tests ./tests
RUN pip install --no-cache-dir ".[dev]"

CMD ["pytest", "-q"]
