# VATUSA GitOps Repository

This repository defines Kubernetes cluster state for VATUSA workloads managed through ArgoCD.

It uses a mixed approach:

- ArgoCD as the GitOps controller
- Kustomize for most application manifests
- Helm for shared infrastructure installed by ArgoCD

Application image build and publish workflows live in other repositories. This repo is the desired-state layer that ArgoCD syncs into the cluster.

## Tooling

### ArgoCD

ArgoCD is used to sync this repository into the cluster.

- `apps/bootstrap/Application.yaml` defines an app-of-apps bootstrap application
- `projects/` contains ArgoCD `AppProject` definitions

### Kustomize

Most workload manifests are plain Kubernetes YAML grouped with `kustomization.yaml` files.

Examples:

- `kustomization.yaml` points at `base/`
- `base/kustomization.yaml` assembles the legacy/base apps
- `current/kustomization.yaml` assembles the current production apps
- `overlays/dev/kustomization.yaml` defines a dev overlay with image overrides
- `cobalt/kustomization.yaml` defines a standalone app namespace and resources

Kustomize is also used to pin container image tags via the `images:` section in some app groupings.

### Helm

Helm is used for shared cluster infrastructure under `apps/`.

Current infra charts include:

- `apps/ingress-nginx/`
- `apps/cert-manager/`
- `apps/postgresql/`
- `apps/rabbitmq/`

These are local wrapper charts that depend on upstream Helm charts and are deployed by ArgoCD Applications rendered from `apps/bootstrap/`.

## Repository Layout

### `apps/`

Cluster-wide infrastructure and ArgoCD bootstrap resources.

- `bootstrap/`: ArgoCD app-of-apps chart that creates child `Application` resources
- `ingress-nginx/`: Helm chart wrapper for the ingress controller
- `cert-manager/`: Helm chart wrapper for certificate management
- `postgresql/`: Helm chart wrapper for PostgreSQL
- `rabbitmq/`: Helm chart wrapper for RabbitMQ
- `configs/`: cluster config manifests managed with Kustomize

### `base/`

Base or older app manifests managed with Kustomize. Typical app shape here is:

- `deployment.yaml`
- `service.yaml`
- `ingress.yaml`
- `certificate.yaml`

### `current/`

Current production application namespace and manifests. This is the clearest reference for how new workloads are modeled in this repo.

Examples:

- `current/www/`
- `current/api/`
- `current/forums/`
- `current/discord-bot-v3/`

### `cobalt/`

A standalone Kustomize-managed application with its own namespace and service account resources.

### `projects/`

ArgoCD `AppProject` definitions and some namespace-specific policies such as quotas and network policies.

### `overlays/`

Kustomize overlays. Currently `overlays/dev/` provides a dev environment rooted in `base/`.

## Networking, Ingress, and TLS

### Ingress

External HTTP traffic is routed through `ingress-nginx`.

Application ingresses generally use:

- `spec.ingressClassName: nginx`

Examples:

- `current/www/ingress.yaml`
- `current/api/ingress.yaml`
- `base/frontend/ingress.yaml`

### TLS

TLS certificates are managed by `cert-manager`.

- `apps/configs/base/cert-manager-ClusterIssuer.yaml` defines a cluster-wide `letsencrypt` issuer
- App directories typically include a `certificate.yaml` that creates the TLS secret used by their `Ingress`

The issuer uses a Cloudflare DNS-01 solver for ACME validation.

## DNS

This repository does not appear to manage public DNS records directly.

What it does manage:

- ingress resources for HTTP routing
- cert-manager certificate resources
- a `ClusterIssuer` that uses Cloudflare for ACME DNS validation

What is not present:

- `external-dns`
- a DNS controller for creating or updating records automatically

That means DNS records are likely managed separately from this repo. The Cloudflare integration here is for certificate validation, not for syncing ingress hostnames into DNS.

## How Deployments Flow

At a high level:

1. An application repository builds and publishes a container image through its own CI workflow.
2. This repo is updated to reference the desired image tag, usually in a Kustomize `images:` block or directly in a manifest.
3. ArgoCD detects the Git change and syncs the manifests into the cluster.
4. Kubernetes rolls out the updated workload.

## Adding a New Web App

For a typical public web application, the usual pattern in this repo is:

1. Create a `Deployment` for the pod(s)
2. Create a `Service` targeting the deployment labels
3. Create an `Ingress` for the hostname/path routing
4. Create a `Certificate` for the ingress hostname(s)
5. Add `ConfigMap` and/or `Secret` references if the app needs config
6. Add a `PersistentVolumeClaim` only if the app needs persistent storage
7. Add the new manifests to the appropriate `kustomization.yaml`
8. Add or update image tag pinning if that app grouping uses `images:`
9. Create the DNS record outside this repo unless DNS is already covered by an existing wildcard

Typical public app resources:

- `deployment.yaml`
- `service.yaml`
- `ingress.yaml`
- `certificate.yaml`

Typical optional resources:

- `configmap.yaml`
- `pvc.yaml`
- additional worker or queue deployments
- service account and RBAC resources

### Reference Patterns

Simple web app:

- `current/www/deployment.yaml`
- `current/www/service.yaml`
- `current/www/ingress.yaml`
- `current/www/certificate.yaml`

Web app with background workers:

- `current/api/deployment.yaml`
- `current/api/service.yaml`
- `current/api/ingress.yaml`
- `current/api/certificate.yaml`
- `current/api/worker.yaml`
- `current/api/deployment-queue.yaml`

App with persistent storage:

- `current/forums/pvc.yaml`

Standalone app with service account wiring:

- `cobalt/base/service-account.yaml`
- `cobalt/base/service-account-bindings.yaml`

## Notes and Assumptions

- Namespaces are sometimes managed explicitly in-app, for example `current/namespace.yaml` and `cobalt/namespace.yaml`
- Some `projects/*/allow-db.yaml` files contain network policy rules, including explicit DNS egress allowances to `kube-dns`
- Secrets referenced by workloads are not defined in this repository and are expected to exist in-cluster by some separate process

## Useful Entry Points

If you are new to this repo, start with:

- `apps/bootstrap/Application.yaml`
- `apps/bootstrap/templates/`
- `apps/configs/kustomization.yaml`
- `current/kustomization.yaml`
- `base/kustomization.yaml`
- `projects/current/Project.yaml`
