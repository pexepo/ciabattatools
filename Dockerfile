# Ciabatta Tools -- one image running both the bot and the mini-app API.
#
# A single container, because BotHost provides one container and one port. The bot
# polls Telegram, the API serves the mini-app, and both live in this process tree.
#
# python:3.12-slim rather than alpine: curl_cffi ships manylinux wheels but no
# musl wheels, so alpine would try to build it from source against a libcurl it
# does not have. curl_cffi is not optional here -- MRKT fingerprints the TLS
# handshake and drops clients that do not look like a browser.

FROM python:3.12-slim

# Unbuffered output so logs reach the host as they happen. Without it a crash loop
# shows an empty log, because the buffer dies with the process.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# ca-certificates is required: every outbound call is HTTPS, and without a trust
# store the failure looks like a network error rather than a missing cert.
# curl is for the healthcheck below.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# Requirements copied on their own, ahead of the source: this layer then stays
# cached, so editing code does not reinstall the dependency tree every build.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY webapp/ ./webapp/
COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh

# Sessions and the SQLite fallback live here. Declared so a host that honours
# VOLUME keeps it; the deploy guide still says to mount it explicitly, because an
# unmounted volume is discarded on redeploy and every user gets logged out.
RUN mkdir -p /app/data
VOLUME ["/app/data"]

# Non-root runtime. The entrypoint starts as root only long enough to make the
# data volume writable by this user, then drops privileges via setpriv before
# launching the processes; see entrypoint.sh. A stolen MTProto session is worth
# more than most container escapes.
RUN useradd --create-home --uid 10001 ciabatta \
 && chown -R ciabatta:ciabatta /app

# BotHost injects its own PORT; this is the fallback for a plain docker run.
ENV PORT=8080
EXPOSE 8080

# Hits the API's liveness endpoint, which deliberately does not touch the
# database: restarting because Postgres blipped turns a stall into an outage.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/healthz" || exit 1

ENTRYPOINT ["./entrypoint.sh"]
