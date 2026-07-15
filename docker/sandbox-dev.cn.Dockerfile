# Default sandbox machine image — CN mirror variant.
#
# Same as sandbox-dev.Dockerfile but with apt sources switched to
# mirrors.tuna.tsinghua.edu.cn (Debian 13 trixie), pip + uv configured
# to use tuna's PyPI mirror system-wide, and zh_CN.UTF-8 locale
# generated so Python and CLI tools handle CJK filenames / output
# without UnicodeDecodeError.
#
#   docker build -t sandbox-dev:latest -f docker/sandbox-dev.cn.Dockerfile .

FROM python:3.14-slim

# Switch apt sources to tuna.
RUN sed -i 's|http://deb.debian.org/debian$|http://mirrors.tuna.tsinghua.edu.cn/debian|' /etc/apt/sources.list.d/debian.sources \
    && sed -i 's|http://deb.debian.org/debian-security$|http://mirrors.tuna.tsinghua.edu.cn/debian-security|' /etc/apt/sources.list.d/debian.sources

# Install dev tools + locales; generate zh_CN.UTF-8.
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
        locales \
    && sed -i '/^#.*zh_CN.UTF-8 UTF-8$/s/^#//' /etc/locale.gen \
    && locale-gen zh_CN.UTF-8 \
    && update-locale LANG=zh_CN.UTF-8 \
    && rm -rf /var/lib/apt/lists/*

ENV LANG=zh_CN.UTF-8 \
    LC_ALL=zh_CN.UTF-8 \
    LANGUAGE=zh_CN:zh

# Pin pip + uv to tuna system-wide.
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple \
    && pip config set global.trusted-host pypi.tuna.tsinghua.edu.cn

ENV UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

RUN pip install --no-cache-dir uv

WORKDIR /workspace
CMD ["sleep", "infinity"]
