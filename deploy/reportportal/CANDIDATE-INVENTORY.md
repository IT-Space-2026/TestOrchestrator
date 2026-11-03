# ReportPortal candidate inventory — SELECTION ONLY

Source: `github.com/reportportal/reportportal` release **26.0.7**
(published 2026-10-27), `docker-compose.yml` fetched verbatim to
`candidate-docker-compose.yml`. This is a *candidate* selection reviewed
from upstream deployment material — **no image digests verified, no
images acquired, nothing deployed**. Bundling (docker save archives)
and live deployment remain open environment-gated steps.

## Components (upstream pinned tags)

| Service | Image:tag | Role in stack |
|---|---|---|
| gateway | traefik:v2.11.60 | edge router to UI/API/UAT |
| postgres | postgres:18.6 | metadata DB (auth, launches, items) |
| rabbitmq | rabbitmq:4.4.0-management | analyzer/log queues |
| opensearch | opensearchproject/opensearch:3.9.0 | log indexing |
| migrations | reportportal/migrations:5.15.6 | schema init (runs once) |
| index | reportportal/service-index:5.15.2 | internal service routing |
| ui | reportportal/service-ui:5.15.6 | web UI |
| api | reportportal/service-api:5.15.6 | REST API (portal publishes here) |
| uat | reportportal/service-authorization:5.15.2 | auth/SSO (API tokens issued here) |
| jobs | reportportal/service-jobs:5.15.3 | cleanup/plugin jobs |
| analyzer | reportportal/service-auto-analyzer:5.15.6 | log analyzer (optional for A01) |
| busybox | busybox:1.38.0 | volume bootstrap helper |

## Required minimal set for publication vs optional

Publication path needs: postgres, rabbitmq, opensearch, migrations,
index, ui, api, uat (+gateway). Analyzer is optional — A01 evidence
does not depend on auto-analysis.

## Honest status

- Candidate selection: DONE (this file + compose at tag 26.0.7).
- Verified digests/architecture availability: NOT DONE — needs
  `docker pull`/`skopeo` or registry API on the build host.
- Acquired image archives (offline `docker save` set): NOT DONE.
- Live deployment/integration validation: NOT DONE — gate stays open.
- Licence: ReportPortal components are Apache-2.0; traefik MIT;
  postgres PostgreSQL License; rabbitmq MPL-2.0; opensearch Apache-2.0 —
  notices to be vendored during bundling, not yet collected.
- Host requirement (from upstream compose): Docker + compose plugin,
  ~8 GB RAM recommended by upstream docs — NOT a validated product
  minimum, unverified until observed on the clean VM.
