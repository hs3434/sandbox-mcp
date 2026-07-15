# Default sandbox machine image — CN mirror variant.
#
# Same as sandbox-dev.Dockerfile but with apt sources switched to
# mirrors.tuna.tsinghua.edu.cn and UV_INDEX_URL set to tuna's PyPI
# mirror, so the docker build succeeds without a global proxy when
# deb.debian.org / pypi.org are unreachable from the build host.
#
#   docker build -t sandbox-dev:latest -f docker/sandbox-dev.cn.Dockerfile .

FROM debian:stable-slim

RUN sed -i 's|http://deb.debian.org/debian$|http://mirrors.tuna.tsinghua.edu.cn/debian|' /etc/apt/sources.list.d/debian.sources \
    && sed -i 's|http://deb.debian.org/debian-security$|http://mirrors.tuna.tsinghua.edu.cn/debian-security|' /etc/apt/sources.list.d/debian.sources

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

ENV UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /workspace
CMD ["sleep", "infinity"]
