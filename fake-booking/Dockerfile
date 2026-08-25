FROM python:3.12-slim AS runtime

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

ENV FORGE_SEED=obj-001
EXPOSE 8080

CMD ["uvicorn", "fake_booking.app:app", "--host", "0.0.0.0", "--port", "8080"]

FROM runtime AS test

COPY tests ./tests
RUN pip install --no-cache-dir ".[dev]"

CMD ["pytest", "-q"]
