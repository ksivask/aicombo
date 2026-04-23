# aiplay — convenience targets

.PHONY: up down logs reset check-agw up-safe rotate-keys test help

help:
	@echo "aiplay targets:"
	@echo "  up           — bring up the stack"
	@echo "  down         — tear down (keeps data/ volume)"
	@echo "  logs         — follow agentgateway + harness-api logs"
	@echo "  reset        — clear data/trials/ (irreversible)"
	@echo "  check-agw    — verify agentgateway:cidgar image exists locally"
	@echo "  up-safe      — check-agw THEN up (fails fast if image missing)"
	@echo "  rotate-keys  — restart adapters to pick up .env changes"
	@echo "  test         — run pytest suite (harness + adapters)"

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f agentgateway harness-api

reset:
	rm -f data/trials/*.json
	@echo "Cleared data/trials/"

check-agw:
	@docker image inspect agentgateway:cidgar > /dev/null 2>&1 \
		&& echo "✅ agentgateway:cidgar found" \
		|| (echo "❌ agentgateway:cidgar missing — build from agw-gh worktree first" && exit 1)

up-safe: check-agw up
	@echo "Stack up. UI at http://localhost:8000"

rotate-keys:
	@ADAPTERS=$$(docker compose ps --services 2>/dev/null | grep '^adapter-' || true); \
	if [ -z "$$ADAPTERS" ]; then \
		echo "No adapter services running"; \
	else \
		echo "Restarting adapters: $$ADAPTERS"; \
		docker compose restart $$ADAPTERS; \
	fi

test:
	cd harness && python3 -m pytest ../tests/ -xvs
