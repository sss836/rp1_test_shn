FROM python:3.12-slim@sha256:ddb0207ae1f0356c2b724d740769b0c5f5f51cc54a0525178f721825f78fe74c
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends openssl \
    && rm -rf /var/lib/apt/lists/* \
    && openssl version >/dev/null && dpkg --version >/dev/null && dpkg-deb --version >/dev/null
# Debian slim supplies dpkg/dpkg-deb; the assertions above fail the build if
# that base-image contract changes. Software distribution stays disabled.
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.txt -c requirements.lock && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin rp1 \
    && mkdir -p /var/lib/rp1/archives /var/lib/rp1/hmi-updates \
    && chown 10001:10001 /var/lib/rp1/archives /var/lib/rp1/hmi-updates \
    && chmod 0750 /var/lib/rp1/hmi-updates
COPY app ./app
COPY scripts ./scripts
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
