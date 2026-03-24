IMAGE := claude-code-dev:latest
PROXY_TEST_IMAGE := docker-socket-proxy-test:latest
PLATFORM ?= linux/$(shell uname -m | sed 's/x86_64/amd64/' | sed 's/aarch64/arm64/')
INSTALL_DIR_DISPLAY := ~/.local/bin
INSTALL_DIR := $(HOME)/.local/bin

.PHONY: build test install clean

build:
	docker build --platform $(PLATFORM) -t $(IMAGE) .

test:
	docker build --platform $(PLATFORM) -t $(PROXY_TEST_IMAGE) docker-socket-proxy
	docker run --rm $(PROXY_TEST_IMAGE)

install: build
	mkdir -p "$(INSTALL_DIR)"
	install -m 755 claude-container "$(INSTALL_DIR)/claude-container"
	ln -f "$(INSTALL_DIR)/claude-container" "$(INSTALL_DIR)/codex-container"
	ln -f "$(INSTALL_DIR)/claude-container" "$(INSTALL_DIR)/gemini-container"
	@echo "$(PATH)" | tr ":" "\n" | grep -Fqx -e "$(INSTALL_DIR)" -e "$(INSTALL_DIR_DISPLAY)" || { \
		echo "Add $(INSTALL_DIR_DISPLAY) to your PATH if you want to run these scripts by name."; \
	}

clean:
	docker rmi $(IMAGE) 2>/dev/null || true
	docker rmi $(PROXY_TEST_IMAGE) 2>/dev/null || true
