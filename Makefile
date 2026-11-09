# Shortcuts for the restaurant watcher. Run `make help` (or `mingw32-make help` on Windows).
# Every target calls the same `jap r ...` commands; the CLI and the UI share the watch
# logic and the log (.state/watch.log).

JAP  = uv run --no-sync jap r
PORT ?= 8501

.PHONY: help sync list check watch start stop restart status log digest test-email ui test

help:
	@echo make sync        install everything incl. the UI - uv sync --extra ui
	@echo make list        show restaurants, windows and who gets emailed
	@echo make check       check once, no email
	@echo make watch       check repeatedly in this terminal
	@echo make start       start the watcher in the background, with email
	@echo make stop        stop the background watcher
	@echo make restart     stop, then start
	@echo make status      running? + the end of the log
	@echo make digest      check now and email everyone the full list of open slots
	@echo make test-email  send a short test email to everyone
	@echo make ui          the UI on port $(PORT), reachable from your whole network
	@echo make test        run the tests

sync:
	uv sync --extra ui

list:
	$(JAP) list

check:
	$(JAP) check

watch:
	$(JAP) watch

start:
	$(JAP) start

stop:
	$(JAP) stop

restart: stop start

status:
	$(JAP) status

log: status

digest:
	$(JAP) digest

test-email:
	$(JAP) test-email

# always broadcast: listen on every network interface, not just this PC
ui:
	$(JAP) ui --host 0.0.0.0 --port $(PORT)

test:
	uv run --no-sync pytest -q
