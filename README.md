# Claude Code Dev Container

A portable Docker-based development environment for running [Claude Code](https://claude.ai/claude-code) with AWS Bedrock support. This container provides a consistent, isolated environment with Claude Code CLI, AWS CLI, and Docker tooling pre-installed.

## Features

- **Claude Code CLI** - Latest version installed and ready to use
- **OpenAI Codex CLI** - Run `codex-container` to use Codex instead
- **Google Gemini CLI** - Run `gemini-container` to use Gemini instead
- **AWS CLI v2** - For AWS Bedrock integration and other AWS operations
- **Docker + Docker Compose** - Filtered Docker access via socket proxy
- **GitHub CLI (`gh`)** - Create pull requests, manage issues, and interact with GitHub
- **Python 3 + MCP** - Python debugging support with pre-configured Pdb MCP server
- **Non-root execution** - Runs as unprivileged `dev` user for security
- **Volume mounting** - Seamlessly access your host repositories
- **Configuration persistence** - Your Claude, Codex, Gemini, and AWS configs are mounted from host

## Prerequisites

- Docker installed on your host machine
- (Optional) AWS credentials configured in `~/.aws/` for Bedrock usage
- (Optional) Claude Code configuration in `~/.claude/` and `~/.claude.json`
- (Optional) GitHub CLI authenticated via `gh auth login` or `CLAUDE_GITHUB_TOKEN` env var for PR/issue workflows

## Quick Start

Clone or download this repo, `cd` into it, and run `make install`. This installs `claude-container`, `codex-container`, and `gemini-container` as hard links to the same script, which auto-selects the right CLI.

## Usage

### Basic usage

```bash
# Launch Claude Code in current directory
claude-container

# Pass additional arguments to Claude
claude-container --model opus

# Launch Codex CLI instead (--yolo is added automatically)
codex-container

# Launch Gemini CLI instead (--yolo is added automatically)
gemini-container

# Pass CLI flags straight through to the selected tool
codex-container --help
gemini-container --help

# Run arbitrary commands in the container with any launcher
codex-container bash
gemini-container bash

# Show launcher help instead of CLI help
claude-container --container-help

# Run arbitrary commands in the container
claude-container bash
claude-container aws s3 ls
```

### Authentication

The launcher script mounts your config and auth token directories, so for Claude or Gemini you can
just SSO before starting the container.

For Codex, the script will export OPENAI_API_KEY from your environment, or from your macOS Keychain.

### Environment Variables

The launcher script passes through these environment variables from your host if set:

- **`AWS_PROFILE`** - AWS profile to use (from `~/.aws/config`)
- **`AWS_REGION`** - AWS region for API calls
- **`CLAUDE_CODE_USE_BEDROCK`** - Set to `1` to use AWS Bedrock instead of Anthropic API
- **`CLAUDE_GITHUB_TOKEN`** - GitHub personal access token for `gh` CLI (passed as `GITHUB_TOKEN` inside the container)
- **`GOOGLE_CLOUD_PROJECT`** - Google Cloud project ID for Gemini CLI Vertex usage
- **`GOOGLE_CLOUD_LOCATION`** - Google Cloud region for Gemini CLI Vertex usage
- **`OPENAI_API_KEY`** - OpenAI API key for Codex CLI (falls back to macOS Keychain if not set)

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
| `~/.gitconfig` | `/home/dev/.gitconfig` | Git user identity and settings |
| `~/.claude` | `/home/dev/.claude` | Claude Code settings |
| `~/.claude.json` | `/home/dev/.claude.json` | Claude API key config |
| `~/.codex` | `/home/dev/.codex` | Codex CLI settings when using `codex-container` |
| `~/.gemini` | `/home/dev/.gemini` | Gemini CLI settings when using `gemini-container` |
| `~/.config/gcloud` | `/home/dev/.config/gcloud` | Google Cloud auth/config when using `gemini-container` |
| `~/.config/gh` | `/home/dev/.config/gh` | GitHub CLI auth tokens |
| `/var/run/docker.sock` | `/var/run/docker-real.sock` | Docker daemon (behind proxy) |

## Container Details

- **Base Image**: `debian:bookworm-slim`
- **User**: `dev` (non-root with sudo access)
- **Working Directory**: Mirrors your host's current directory
- **Default Command**: `claude --dangerously-skip-permissions --model us.anthropic.claude-opus-4-6-v1`

## Docker Socket Proxy

The container includes a security proxy that sits between Claude and the Docker daemon. Instead of giving Claude unrestricted Docker access, the proxy intercepts Docker API requests and enforces the following restrictions:

### What's Blocked

- **Privileged containers** (`--privileged`)
- **Dangerous capabilities** (`SYS_ADMIN`, `SYS_PTRACE`, `NET_ADMIN`, `SYS_RAWIO`, `SYS_MODULE`, `DAC_READ_SEARCH`, `ALL`)
- **Host PID namespace** (`--pid=host`)
- **Host network mode** (`--network=host`)

### Mount Restrictions

Bind mounts are restricted based on your working directory. Given a working directory of `/home/user/projects/my-app`:

| Path | Mode | Result |
|------|------|--------|
| `/home/user/projects/my-app/src` | rw | Allowed |
| `/home/user/projects/sibling` | ro | Allowed |
| `/home/user/projects/sibling` | rw | **Rejected** |
| `/etc/shadow` | any | **Rejected** |

The proxy passes `ALLOWED_MOUNT_BASE` (parent of cwd) and `ALLOWED_RW_BASE` (cwd) to control these restrictions.

### How It Works

1. The host Docker socket is mounted as `/var/run/docker-real.sock` (not the usual path)
2. On container startup, the entrypoint locks down the real socket (`chmod 700`) so only root can access it
3. A Python proxy starts in the background, listening on `/var/run/docker.sock`
4. The `dev` user's Docker CLI talks to the proxy, which validates and forwards requests to the real daemon

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

### GitHub CLI not authenticated

There are two ways to authenticate `gh` inside the container:

**Option 1: Interactive login (recommended for personal use)**

```bash
# On your host machine
gh auth login
```

This stores credentials in `~/.config/gh/` which the container mounts automatically.

**Option 2: `CLAUDE_GITHUB_TOKEN` environment variable**

```bash
export CLAUDE_GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx
claude-container
```

Required token scopes:

- **Classic PAT**: `repo` scope
- **Fine-grained PAT**: "Contents: Read and write" + "Pull requests: Read and write" on target repos

See GitHub docs for creating tokens: [Managing your personal access tokens](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)

For more on the GitHub CLI: [About GitHub CLI](https://docs.github.com/en/github-cli/github-cli/about-github-cli)

### Codex CLI not authenticating

`codex-container` pulls `OPENAI_API_KEY` from the environment first, then falls back to the macOS Keychain. To store the key in your Keychain:

```bash
security add-generic-password -s OPENAI_API_KEY -a openai -w 'sk-...'
```

Or export it directly:

```bash
export OPENAI_API_KEY=sk-...
codex-container
```

### Gemini CLI not authenticating

`gemini-container` mounts `~/.gemini` and `~/.config/gcloud` into the container so existing Gemini CLI and Google Cloud authentication can be reused there. For Vertex, it also passes through `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION` when they are set on the host.

### Claude Code not authenticating

Run Claude Code once on your host to set up authentication, or manually place your API key in `~/.claude.json`:

```json
{
  "apiKey": "your-api-key-here"
}
```

## Container Plugin

The container includes a built-in plugin that provides additional subagents and MCP servers. This plugin **layers on top of** any configuration you have in your mounted `~/.claude/` directory without modifying it.

### How It Works

The container uses Claude Code's `--plugin-dir` flag to load `/home/dev/container-plugin`, which contains:

```
container-plugin/
├── .claude-plugin/
│   └── plugin.json           # Plugin manifest
├── pdb_mcp_server.py         # Pdb MCP server (loaded only when python-test-debugger is used)
└── agents/
    └── python-test-debugger.md  # Subagent definitions (includes inline MCP server config)
```

**Benefits of the plugin approach:**

- **Non-invasive** - Doesn't modify your `~/.claude/settings.json`
- **Layered** - Container tools add to (not replace) your existing config
- **Priority** - Your user-level agents take precedence over plugin agents if names conflict

### Included Subagents

#### python-test-debugger

A specialized agent for debugging failing Python tests interactively. Use it when:

- VCR/mocking doesn't seem to be working
- Tests fail after dependency updates
- Async/event loop errors occur
- Tests behave differently when run individually vs. in a suite

The agent has access to the `pdb` MCP server for interactive debugging.

#### memory-bank-analyzer

An agent that extracts project context from `memory_bank/` files before starting implementation work. Claude uses this agent **proactively** after you define a task to:

- Extract coding standards and style guidelines
- Identify architectural patterns and conventions
- Find project structure and organization principles
- Understand testing approaches and requirements

This ensures implementations follow your project's established patterns and standards.

### Included MCP Servers

#### pdb (Python Debugger)

`pdb_mcp_server.py` is embedded in the `container-plugin/` directory and is only started when the `python-test-debugger` skill is invoked — it does **not** run during normal Claude sessions. It provides:

- **`start_pdb_session`** - Start a Pdb session in a Docker Compose service
- **`send_pdb_command`** - Send commands (e.g., `n`, `s`, `p var`)
- **`stop_pdb_session`** - Stop the debugging session

### Adding Your Own Container Extensions

To add more subagents or MCP servers to the container:

1. Add subagent markdown files to `container-plugin/agents/`
2. To include an MCP server that loads only when a specific agent is used, place the server script in `container-plugin/` and define it inline in the agent's frontmatter:
   ```yaml
   mcpServers:
     my-server:
       command: python3
       args: ["/home/dev/container-plugin/my_server.py"]
   ```
3. Rebuild the container

Example subagent (`container-plugin/agents/my-agent.md`):

```markdown
---
name: my-agent
description: Description of when Claude should use this agent
tools: Read, Grep, Glob, Bash
model: inherit
---

You are a specialist in...
```
