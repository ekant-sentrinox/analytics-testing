# ---------------------------------------------------------------------------
# Generator + pipeline exporter image (SERVER 1).
#
# Optional: the normal path runs both in the project venv via scripts/. This
# exists for reproducible runs and for pointing several generator instances at
# one collector.
#
# Pinned base. A benchmark whose client runtime changes between campaigns
# cannot be compared against itself.
# ---------------------------------------------------------------------------
FROM python:3.11.10-slim-bookworm

# grpcio and psycopg[binary] ship wheels, so no compiler is needed; curl is for
# the healthcheck and for reaching IMDS when debugging credentials.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /work

COPY generator/requirements.txt /work/generator/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r /work/generator/requirements.txt

COPY generator/ /work/generator/
COPY monitoring/exporters/ /work/monitoring/exporters/
COPY bench/ /work/bench/

# config/ and results/ are bind-mounted at run time so a config change does not
# need a rebuild and results survive the container.
RUN mkdir -p /work/results /work/logs /work/config

# Non-root. The bind-mounted results/ and logs/ must be writable by uid 1000,
# which is ec2-user on the host — that is why this uid and not a random one.
RUN useradd -u 1000 -m bench && chown -R bench:bench /work
USER bench

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/work

EXPOSE 9101 9102

ENTRYPOINT ["python", "-u", "-m", "generator.src.main"]
CMD ["--config", "/work/config/generator.yaml", "--profile", "smoke"]
