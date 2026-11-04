# syntax=docker/dockerfile:1.7
# RAHBAR api: reads the index, never writes it (section 4.2).
# Build context: api/        docker build -t rahbar-api:<sha> api

# --- build stage: resolve wheels into a self-contained virtualenv -------------
FROM python:3.12-slim-bookworm AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
COPY requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install -r /tmp/requirements.txt

# --- runtime stage: no compilers, no pip cache, non-root --------------------
FROM python:3.12-slim-bookworm
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
# A fixed numeric uid so the pod's runAsUser / runAsNonRoot checks can verify it.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin rahbar
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY app/ app/
USER 10001
EXPOSE 8000
# Exec form: SIGTERM reaches uvicorn directly, so the preStop + grace period work.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
