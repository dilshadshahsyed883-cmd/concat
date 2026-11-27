# Concat Web SaladCloud idle auto-stop (canonical main)

The previous opt-in **Salad Auto Shutdown** API fields remain compatible.
GPU Config now presents [Salad Worker Automation](SALAD_AUTO_WORKER.md):
Automatic mode includes a fixed 60-second idle stop, and saving Manual mode
disables legacy automatic shutdown. Legacy saved timeouts are retained.

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
out-of-band jobs. For automatic startup plus fixed 60-second shutdown, use the opt-in
[Salad Auto Worker](SALAD_AUTO_WORKER.md). It uses the same dedicated group and
existing credentials; no GPU image rebuild is required.

Release is manual from an exact reviewed main SHA via
`deploy/manual-integration-release.sh`; GitHub CI does not deploy.
