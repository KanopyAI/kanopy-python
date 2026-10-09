# Customer impact accompanies code

Every feature, fix, configuration or tooling PR needs a structured customer-impact
entry in `.release-notes/<slug>.json`, including an explicit assessment for internal
changes. Follow `docs/release-communication.md`; write the entry as part of the
change. Include `developer_notice` and assess behavior beyond public API schemas:
permissions, networking, storage, flags, availability, processing and rerun effects.

Stage source changes before stamping. Review the entire PR delta before refreshing
an entry. Records already on the base branch are immutable historical assessments;
append a new record for corrections or reversions. Never invent verified deployment,
publication, tests, notice dates or enabled flags.

Run feature validation and the merged-candidate preflight before requesting review.
Reconcile any concurrent main changes that produce unassessed merged file blobs.
Merging source does not establish that an SDK package has been published.

The release-note engine remains synchronized from the canonical backend copy;
run its tests and preserve this repository's release policy. Autofix controller
code and tests now live in KanopyAI/kanopy-autofix. Keep local validation policy
here, consume reviewed immutable engine pins, and use the generated consumer
contract checks. Do not copy autofix controllers or parity manifests between repos.
