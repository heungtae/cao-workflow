# Adding a workflow

1. Create `workflows/<name>/workflow.py` as a CAO script-tier file with static literal `INPUTS`, `get_inputs()`, stable step IDs, and `emit_output()`.
2. Add only required `agents/<name>.md` profiles. Use installed CAO schema keys; write responsibility, forbidden actions, untrusted input policy, and an exact output contract. Avoid a fixed model unless operationally required.
3. Add one workflow entry and agent entries to `manifest.json`. The manifest is the only management inventory.
4. Add configuration examples and targeted fixtures/tests for filtering, deduplication, aggregation, and safe removal where applicable.
5. Run `make validate`, `make test`, and an isolated `CAO_HOME_DIR=/tmp/... ./scripts/install.sh` cycle. With `cao-server`, also run `cao workflow validate` on the deployed file.
6. Update README and operations docs. Change the workflow version when profile or gate changes alter review semantics; this deliberately invalidates prior GitHub markers for the same HEAD.

Use `cao --version` and command help before relying on a CAO feature. CAO 2.5.0 has no workflow create/update command. Do not add YAML and Python copies of the same workflow. External triggers should invoke `scripts/run.sh` and remain separate from workflow logic.
