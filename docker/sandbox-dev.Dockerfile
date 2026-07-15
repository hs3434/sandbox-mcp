# Default sandbox machine image.
#
# A capable-but-lean dev baseline so the agent can start working
# immediately on the provisioned default machine, without an extra
# docker_build step.  ripgrep is REQUIRED by sandbox_file_search; the
# rest are the common tools an agent reaches for: shell/networking
# utilities (procps/iproute2/dnsutils), archive handling (unzip/xz),
# security (gnupg), and sync (rsync).
#
# Base is python:3.14-slim (Debian 13 trixie) so the agent gets Python
# 3.14 + pip out of the box; uv is installed via pip and is therefore
# decoupled from astral.sh availability at build time.  Build paths
# reduce to: docker registry pull (covered by daemon proxies) + PyPI
# pull for uv.  No more `astral.sh` round-trip.
#
#   docker build -t sandbox-dev:latest -f docker/sandbox-dev.Dockerfile .

FROM python:3.14-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        ripgrep \
        build-essential \
        jq \
        less \
        file \
        openssh-client \
        tree \
        vim-tiny \
        procps \
        unzip \
        xz-utils \
        iproute2 \
        dnsutils \
        gnupg \
        rsync \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /workspace
CMD ["sleep", "infinity"]
