FROM debian:bookworm-slim

ARG TARGETARCH
ENV DEBIAN_FRONTEND=noninteractive \
    PATH="/home/dev/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# Install Docker CLI + Compose from Docker's repo
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg lsb-release unzip less sudo git \
  && mkdir -p /etc/apt/keyrings \
  && curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
  && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
      https://download.docker.com/linux/debian $(lsb_release -cs) stable" \
      > /etc/apt/sources.list.d/docker.list \
  && apt-get update && apt-get install -y --no-install-recommends \
      docker-ce-cli docker-compose-plugin \
  && rm -rf /var/lib/apt/lists/*

# Install AWS CLI v2 (arch-aware)
RUN set -eux; \
    case "$TARGETARCH" in \
      amd64) AWS_ARCH="x86_64" ;; \
      arm64) AWS_ARCH="aarch64" ;; \
      *) echo "Unsupported arch: $TARGETARCH" && exit 1 ;; \
    esac; \
    curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${AWS_ARCH}.zip" -o /tmp/awscliv2.zip; \
    cd /tmp && unzip -q awscliv2.zip; \
    # install to a known prefix and expose shim in /usr/local/bin
    /tmp/aws/install -i /usr/local/aws-cli -b /usr/local/bin; \
    aws --version; \
    rm -rf /tmp/aws /tmp/awscliv2.zip

# Non-root user
RUN useradd -ms /bin/bash dev && \
  usermod -aG sudo dev && \
  echo "dev ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers
USER dev
WORKDIR /work

# Install Claude Code as 'dev' (lands in /home/dev/.local/bin)
RUN curl -fsSL https://claude.ai/install.sh | bash && \
    /home/dev/.local/bin/claude --version

USER root
WORKDIR /work

# Entrypoint script: fix docker.sock bind mount perms, drop root privs
RUN cat > /usr/local/bin/start-claude <<'BASH' && chmod +x /usr/local/bin/start-claude
#!/usr/bin/env bash
set -euo pipefail

run_as_dev() {
  exec sudo -E -u dev -H env \
    HOME=/home/dev \
    PATH="/home/dev/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
    bash -lc "$*"
}

SOCK=/var/run/docker.sock
if [ -S "$SOCK" ]; then
  chown dev "$SOCK" 2>/dev/null || true
fi

# If first arg doesn't start with '-', run it as a command instead of claude
if [ $# -gt 0 ] && [[ "$1" != -* ]]; then
  run_as_dev "$@"
fi

# Otherwise, run claude with --dangerously-skip-permissions and any provided options
# Default model is 'sonnet[1m]' but can be overridden by --model in args
if [[ "$*" == *"--model"* ]]; then
  run_as_dev claude --dangerously-skip-permissions "$@"
else
  run_as_dev claude --dangerously-skip-permissions --model 'sonnet[1m]' "$@"
fi
BASH

ENTRYPOINT ["/usr/local/bin/start-claude"]
