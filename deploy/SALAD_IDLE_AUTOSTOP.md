# Concat Web SaladCloud idle auto-stop (integration branch)

The owner-only GPU Config page includes an opt-in **Salad Auto Shutdown** toggle
(default OFF), with a 1–60 minute up/down input (default 5, step 1).

## VPS configuration (private /etc/concat-web/concat-web.env)
Set these three **identifiers**, matching the dedicated SaladCloud GPU group:
- `CONCAT_SALAD_ORGANIZATION=<your-organization-slug>`
- `CONCAT_SALAD_PROJECT=<your-project-slug>`
- `CONCAT_SALAD_CONTAINER_GROUP=<your-container-group-slug>`

Reuse the existing Salad API Key saved via GPU Config; do not put credentials
inside source files or terminal command history. These identifiers are not
deduced from the gateway hostname. A management token with permission to GET
and STOP this group is required.

Auto-stop is fail-closed if a required identifier, API key, endpoint or worker
token is missing, the queue has any nonterminal job, MP4 intake/Drive transfer
is active, the worker cannot confirm `active_renders=0`, Salad status is not
`running`, or Salad autostart_policy is on. Failed API calls are retried with
a minimum 30-second interval. The stop is sent only for a verified running
group over the fixed `https://api.salad.com/api/public` endpoint.
The stop response `202` is an **accepted request**, not proof the group has
already stopped; check the Salad portal for final status.

This assumes **dedicated Concat worker ownership**. Do not enable it when
other services or people submit jobs directly to the same worker: the existing
worker protocol reports active renders but does not enumerate all queued
out-of-band jobs. Auto-stop does not auto-start stopped Salad groups.

Release is manual from exact integration SHA via
`deploy/manual-integration-release.sh`; GitHub CI does not deploy.
