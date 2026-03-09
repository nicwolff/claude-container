# Stage 1: Install AWS CLI v2 (keeps unzip out of the final image)
FROM debian:bookworm-slim AS aws-builder
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl unzip \
  && set -eux \
  && case "$TARGETARCH" in \
       amd64) AWS_ARCH="x86_64" ;; \
       arm64) AWS_ARCH="aarch64" ;; \
       *) echo "Unsupported arch: $TARGETARCH" && exit 1 ;; \
     esac \
  && curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${AWS_ARCH}.zip" \
       -o /tmp/awscliv2.zip \
  && cd /tmp && unzip -q awscliv2.zip \
  && /tmp/aws/install -i /usr/local/aws-cli -b /usr/local/bin

# Stage 2: Runtime image
FROM debian:bookworm-slim

ARG TARGETARCH
ENV DEBIAN_FRONTEND=noninteractive \
    PATH="/home/dev/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
    PIP_BREAK_SYSTEM_PACKAGES=1

# Install runtime packages and Docker CLI, then remove build-only deps
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg lsb-release less sudo git \
      python3 python3-pip python3-venv \
  && mkdir -p /etc/apt/keyrings \
  && curl -fsSL https://download.docker.com/linux/debian/gpg \
       | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
  && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
      https://download.docker.com/linux/debian $(lsb_release -cs) stable" \
      > /etc/apt/sources.list.d/docker.list \
  && apt-get update && apt-get install -y --no-install-recommends \
      docker-ce-cli docker-compose-plugin \
  && apt-get purge -y --auto-remove gnupg lsb-release \
  && rm -rf /var/lib/apt/lists/*

# Copy AWS CLI from builder stage
COPY --from=aws-builder /usr/local/aws-cli /usr/local/aws-cli
COPY --from=aws-builder /usr/local/bin/aws /usr/local/bin/aws
COPY --from=aws-builder /usr/local/bin/aws_completer /usr/local/bin/aws_completer

# Non-root user
RUN useradd -ms /bin/bash dev && \
  usermod -aG sudo dev && \
  echo "dev ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers
USER dev
WORKDIR /work

# Install Claude Code as 'dev' (lands in /home/dev/.local/bin)
RUN curl -fsSL https://claude.ai/install.sh | bash && \
    /home/dev/.local/bin/claude --version

# Install MCP Python package for pdb_mcp_server (separate layer for caching)
RUN python3 -m pip install --user --no-cache-dir mcp

# Copy container plugin (subagents, MCP servers, and config)
COPY --chown=dev:dev container-plugin /home/dev/container-plugin

USER root
WORKDIR /work

# Docker socket proxy: filter privileged containers and restrict mounts
COPY docker-socket-proxy/docker_socket_proxy.py /usr/local/bin/docker-socket-proxy
RUN chmod +x /usr/local/bin/docker-socket-proxy

# Entrypoint script: launch proxy, drop root privs
COPY start-claude /usr/local/bin/start-claude
RUN chmod +x /usr/local/bin/start-claude

ENTRYPOINT ["/usr/local/bin/start-claude"]
