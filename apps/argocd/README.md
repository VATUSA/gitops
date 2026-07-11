# ArgoCD, self-managed

This directory makes ArgoCD **manage its own install**. The control plane is a
Kustomize app whose base is the official upstream `install.yaml`, pinned to an exact
version. The `argocd` child Application (`apps/bootstrap/templates/argocd.yaml`) syncs
this directory like any other app, so ArgoCD reconciles itself.

Before this existed, ArgoCD was installed once by a manual `kubectl apply` at v2.10.6
and left to rot — invisible to Git, unreviewable, four minors + a major behind and past
EOL. It is now codified and current (v3.4.5), and upgrading is a one-line change.

## Files

| File | What it is |
|---|---|
| `kustomization.yaml` | Remote `install.yaml` base at the pinned version + the patches below |
| `argocd-cm.yaml` | Our live `argocd-cm` values, merged onto the upstream (empty) one |
| `argocd-cmd-params-cm.yaml` | Our live `argocd-cmd-params-cm` values (currently defaults) |

`argocd-rbac-cm`, the argocd `Ingress`, and the namespace-creator RBAC are **not** here —
they stay owned by `apps/configs/base/`. `argocd-secret` is owned by nobody (see below).

## To upgrade

1. Find the target version. Latest release: <https://github.com/argoproj/argo-cd/releases>.
   Prefer the newest **patch** of the target minor (e.g. `v3.4.5`, never `.0`).
2. Skim the upgrade notes for **every minor you cross**:
   <https://github.com/argoproj/argo-cd/tree/master/docs/operator-manual/upgrading>.
   Reconcile any breaking change by pinning the old behavior in `argocd-cm.yaml` in the
   **same PR** (see the two pins already there), so a new default never surprises a sync.
3. Bump the single version tag in `kustomization.yaml` (the `resources:` URL).
4. Review the rendered diff before syncing:
   ```bash
   kubectl kustomize apps/argocd | kubectl diff --server-side --force-conflicts -f -
   ```
   Expected noise you can ignore: `last-applied-configuration`, empty `annotations: {}`,
   and `app.kubernetes.io/instance` label churn — client-apply artifacts ArgoCD doesn't
   use. Anything touching `argocd-secret`, `argocd-rbac-cm`, or the *data* of
   `argocd-cm` / `argocd-cmd-params-cm` means stop and reconcile the patch.
5. Open a PR to `main`. The `argocd` app syncs it (selfHeal + prune are on).
6. **Verify** (below).

### Cross a MAJOR boundary deliberately

For a major step (e.g. 2.x → 3.x) do the extra work the minor bumps don't need:

- **Back up off-cluster first:** `argocd admin export -n argocd > argocd-export.yaml`,
  plus the raw `argocd-secret` and all `applications,appprojects,applicationsets`.
- **Snapshot stateful downstream volumes** (DigitalOcean Block Storage → the `pvc-*`
  volumes). A prune deletes workloads, not PVCs, but Git can't restore a database.
- **Temporarily set `syncPolicy.automated.prune: false`** on the `argocd` app
  (`apps/bootstrap/templates/argocd.yaml`) for that one sync, so a surprised diff can't
  cascade a prune while you watch. Restore `prune: true` once it's Healthy.

## Invariants — do not break these

- **NEVER manage `argocd-secret`.** It is stateful/runtime-owned: admin password,
  `server.secretkey` (session/JWT signing key — losing it invalidates every session and
  token), the Dex GitHub OAuth client credentials, and the `argocd.vatusa.dev` TLS
  cert+key. It is `$patch: delete`'d from the render so ArgoCD neither overwrites nor
  prunes the live Secret. This is what makes SSO and logins survive every upgrade and
  rollback.
- **NEVER app-wide Server-Side Apply.** SSA is set per-resource via the
  `argocd.argoproj.io/sync-options: ServerSideApply=true` annotation on the three
  argoproj.io CRDs only (the `applicationsets` CRD's last-applied annotation is ~220KB
  and crosses the 262144-byte client-side-apply limit in 3.x). An app-wide `syncOption`
  caused phantom OutOfSync flapping here before — see git log.
- **`argocd-rbac-cm` stays owned by `apps/configs`.** The upstream manifest ships an
  empty copy; it's `$patch: delete`'d here to avoid dual ownership / OutOfSync flapping
  between the two Applications.
- **No `resources-finalizer` on the `argocd` child Application.** Deleting the app
  orphans (leaves running) the install instead of cascade-pruning the control plane.

## Verify after every synced step

```bash
kubectl -n argocd get pods                      # all Running/Ready, 0 restarts
kubectl -n argocd get deploy argocd-server \
  -o jsonpath='{.spec.template.spec.containers[0].image}'   # == target tag
```

- <https://argocd.vatusa.dev> loads, and **GitHub SSO login works** (proves
  `argocd-secret` untouched and the RBAC subject mapping intact).
- `argocd app list` → all other child apps still Synced/Healthy (proves the sync didn't
  orphan or prune anything downstream).
- Smoke-test a downstream site (e.g. `www.vatusa.net`, `cobalt.vatusa.net`) — an ArgoCD
  upgrade must not touch them.
- After a major: confirm a facility (non-admin) user can still act on their apps'
  sub-resources — offline eval:
  `argocd admin settings rbac can role:<fac>-admin update applications '<fac>/<app>/apps/Deployment/<ns>/<name>'`.

## Rollback

`git revert` the version-bump commit; ArgoCD re-syncs the previous pinned manifests.
Break-glass if the control plane can't self-sync:
`kubectl apply -f https://raw.githubusercontent.com/argoproj/argo-cd/<prev>/manifests/install.yaml`.
Because `argocd-secret` is never managed, credentials and SSO survive every rollback.
