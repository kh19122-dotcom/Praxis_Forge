FROM python:3.12-slim AS runtime

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

ENV FORGE_PROXY_PORT=8090
ENV FORGE_ADMIN_PORT=8092
EXPOSE 8090 8092

CMD ["python", "-m", "chaos_proxy"]

FROM runtime AS test

COPY tests ./tests
RUN pip install --no-cache-dir ".[dev]"

CMD ["pytest", "-q"]
