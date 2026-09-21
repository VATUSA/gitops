# `apps/bootstrap-azure` — the AKS app-of-apps

The AKS (`vatusa-dev-aks`) counterpart to `apps/bootstrap`. Per
[`docs/migration-steps.md`](../../../docs/migration-steps.md) §3, AKS runs its **own**
ArgoCD; each instance manages only its own local cluster and neither is ever given
credentials for the other. Both read this same repository read-only, which cannot
conflict.

This is a **sibling chart, not a parameterisation** of `apps/bootstrap`. That chart's
templates hardcode DigitalOcean-flavoured overlay paths (`mithril/overlays/dev`,
`apps/valkey/overlays/dev`, …); syncing it against AKS would deploy the DO overlays onto
Azure. Keeping them separate means a change here cannot reach the cluster still serving
production.

## What it deploys

| Application | Source path | Notes |
|---|---|---|
| `argocd` | `apps/argocd-azure` | thin overlay on `apps/argocd`; different `url`, no Dex |
| `config` | `apps/configs-azure` | thin overlay on `apps/configs`; different ArgoCD Ingress host, no rabbitmq Ingress |
| `cert-manager` | `apps/cert-manager` | verbatim; shared with DOKS |
| `ingress-nginx` | `apps/ingress-nginx-azure` | copy of `apps/ingress-nginx` + the static-IP Service annotations |
| `valkey-dev` | `apps/valkey/overlays/dev-azure` | |
| `cobalt-dev` | `cobalt/overlays/dev-azure` | |
| `current-dev` | `current/overlays/dev-azure` | `base/api` + `base/www` only |
| `mithril-dev` | `mithril/overlays/dev-azure` | |
| `webapps-dev` | `webapps/overlays/dev-azure` | |

## What it deliberately omits, and why

This is a **subset** of the DOKS bootstrap, not a copy:

- **`metrics-server`** — AKS ships its own in `kube-system`. Syncing ours would install a
  second, competing one.
- **`metrics-reader`** — supports the DO resource-utilisation analytics work; nothing on
  AKS consumes it.
- **`fluent-bit`** — its only output is an S3 sink pointed at the DigitalOcean Spaces
  bucket `vatusa-api-logs` (sfo3). Needs a destination decision before it means anything
  on Azure; log shipping is not a prerequisite for standing dev up.
- **`external-secrets`** and **`openbao`** — §3 decided to stay on plain Kubernetes
  Secrets through the whole migration and revisit OpenBao as a post-prod improvement.
  OpenBao's state is Shamir-sealed, file-backed and local to its PVC; it does not
  replicate, so "moving" it means a from-scratch re-init either way.
- **`rabbitmq`** — exists on DOKS only for `discord-bot-v3`'s `discord_sync` queue, and
  `discord-bot-v3` is not part of the `dev-azure` overlay of `current`.
- **`schedule-message-bot`** — single-environment (prod-only) app. Stays on DOKS until
  the prod cutover.
- **`*-prod` Applications and the `zan` tenant** — Phase 1 §7–§9 and §11 respectively.

## AppProjects

Every Application here uses `project: default`. DOKS additionally defines `cobalt`,
`current`, `webapps`, `ngws` and `zan` AppProjects (with ResourceQuotas and
NetworkPolicies under `projects/`). Those exist to fence off facility tenants and
per-team RBAC on a shared production cluster — neither applies to a single-operator dev
cluster with no SSO configured. Port them when prod moves, not before.

## Bootstrapping a cluster from nothing

ArgoCD cannot install itself, so the first step is manual:

```sh
# 1. ArgoCD itself, from the same manifests it will later self-manage.
kubectl --context vatusa-dev-aks create namespace argocd
kubectl --context vatusa-dev-aks apply -k apps/argocd-azure

# 2. Hand it the root app; everything else follows from git.
kubectl --context vatusa-dev-aks apply -f apps/bootstrap-azure/Application.yaml
```

Then create the secrets that are intentionally not in this repo — see
[`docs/sitrep-20260920.md`](../../docs/sitrep-20260920.md) §4.3 for the full list.

## `targetRevision`

`values.yaml` pins `targetRevision: celeo/azure-dev` so the AKS stack can run before that
branch merges. **Set it back to `HEAD` once it does**, in both `values.yaml` and
`Application.yaml` (the root app's own source is not templated by itself).
