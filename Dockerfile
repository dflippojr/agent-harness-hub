# Hub image. The daemon installer (agent-harness #547) runs it as
#   docker run -d --network host --user <uid>:<gid> --mount type=bind,src=<hub dir>,dst=/hub-state <image> \
#     --daemon-url http://127.0.0.1:<port> --claim-file /hub-state/claim.json --state-dir /hub-state
# Host networking keeps the Hub on the host's loopback next to the daemon; the state directory is a bind mount
# owned by the daemon owner's account, so the Hub key never lives in the image or a volume.
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /src
COPY pyproject.toml README.md requirements.lock requirements-build.lock ./
COPY harness_hub ./harness_hub
RUN pip install --require-hashes -r requirements.lock -r requirements-build.lock \
    && pip install --no-deps --no-build-isolation . && rm -rf /src

# The installer passes --user; this is the default for a manual `docker run`.
RUN useradd --system --uid 10001 --home-dir /hub-state hub && mkdir -p /hub-state && chown hub /hub-state
USER hub
WORKDIR /hub-state

# Remote access requires an HTTPS proxy (e.g. tailscale serve) with --public-url.
# Keep the upstream port private; never publish it on every host interface with -p 8120:8120.
EXPOSE 8120
ENTRYPOINT ["python", "-m", "harness_hub"]
CMD ["--state-dir", "/hub-state"]
