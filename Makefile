IMAGE := claude-code-dev:latest
PROXY_TEST_IMAGE := docker-socket-proxy-test:latest
PLATFORM ?= linux/$(shell uname -m | sed 's/x86_64/amd64/' | sed 's/aarch64/arm64/')

.PHONY: build test install clean

build:
	docker build --platform $(PLATFORM) -t $(IMAGE) .

test:
	docker build --platform $(PLATFORM) -t $(PROXY_TEST_IMAGE) docker-socket-proxy
	docker run --rm $(PROXY_TEST_IMAGE)

install: build
	install -m 755 claude-container /usr/local/bin/claude-container
	ln -f /usr/local/bin/claude-container /usr/local/bin/codex-container
	ln -f /usr/local/bin/claude-container /usr/local/bin/gemini-container

clean:
	docker rmi $(IMAGE) 2>/dev/null || true
	docker rmi $(PROXY_TEST_IMAGE) 2>/dev/null || true
