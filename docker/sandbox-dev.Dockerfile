# Default sandbox machine image.
#
# A capable-but-lean dev baseline so the agent can start working
# immediately on the provisioned default machine, without an extra
# docker_build step.  ripgrep is REQUIRED by sandbox_file_search; the
# rest (git, build-essential, jq, ...) are the common tools an agent
# reaches for.
#
# Base is the locally-cached debian:stable-slim so no registry pull is
# needed for the base layer (the daemon may not be able to reach Docker
# Hub directly).
#
#   docker build -t sandbox-dev:latest -f docker/sandbox-dev.Dockerfile .

FROM debian:stable-slim

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
    && rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && mv /root/.local/bin/uv /usr/local/bin/uv \
    && mv /root/.local/bin/uvx /usr/local/bin/uvx \
    && rm -rf /root/.local

WORKDIR /workspace
CMD ["sleep", "infinity"]
