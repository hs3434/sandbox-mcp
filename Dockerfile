# docker build -t sandbox-mcp .
 
FROM python:3.12-slim

# openssh-client: required by the SSH backend (subprocess ssh/scp calls)
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/*

ARG PIP_EXTRA_ARGS=""
WORKDIR /app
COPY pyproject.toml LICENSE ./
COPY src/ ./src/
RUN pip install --no-cache-dir ${PIP_EXTRA_ARGS} .

# HOME must match the config volume mount target in docker-compose.yml.
# sandbox-mcp resolves config via Path.home() / ".sandbox-mcp".
ENV HOME=/home/sandbox
RUN mkdir -p /home/sandbox/.sandbox-mcp

EXPOSE 8010

ENTRYPOINT ["sandbox-mcp-http"]
CMD ["--host", "0.0.0.0", "--port", "8010"]
