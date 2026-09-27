.PHONY: doctor validate test install update status list uninstall review
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
