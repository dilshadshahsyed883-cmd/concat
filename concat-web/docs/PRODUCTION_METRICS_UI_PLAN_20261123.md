# Concat performance report

The final authorized scope is report-only, implemented entirely in `concat-web`.
GPU Config contains a time-range selector and Download Report (.txt). The
Production page and its API payload are unchanged. No resource popup, live log
viewer, worker change, Docker build, merge or deployment is included.

Background collection persists resource readings, job state transitions,
simultaneous activity, available worker performance summaries and image-pull
reallocation decisions. The TXT report is the only new user-facing output.

See `deploy/PERFORMANCE_REPORT.md` for data sources, retention, interpretation
and verification. The earlier ideas for per-job live metrics and a Resource
Usage popup are deferred outside this implementation.
