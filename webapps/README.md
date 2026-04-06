# webapps

This directory defines the Kubernetes resources for the `webapps` site.

It mirrors the standalone-app pattern used by `cobalt`:

- dedicated namespace
- standalone `kustomization.yaml`
- direct Kubernetes manifests for the workload
- image tag pinning through Kustomize `images:`

## Resources

- `namespace.yaml`: creates the `webapps` namespace
- `kustomization.yaml`: assembles the app resources and pins the image tag
- `base/deployment.yaml`: the Next.js web container
- `base/service.yaml`: internal ClusterIP service
- `base/ingress.yaml`: public ingress for `next.vatusa.net`
- `base/certificate.yaml`: cert-manager certificate for `next.vatusa.net`
- `base/configmap.yaml`: non-secret runtime environment values

## Runtime Assumptions

- Container image: `vatusa/webapps`
- Container port: `3000`
- Public hostname: `next.vatusa.net`
- Health endpoint: `/health`
- Secret name: `webapps-secrets`
- ConfigMap name: `webapps-config`

Secrets are expected to be created outside this repository, following the same pattern used by other apps in this repo.

## Deployment Notes

- The `Service` exposes port `80` and forwards to container port `3000`
- The `Ingress` uses the `nginx` ingress class
- TLS is provided by `cert-manager` using the cluster-wide `letsencrypt` issuer
- The image tag should be updated in `kustomization.yaml` by the application delivery workflow

## Updating the App

Typical update flow:

1. Build and publish a new `vatusa/webapps` container image from the app repository
2. Update the `images:` tag in `kustomization.yaml`
3. Let ArgoCD sync the change into the cluster

## Configuration

Non-secret environment variables should go in `base/configmap.yaml`.

Secret environment variables should be placed in the externally managed `webapps-secrets` secret.
