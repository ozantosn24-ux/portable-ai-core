# Build context is the repository root:
#   docker compose -p drill build app     (compose.yaml sets context: ../..)
#
# Dependencies come from requirements.lock with --require-hashes, so the image content
# is reproducible: an unpinned transitive dependency cannot slip in.
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY deploy/compose-drill/app-entrypoint.sh /usr/local/bin/app-entrypoint.sh

RUN chmod 0755 /usr/local/bin/app-entrypoint.sh \
 && useradd --uid 10001 --create-home --shell /usr/sbin/nologin appuser

USER 10001
EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/app-entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "wozto_ai_reference.api:app", "--host", "0.0.0.0", "--port", "8000"]
