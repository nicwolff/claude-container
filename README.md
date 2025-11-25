# Claude Code Dev Container

A portable Docker-based development environment for running [Claude Code](https://claude.ai/claude-code) with AWS Bedrock support. This container provides a consistent, isolated environment with Claude Code CLI, AWS CLI, and Docker tooling pre-installed.

## Features

- **Claude Code CLI** - Latest version installed and ready to use
- **AWS CLI v2** - For AWS Bedrock integration and other AWS operations
- **Docker + Docker Compose** - Full Docker support via socket mounting
- **Python 3 + MCP** - Python debugging support with pre-configured Pdb MCP server
- **Non-root execution** - Runs as unprivileged `dev` user for security
- **Volume mounting** - Seamlessly access your host repositories
- **Configuration persistence** - Your Claude and AWS configs are mounted from host

## Prerequisites

- Docker installed on your host machine
- (Optional) AWS credentials configured in `~/.aws/` for Bedrock usage
- (Optional) Claude Code configuration in `~/.claude/` and `~/.claude.json`

## Quick Start

### 1. Build the container

```bash
docker build --platform linux/amd64 -t claude-code-dev:latest .
# or for ARM64 (Apple Silicon, ARM servers):
docker build --platform linux/arm64 -t claude-code-dev:latest .
```

### 2. Install the launcher script

```bash
# Make the script executable
chmod +x claude-container

# Move it to your PATH (optional but recommended)
sudo mv claude-container /usr/local/bin/
# or
mkdir -p ~/bin && mv claude-container ~/bin/
```

### 3. Run from any repository

```bash
cd /path/to/your/project
claude-container
```

The container will launch with Claude Code ready to assist with your project!

## Usage

### Basic usage

```bash
# Launch Claude Code in current directory
claude-container

# Pass additional arguments to Claude
claude-container --model opus

# Run arbitrary commands in the container
claude-container bash
claude-container aws s3 ls
```

### Environment Variables

The launcher script passes through these environment variables from your host if set:

- **`AWS_PROFILE`** - AWS profile to use (from `~/.aws/config`)
- **`AWS_REGION`** - AWS region for API calls
- **`CLAUDE_CODE_USE_BEDROCK`** - Set to `1` to use AWS Bedrock instead of Anthropic API

Example:

```bash
# Set environment variables for your session
export AWS_PROFILE=myprofile
export AWS_REGION=us-west-2
export CLAUDE_CODE_USE_BEDROCK=1

# Launch container (will inherit the above settings)
claude-container
```

Or as a one-liner:

```bash
AWS_PROFILE=prod AWS_REGION=us-east-1 CLAUDE_CODE_USE_BEDROCK=1 claude-container
```

## What Gets Mounted

The `claude-container` script automatically mounts:

| Host Path | Container Path | Purpose |
|-----------|---------------|---------|
| Current directory | Same path in container | Your working repository |
| `~/.aws` | `/home/dev/.aws` | AWS credentials and config |
| `~/.claude` | `/home/dev/.claude` | Claude Code settings |
| `~/.claude.json` | `/home/dev/.claude.json` | Claude API key config |
| `/var/run/docker.sock` | `/var/run/docker.sock` | Docker daemon access |

## Container Details

- **Base Image**: `debian:bookworm-slim`
- **User**: `dev` (non-root with sudo access)
- **Working Directory**: Mirrors your host's current directory
- **Default Command**: `claude --dangerously-skip-permissions --model 'sonnet[1m]'`

## Troubleshooting

### Docker socket permissions

If you encounter Docker permission errors, ensure your user is in the `docker` group on the host:

```bash
sudo usermod -aG docker $USER
# Log out and back in for changes to take effect
```

### AWS credentials not found

Ensure your `~/.aws` directory exists and contains valid credentials:

```bash
aws configure
# or
aws configure --profile myprofile
```

### Claude Code not authenticating

Run Claude Code once on your host to set up authentication, or manually place your API key in `~/.claude.json`:

```json
{
  "apiKey": "your-api-key-here"
}
```

## MCP Server (Python Debugger)

The container includes a pre-configured MCP server for Python debugging with Pdb in Docker containers. The server provides three tools:

- **`start_pdb_session`** - Start a Pdb debugging session in a Docker Compose service
- **`send_pdb_command`** - Send commands to the active Pdb session (e.g., `n`, `s`, `p var`)
- **`stop_pdb_session`** - Stop the active debugging session

### Automatic Configuration

On startup, the container automatically registers the pdb MCP server using `claude mcp add`:

- The server is only registered if it's not already present
- It's registered globally for Claude Code CLI, so you can use it across all your projects
- The configuration persists in your `~/.claude/settings.json` file (which is mounted from your host)

This means you can use the container's debugging capabilities alongside any other MCP servers you have configured.
