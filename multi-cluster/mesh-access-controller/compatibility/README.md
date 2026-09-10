# Kubernetes 1.34 / modern Istio compatibility lab

The isolated clusters are `mesh-access134-a` and `mesh-access134-b`, running
Kubernetes 1.34.0 and Istio 1.31.0. The older `cluster-a`/`cluster-b` fixtures
remain separate. This directory saves the modern run's evidence so it does not
overwrite the original integration results.

`verify.py` reuses the full controller integration contract with explicit
modern contexts and a separate kubeconfig. It verifies server/control-plane
versions, compares controller source hashes inside both running pods with the
workspace file, and records actual native-sidecar placement and Envoy version.
The default kubeconfig is `/tmp/mesh-access-k134.config`; set
`MESH_ACCESS_KUBECONFIG` to use a different path containing these exact contexts.

The initial unchanged-controller run failed at configuration reconciliation:
Istio injected the requester and backend proxies as restartable init containers,
but the original detector inspected only `spec.containers`. That evidence is in
`initial-native-sidecar-failure/`. The detector now also considers
`spec.initContainers` with `restartPolicy: Always`, including gateway named-port
resolution. Finite init containers do not qualify. The CRD and trust patches
were not changed as part of this fix.

Generated evidence, initial-failure snapshots, and downloaded binaries are kept
in the original lab workspace and are excluded from this repository. Run the
verification scripts to generate fresh evidence locally. The results summarized
here describe the completed lab run.

## Reproduce

Use kind v0.30.0 and the official Istio 1.31.0 distribution. The downloaded tools
were checked against their release SHA-256 checksums. Local binaries are under
`tools/` and are excluded from version control. Commands below run from this
directory unless noted.

Create the clusters once (do not run create against preserved existing ones):

```sh
tools/kind-v0.30.0 create cluster --name mesh-access134-a --image kindest/node:v1.34.0 \
  --config kind.yaml --kubeconfig /tmp/mesh-access-k134.config --wait 120s
tools/kind-v0.30.0 create cluster --name mesh-access134-b --image kindest/node:v1.34.0 \
  --config kind.yaml --kubeconfig /tmp/mesh-access-k134.config --wait 120s
```

Build the controller from its parent directory and load images into each node:

```sh
docker build -t mesh-access-controller:dev ..
docker pull istio/pilot:1.31.0
docker pull istio/proxyv2:1.31.0
docker pull curlimages/curl:8.10.1
docker pull nginx:alpine
docker save mesh-access-controller:dev istio/pilot:1.31.0 istio/proxyv2:1.31.0 curlimages/curl:8.10.1 nginx:alpine | \
  docker exec -i mesh-access134-a-control-plane ctr --namespace k8s.io images import -
docker save mesh-access-controller:dev istio/pilot:1.31.0 istio/proxyv2:1.31.0 curlimages/curl:8.10.1 nginx:alpine | \
  docker exec -i mesh-access134-b-control-plane ctr --namespace k8s.io images import -
```

Install independent control planes. The manifests set separate trust domains;
each Istiod generates its own CA. Do not copy a CA Secret between clusters.

```sh
tools/istio-1.31.0/bin/istioctl install --kubeconfig /tmp/mesh-access-k134.config \
  --context kind-mesh-access134-a -f istio-a.yaml -y --readiness-timeout 180s
tools/istio-1.31.0/bin/istioctl install --kubeconfig /tmp/mesh-access-k134.config \
  --context kind-mesh-access134-b -f istio-b.yaml -y --readiness-timeout 180s
python3 verify.py
docker stop mesh-access134-a-control-plane mesh-access134-b-control-plane
```

The test creates temporary namespaces, controller installations, a DNS-SAN
gateway certificate signed by B's real CA, and DNS records. In `finally` it
restores CoreDNS and removes its temporary namespaces and MeshAccess CRDs.
It refuses an existing MeshAccess CRD to avoid deleting another installation.
CA signing keys are only handled in memory and temporary private files; evidence
does not include Secret objects or private keys.

To resume preserved clusters later:

```sh
docker start mesh-access134-a-control-plane mesh-access134-b-control-plane
tools/kind-v0.30.0 export kubeconfig --name mesh-access134-a --kubeconfig /tmp/mesh-access-k134.config
tools/kind-v0.30.0 export kubeconfig --name mesh-access134-b --kubeconfig /tmp/mesh-access-k134.config
```

Wait for nodes and Istio pods to become Ready before testing. Run one lab pair
at a time: Docker can reuse the stopped legacy nodes' addresses, and legacy
fixtures contain DNS entries that must be checked if both pairs are started.

## Scope

A passing run verifies this exact Kubernetes/Istio combination, the deployed
controller source hash, native requester/backend sidecars, and a terminating
Istio gateway. It does not establish support for every intervening release or
ambient mesh. Runtime reconciliation still uses the same stable Kubernetes APIs
and the Istio APIs served by both tested control planes.

Before an upgrade, inspect actual xDS, test successful traffic and negative
controls, and check ordinary traffic. An accepted CRD or `Configured=True`
alone is insufficient. `evidence/results.json` records a `complete` result only
after the whole traffic contract succeeds, followed by a cleanup result.

Official version provenance:
[Istio 1.31 release](https://istio.io/latest/news/releases/1.31.x/announcing-1.31/),
[Istio support matrix](https://istio.io/latest/docs/releases/supported-releases/).

## Completed verification: 2026-09-11

The complete live contract passed; evidence/results.json includes complete and
cleanup records. Authorized mTLS returned 200, the wrong actual service account
returned 403, removing either CA caused 503, and restoring trust returned 200.
Authorization changes, ordinary HTTPS on the same listener, unrelated local mesh
traffic, namespace RBAC, drift correction, and shared-resource deletion passed.
Trust and authorization changes preserved the gateway pod UID.

Both running controller pods matched workspace controller.py SHA-256:
211739560aa9c6c4c347cb47ab4f00f35a81d5c146e59e08df65fa4f8fb68767.
The actual Envoy version was
0a12a02f0db52f6d229ec0fbe0f2091e1c02098b/1.39.1-dev/Clean/RELEASE/BoringSSL.
Fifteen standalone behavior tests passed, including native-sidecar detection
and rejection of finite init containers.

Both modern node containers were stopped after cleanup and preserved for reuse.

The same updated controller source also passed the complete legacy regression
on Kubernetes 1.24.17 / Istio 1.13.5 on 2026-09-11. Both legacy controller pods'
source hashes matched the modern run. Legacy evidence is saved in
mesh-access-controller/evidence/results.json and legacy-regression-versions.json.
