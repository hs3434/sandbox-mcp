FROM python:3.12-slim

# openssh-client: required by the SSH backend (subprocess ssh/scp calls)
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml LICENSE ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

# Config and workspace directories
RUN mkdir -p /home/sandbox/.sandbox-mcp /var/lib/sandbox-mcp/workspaces

EXPOSE 8010

ENTRYPOINT ["sandbox-mcp-http"]
CMD ["--host", "0.0.0.0", "--port", "8010"]
