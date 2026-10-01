.PHONY: doctor validate test install update status list uninstall review review-apply
doctor:
	./scripts/doctor.sh
validate:
	./scripts/validate.sh
test:
	python3 -m unittest discover -s tests -v
install:
	./scripts/install.sh
update:
	./scripts/update.sh
status:
	./scripts/status.sh
list:
	./scripts/list.sh
uninstall:
	./scripts/uninstall.sh --yes
review:
	./scripts/run.sh github-pr-review --repository "$(REPO)" $(if $(PR),--pr "$(PR)",)
review-apply:
	./scripts/run.sh github-pr-review --apply --repository "$(REPO)" --pr "$(PR)" --policy "$(POLICY)" --apply-mode "$(or $(APPLY_MODE),patch)"
