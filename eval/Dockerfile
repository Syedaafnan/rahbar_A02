# syntax=docker/dockerfile:1.7
# RAHBAR eval: the A03 evaluation suite, release gate and coverage check, as a Job image.
# Build context: eval/        docker build -t rahbar-eval:dev eval
#
# The golden set and the other test data are baked in (they are versioned with the
# code). The gate THRESHOLDS are not: they come from the rahbar-gate ConfigMap mounted
# at /config/gate.yaml, so changing one needs no rebuild (A03 F2).
FROM python:3.12-slim-bookworm
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    RESULTS_DIR=/results GATE_CONFIG=/config/gate.yaml
COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt && rm /tmp/requirements.txt
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin rahbar
WORKDIR /app
COPY suite.py gate.py coverage_check.py ragops_metrics.py label_answers.py gate.yaml ./
COPY golden.jsonl golden_e4.jsonl embedding_pairs.jsonl live_queries.jsonl judge_labels.jsonl ./
USER 10001
ENTRYPOINT ["python"]
CMD ["suite.py", "--help"]
