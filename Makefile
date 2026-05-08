REPO         := $(shell pwd)
SCRIPTS_DIR  := $(REPO)/scripts
SYSTEMD_DIR  := $(REPO)/systemd
DEPLOY_BIN   := /usr/local/bin
DEPLOY_UNITS := /etc/systemd/system

SCRIPTS := $(notdir $(wildcard $(SCRIPTS_DIR)/*.py) $(wildcard $(SCRIPTS_DIR)/*.sh))
UNITS   := $(notdir $(wildcard $(SYSTEMD_DIR)/*.service) $(wildcard $(SYSTEMD_DIR)/*.timer))

.PHONY: help install deploy deploy-scripts deploy-units sync diff check restart status

help:
	@echo "Music pipeline make targets:"
	@echo "  make install         Run ./install.sh (full deploy: scripts + units + configs)"
	@echo "  make deploy          Copy scripts and units only (no config regen, no restart)"
	@echo "  make deploy-scripts  Copy scripts/* → $(DEPLOY_BIN)"
	@echo "  make deploy-units    Copy systemd/* → $(DEPLOY_UNITS) and daemon-reload"
	@echo "  make sync            Pull deployed scripts/units back into the repo (capture drift)"
	@echo "  make diff            Show diffs between repo and deployed copies"
	@echo "  make check           Exit non-zero if repo and deployed copies disagree"
	@echo "  make restart         Restart the bot service"
	@echo "  make status          Show timer + service status"

install:
	@[ "$$(id -u)" = "0" ] || { echo "install must run as root"; exit 1; }
	./install.sh

deploy: deploy-scripts deploy-units

deploy-scripts:
	@[ "$$(id -u)" = "0" ] || { echo "deploy-scripts must run as root"; exit 1; }
	install -m 755 $(SCRIPTS_DIR)/*.py $(SCRIPTS_DIR)/*.sh $(DEPLOY_BIN)/
	@echo "Deployed $(words $(SCRIPTS)) script(s) → $(DEPLOY_BIN)"

deploy-units:
	@[ "$$(id -u)" = "0" ] || { echo "deploy-units must run as root"; exit 1; }
	install -m 644 $(SYSTEMD_DIR)/* $(DEPLOY_UNITS)/
	systemctl daemon-reload
	@echo "Deployed $(words $(UNITS)) unit(s) → $(DEPLOY_UNITS)"

# Pull deployed copies back into the repo. Useful when someone (you, me, future-you)
# edited /usr/local/bin/* directly and the repo is now stale.
sync:
	@for f in $(SCRIPTS); do \
	    if [ -f $(DEPLOY_BIN)/$$f ]; then \
	        cp -p $(DEPLOY_BIN)/$$f $(SCRIPTS_DIR)/$$f; \
	    fi; \
	done
	@for u in $(UNITS); do \
	    if [ -f $(DEPLOY_UNITS)/$$u ]; then \
	        cp -p $(DEPLOY_UNITS)/$$u $(SYSTEMD_DIR)/$$u; \
	    fi; \
	done
	@echo "Synced deployed copies back into repo. Review with: git status && git diff"

diff:
	@for f in $(SCRIPTS); do \
	    if [ -f $(DEPLOY_BIN)/$$f ]; then \
	        diff -u $(SCRIPTS_DIR)/$$f $(DEPLOY_BIN)/$$f && echo "[ok] $$f" || true; \
	    else \
	        echo "[missing in deploy] $$f"; \
	    fi; \
	done
	@for u in $(UNITS); do \
	    if [ -f $(DEPLOY_UNITS)/$$u ]; then \
	        diff -u $(SYSTEMD_DIR)/$$u $(DEPLOY_UNITS)/$$u && echo "[ok] $$u" || true; \
	    else \
	        echo "[missing in deploy] $$u"; \
	    fi; \
	done

check:
	@drift=0; \
	for f in $(SCRIPTS); do \
	    if [ ! -f $(DEPLOY_BIN)/$$f ] || ! cmp -s $(SCRIPTS_DIR)/$$f $(DEPLOY_BIN)/$$f; then \
	        echo "DRIFT: scripts/$$f"; drift=1; \
	    fi; \
	done; \
	for u in $(UNITS); do \
	    if [ ! -f $(DEPLOY_UNITS)/$$u ] || ! cmp -s $(SYSTEMD_DIR)/$$u $(DEPLOY_UNITS)/$$u; then \
	        echo "DRIFT: systemd/$$u"; drift=1; \
	    fi; \
	done; \
	[ $$drift -eq 0 ] && echo "No drift between repo and deployed copies." || exit 1

restart:
	systemctl restart slskd-telegram-bot.service

status:
	@systemctl list-timers '*pipeline*' '*beets*' '*slskd*' --no-pager 2>/dev/null || true
	@echo
	@systemctl --no-pager status slskd-telegram-bot.service 2>/dev/null | head -10 || true
