# Verify AgentMesh from a fresh checkout

This is the complete lab runbook for an agent with no previous conversation or
cluster state. Start with the modern lab below. It creates two real Kubernetes
clusters, two independent Istio CAs, real requester certificates, a DNS-addressed
terminating ingress gateway, and a backend protected by local mesh mTLS.

Run shell commands from the **repository root**, in Bash, unless stated otherwise.
No company cluster, shared CA, external DNS account, cloud load balancer, or
preexisting application is needed. The Go controller and its host-side tests are already here.

## New APIs: egress whitelist and developer-selected exposure

After the modern lab setup below, build and load the current image into both
nodes, then run these commands from the repository root (using the test venv):

```sh
go test -race ./...
go vet ./...
.venv/bin/python -u verify_agent_mesh.py
```

The new API contract is documented in [AGENT-MESH.md](AGENT-MESH.md). This test
creates AgentMeshEgress, AgentMeshExpose and AgentMeshTrustedBundle declarations, verifies all four
protocols and whitelist negative cases, and reuses the full original mTLS test.
It writes agent-evidence/ and removes all three test CRDs during cleanup.
Use a clean lab with none of the controller CRDs preinstalled.

The optional compatibility/verify.py wrapper runs the smaller mTLS contract
using the same three APIs. Use verify_agent_mesh.py for the complete whitelist
and exposure checks. Do not run both scripts concurrently.

For application payloads, concurrency, rolling updates and the latest regression
fixes, run `verify_application.py` **instead** after the same lab preparation.
It includes the full whitelist/mTLS checks. See
[APPLICATION-VERIFICATION.md](APPLICATION-VERIFICATION.md) for commands, topology,
pass criteria and interpretation of rollout results. Its evidence directories
are `application-evidence/` and `application-legacy-evidence/`.

## 1. What has actually been verified

| Kubernetes | Istio | Placement | Result (see current build evidence) |
|---|---|---|---|
| 1.34.0 | 1.31.0 | Native requester/backend sidecars; regular gateway | Full application and regression suite passed |
| 1.24.17 | 1.13.5 | Regular sidecars | Full application and legacy regression suite passed |

See VERIFICATION.md for the current Go build, installation modes and completed
coverage. Historical Python evidence is explicitly labeled and is not proof of
the Go implementation. The modern setup below is reproducible from scratch;
the legacy shortcut requires the preserved legacy prerequisites.

Standalone Go tests include snapshots of the previous traffic configuration,
validation failures, gateway owner boundaries, drift/revocation, ConfigMap scope,
cluster namespace isolation, API pagination and projected-token rotation.

## 2. Traffic and trust model

```text
Cluster A, trust domain cluster-a-mesh
  caller application: HTTP http://controller.gateway.test:443/
    -> native istio-proxy sidecar
       ServiceEntry + VirtualService + DestinationRule subset
       requester EnvoyFilter trusts gateway CA B and checks DNS SAN
       client certificate identifies cluster-a-mesh/ns/<test-ns>/sa/caller
    -> TLS with SNI controller.gateway.test
    -> Docker node B IP:31543 (NodePort)

Cluster B, trust domain cluster-b-mesh
  ingressgateway Service 443 -> gateway Envoy listener 8443
    Gateway MUTUAL terminates requester TLS
    gateway EnvoyFilter, SNI + port 8443, adds requester CA A
    Owner-managed AuthorizationPolicy verifies the original requester identity
    -> owner-configured local ISTIO_MUTUAL connection through existing backend Service
    -> backend sidecar -> HTTP backend application on 8080
```

The gateway runs **in the same temporary namespace as the backend**, separate
from `istio-system`. Each cluster has one controller in a separate temporary
operator namespace, with a ClusterRole. The test runner uses cluster-admin access
to prepare the environment. The controller discovers across namespaces, but cannot
read Secrets or edit the administrator's cluster-scoped TrustedBundle spec.

The gateway is a TLS termination hop, not passthrough. Backend authorization
allows the **gateway SA**; it does not see the original requester as its TLS peer.
Original-requester authorization therefore happens at the gateway.

| Name/value | Meaning |
|---|---|
| `mesh-access134-a`, `mesh-access134-b` | Exact kind cluster names required by the modern wrapper |
| `kind-mesh-access134-a`, `kind-mesh-access134-b` | Kubeconfig contexts |
| `/tmp/mesh-access-k134.config` | Dedicated lab kubeconfig; never select a company context |
| `mesh-access-poc-<timestamp>` | Fresh namespace, same name in both clusters, created by each run |
| `mesh-access-poc-<timestamp>-system` | One controller installation per cluster |
| `controller.gateway.test` | mTLS hostname, SNI and required server DNS SAN |
| `normal.controller.test` | Ordinary HTTPS control on the same gateway listener |
| `31543` | Gateway Service NodePort on destination Docker node |
| `443` | Logical URL port and gateway Service port |
| `8443` | Actual gateway Envoy listener port matched by the trust filter |
| `80` / `8080` | Backend Service port / application port |

Both AgentMeshTrustedBundle CRs initially contain A+B public roots. Istiod creates each CA
independently; no CA Secret is copied between clusters. The test signs a short
lived gateway server certificate using B's real CA, with both DNS SANs. Its
credential Secret deliberately has **only B** in `ca.crt`: the gateway filter
must add A for cross-cluster client authentication to work. Private keys are
handled only in memory and temporary private files by the test runner.

The application sends **HTTP**, and the AgentMeshEgress declaration uses
protocol MTLS. The sidecar originates TLS. Sending application HTTPS into this
HTTP ServiceEntry tests a different, unsupported flow.

## 3. Host prerequisites

The recorded environment used Linux amd64, Docker, Bash, Go 1.25+ (tested 1.27.1), Python 3.12, OpenSSL,
kubectl, kind v0.30.0 and Istio's 1.31.0 CLI. The download commands below target
Linux amd64. Other host architectures need matching tool/image artifacts and
were not verified here.

Use a dedicated Docker host with enough capacity for two single-node clusters
and several proxy pods. As a planning allowance, start with 8 CPU cores, 16 GiB
RAM and 25 GiB free disk; these are estimates, not measured minimum requirements.
Allow internet access for release downloads, Docker images and PyYAML. An offline
agent can follow the guide without searching the web, but the host must have
these artifacts downloaded or supplied beforehand.

Install Docker, Python 3.12 with venv support, curl, Git, OpenSSL, tar, sha256sum,
and a Kubernetes 1.34 kubectl using your host's approved installation method.
Docker must already be running and accessible to your shell:

```sh
docker info
python3 --version
kubectl version --client
openssl version
curl --version
git status --short
```

Stop if these fail. Do not work around missing Docker privileges by changing
cluster security settings. If preserved `cluster-a`/`cluster-b` labs exist, stop
them before starting this pair and wait for `docker stop` to finish. Docker can
reassign stopped node addresses; never assume a recorded node IP is current.

Prepare host-only test dependencies (the controller runtime needs no PyYAML):

```sh
python3 -m venv .venv
.venv/bin/python -m pip install PyYAML==6.0.2
go test -race ./...
go vet ./...
```

Expected: Go tests and vet exit 0. Rejection tests may log reconciliation errors;
use the test result to distinguish expected rejection from test failure. The
controller runtime is Go only; Python/PyYAML are host-side test dependencies.

## 4. Download the exact lab tools

```sh
mkdir -p compatibility/tools
curl -fL https://github.com/kubernetes-sigs/kind/releases/download/v0.30.0/kind-linux-amd64 -o compatibility/tools/kind-linux-amd64
curl -fL https://github.com/kubernetes-sigs/kind/releases/download/v0.30.0/kind-linux-amd64.sha256sum -o compatibility/tools/kind-linux-amd64.sha256sum
curl -fL https://github.com/istio/istio/releases/download/1.31.0/istio-1.31.0-linux-amd64.tar.gz -o compatibility/tools/istio-1.31.0-linux-amd64.tar.gz
curl -fL https://github.com/istio/istio/releases/download/1.31.0/istio-1.31.0-linux-amd64.tar.gz.sha256 -o compatibility/tools/istio-1.31.0-linux-amd64.tar.gz.sha256
python3 - <<'PY'
import hashlib
from pathlib import Path
p = Path('compatibility/tools')
for name, suffix in [('kind-linux-amd64', '.sha256sum'),
                     ('istio-1.31.0-linux-amd64.tar.gz', '.sha256')]:
    expected = (p / (name + suffix)).read_text().split()[0]
    actual = hashlib.sha256((p / name).read_bytes()).hexdigest()
    assert actual == expected, 'Checksum mismatch: ' + name
    print('Verified', name)
PY
chmod +x compatibility/tools/kind-linux-amd64
cp compatibility/tools/kind-linux-amd64 compatibility/tools/kind-v0.30.0
tar -xzf compatibility/tools/istio-1.31.0-linux-amd64.tar.gz -C compatibility/tools
compatibility/tools/kind-v0.30.0 version
compatibility/tools/istio-1.31.0/bin/istioctl version --remote=false
```

## 5. Create two clean clusters and independent Istio installations

First run `compatibility/tools/kind-v0.30.0 get clusters`. If the two names below
already exist, use section 10 to resume them; do not create over existing labs.

```sh
compatibility/tools/kind-v0.30.0 create cluster --name mesh-access134-a --image kindest/node:v1.34.0 --config compatibility/kind.yaml --kubeconfig /tmp/mesh-access-k134.config --wait 120s
compatibility/tools/kind-v0.30.0 create cluster --name mesh-access134-b --image kindest/node:v1.34.0 --config compatibility/kind.yaml --kubeconfig /tmp/mesh-access-k134.config --wait 120s
.venv/bin/python build.py
docker pull istio/pilot:1.31.0
docker pull istio/proxyv2:1.31.0
docker pull curlimages/curl:8.10.1
docker pull nginx:alpine
docker pull python:3.12-slim
set -o pipefail
for node in mesh-access134-a-control-plane mesh-access134-b-control-plane; do
  docker save --platform linux/amd64 mesh-access-controller:dev istio/pilot:1.31.0 istio/proxyv2:1.31.0 curlimages/curl:8.10.1 nginx:alpine python:3.12-slim |
    docker exec -i "$node" ctr --namespace k8s.io images import --platform linux/amd64 -
done
compatibility/tools/istio-1.31.0/bin/istioctl install --kubeconfig /tmp/mesh-access-k134.config --context kind-mesh-access134-a -f compatibility/istio-a.yaml -y --readiness-timeout 180s
compatibility/tools/istio-1.31.0/bin/istioctl install --kubeconfig /tmp/mesh-access-k134.config --context kind-mesh-access134-b -f compatibility/istio-b.yaml -y --readiness-timeout 180s
```

`build.py` already streams a minimal Docker build context, including the Go
sources and their embedded build identity. It works when the daemon cannot read
the checkout directory. The runtime image contains `/controller` only. Tests use
separate `python:3.12-slim` application/probe containers; never install Python into
the controller image to satisfy a test.

The image stream avoids the original host's kind image-loader temporary-directory
problem. Build from this checkout and load into **both** nodes after code edits;
reusing the `:dev` name alone does not update an image cached inside a kind node.
`nginx:alpine` is a mutable test fixture tag, so the lab does not promise identical
image bytes across future runs. The Kubernetes and Istio versions are explicit.

The supplied Istio manifests use the default profile, which provides an
`istio-system/istio-ingressgateway` Deployment. The test copies its pod template
to create the separate namespaced gateway. Do not omit this gateway installation.
A default gateway Service may show pending external IP on kind; the test accesses
its own NodePort gateway and does not require a LoadBalancer address.

Check both clusters explicitly:

```sh
for side in a b; do
  kubectl --kubeconfig /tmp/mesh-access-k134.config --context "kind-mesh-access134-$side" wait --for=condition=Ready nodes --all --timeout=120s
  kubectl --kubeconfig /tmp/mesh-access-k134.config --context "kind-mesh-access134-$side" -n istio-system rollout status deploy/istiod --timeout=180s
  kubectl --kubeconfig /tmp/mesh-access-k134.config --context "kind-mesh-access134-$side" -n istio-system rollout status deploy/istio-ingressgateway --timeout=180s
  kubectl --kubeconfig /tmp/mesh-access-k134.config --context "kind-mesh-access134-$side" get --raw=/version
  kubectl --kubeconfig /tmp/mesh-access-k134.config --context "kind-mesh-access134-$side" -n istio-system get deploy/istiod -o jsonpath='{.spec.template.spec.containers[*].image}'
done
```

Expect Kubernetes `v1.34.0` and `istio/pilot:1.31.0`. Do not preinstall the
AgentMesh CRDs or controller: the test installs them and refuses an existing CRD
to avoid deleting someone else's installation during cleanup.

## 6. Run the full verification

Before rerunning, archive `agent-evidence/` if you need the previous run;
the script overwrites its results. Run only one verification process at a time.

```sh
mkdir -p agent-evidence
set -o pipefail
.venv/bin/python -u verify_agent_mesh.py 2>&1 | tee agent-evidence/run.log
```

Allow several minutes after image downloads; exact duration depends on the host.
The wrapper uses only the explicit modern contexts. Its optional
`MESH_ACCESS_KUBECONFIG` environment variable can override the kubeconfig path,
but the context names must remain the same.

You do **not** need to apply any example CRs or create Secrets manually. The test:

1. Installs the three CRDs, fresh injected namespaces, one controller per cluster and A+B
   TrustedBundle CRs; verifies that the real roots differ.
2. Creates caller, denied caller, unselected caller and local-control workloads
   in A; backend and a namespaced gateway in B.
3. Enables STRICT mesh mTLS and a backend ALLOW policy for B's gateway SA.
4. Signs the gateway DNS certificate and installs the gateway's separate SDS
   Secret-read Role. The controller ClusterRole receives no Secret permission.
5. Adds temporary source CoreDNS records pointing both hostnames to the current
   B Docker node IP; configures the requester endpoint override to IP:31543.
6. Applies request declarations for two actual SAs but initially authorizes only
   `caller` at the destination; also creates a separate ordinary HTTPS chain.
   The test runner acts as gateway owner: it creates the exact-SNI MUTUAL Gateway,
   VirtualService and backend TLS rule before applying AgentMeshExpose. Gateway
   RBAC is provisioned only for this fresh fixture from fixtures/gateway-rbac.yaml.
   The controller contributes only the exposure trust filter. The test owner
   creates and updates a separate `owner-client-policy` AuthorizationPolicy.
7. Checks `/controller --version` against the Go source identity in both pods, active Envoy configuration,
   whitelist controls, backend SA independence and the traffic matrix below.
8. Removes temporary resources and restores source CoreDNS in `finally`.

Native `istio-proxy` is a restartable init container (`restartPolicy: Always`).
The controller must recognize it. An older controller image reports a missing
proxy even though injection succeeded; the wrapper's hash check prevents calling
an old image a successful test of new source.

## 7. Required evidence and pass criteria

| Check | Required result |
|---|---|
| Authorized cross-cluster caller | HTTP 200 and expected backend body |
| Different actual requester SA | HTTP 403, gateway RBAC denial |
| Selected and unselected callers to local control | HTTP 200 |
| Ordinary HTTPS, same listener, different SNI, no client certificate | Expected backend body |
| Active source cluster | Exact SNI/DNS SAN, inline trust and one default client SDS entry |
| Active gateway chains | mTLS chain requires client cert; ordinary chain does not |
| Controller reads other namespace pods | Allowed for cluster-wide discovery |
| Controller reads Secrets / patches TrustedBundle spec, Services, Gateways or AuthorizationPolicies | Denied by the real Kubernetes authorizer |
| Manually modified generated DestinationRule | Controller corrects drift |
| Remove B root from requester bundle | HTTP 503 |
| Remove A root from gateway bundle | HTTP 503 |
| Restore both bundles | HTTP 200 |
| Owner changes native AuthorizationPolicy | Original caller 403, newly allowed caller 200 |
| Owner restores native AuthorizationPolicy | Original caller 200 |
| Backend SA changed without exposure CR edit | HTTP 200; no backend SA selector label |
| Trust and authorization updates | Gateway pod UID unchanged |
| Initial exposure, trust/auth updates, Expose deletion | Gateway Deployment/labels, Service, RBAC, Secret, Gateway, VirtualService, backend TLS rule and restored owner AuthorizationPolicy snapshots unchanged |
| Owner's client SAN constraints | Retained in active TLS config; a trusted-CA client with an unlisted SAN fails TLS with 503 |
| Delete one request declaration | Its route/filter removed, remaining caller works |
| Delete final declarations | Generated objects pruned, original resources retained |
| Script completion | Exit code 0, `complete` and subsequent `cleanup` evidence |

The full `verify_application.py` suite also calls `verify_cluster_scope.py`.
It adds a second application namespace with the same SA name, proves automatic
enrollment using the shared cluster bundle, removes/restores a root and checks
fresh mTLS traffic in both namespaces, tests developer namespace RBAC, and deletes
the second namespace's final Egress to verify isolated pruning. API discovery must
report Egress/Expose as namespaced and TrustedBundle as cluster-scoped.

The application namespace is `mesh-access-poc-<timestamp>`; the one controller
namespace is that name plus `-system` in each cluster. The harness refuses existing
AgentMesh CRDs and installation ClusterRoles/ClusterRoleBinding, then cleans its
own cluster RBAC, operator namespace, application namespaces and CRDs. A fresh
kubectl discovery cache per run prevents old namespaced-bundle discovery from
being reused after CRD scope changes.

The test restarts requester pods around trust changes to force fresh TLS
connections. A cached successful connection is not acceptable proof of updated
CA validation. Modern 503 bodies report connection failures; legacy Envoy
reported detailed certificate errors. Do not require identical error wording.

`Configured=True` means API reconciliation succeeded; it does not prove xDS
acceptance or successful TLS. One HTTP 200 does not establish the negative cases.

```sh
.venv/bin/python - <<'PY'
import json
from pathlib import Path
p = Path('agent-evidence/results.json')
rows = json.loads(p.read_text())
assert any(r['test'] == 'complete' for r in rows), 'No complete result'
assert rows[-1]['test'] == 'cleanup', 'Cleanup not confirmed'
for r in rows:
    print(r['test'], '=>', r['result'])
PY
```

Archive `agent-evidence/` and record `git rev-parse HEAD`. Evidence includes
results, active configuration snapshots and controller logs. Do not export
Secrets or CA signing keys as debug artifacts. The final report should state the
actual versions, commit/hash, positive and negative outcomes, and cleanup state.

## 8. Troubleshooting without changing the test contract

| Symptom | Inspect / remedy |
|---|---|
| Docker or image import fails | Fix Docker access/disk. On older containerd, use explicit linux/amd64 export/import below; do not continue with cached images |
| ImagePullBackOff | Confirm the image exists in both nodes and the installed Istio image version matches |
| Missing `istio-ingressgateway` Deployment | Install the supplied default-profile Istio manifests |
| `pod lacks istio-proxy` | Inspect regular and restartable init containers; rebuild/load the current controller |
| Controller source hash mismatch | Rebuild from this checkout and load the image into both nodes |
| Existing AgentMesh CRDs refused | Use clean dedicated labs; do not delete a CRD used by other namespaces |
| Istio validation webhook connection refused just after node restart | Wait for the real webhook, not only cached Deployment readiness. The harness retries a server-side dry-run PeerAuthentication before creating fixtures; it never disables validation |
| Gateway missing certificate / SDS unauthorized | Gateway owner must resolve its existing credentials/RBAC. fixtures/gateway-rbac.yaml is lab-only; AgentMesh does not repair owner settings |
| Configured=False | Read AgentMesh CR status and controller logs; correct prerequisites rather than relaxing TLS |
| HTTP 000 / timeout | Check B's current Docker IP, source CoreDNS, NodePort 31543, and pod readiness |
| Authorized caller 403 | Inspect gateway allow principal and backend policy separately; backend must allow gateway SA |
| Authorized caller 503 | Check active source SAN/trust/client SDS, gateway trust/server SDS, then backend readiness |
| CA removal still returns 200 | Ensure fresh requester pods/connections and verify active bundle contents |
| Ordinary HTTPS breaks | Inspect the distinct SIMPLE SNI chain and the mTLS filter's exact SNI+8443 scope |

For live inspection in another terminal, set the namespace printed in the
`controllers running with namespaced ServiceAccounts` record:

```sh
TEST_NS=mesh-access-poc-REPLACE_WITH_ACTUAL_TIMESTAMP
kubectl --kubeconfig /tmp/mesh-access-k134.config --context kind-mesh-access134-a -n "$TEST_NS" get agentmeshegress,agentmeshexpose,agentmeshtrustedbundle -o yaml
kubectl --kubeconfig /tmp/mesh-access-k134.config --context kind-mesh-access134-a -n "${TEST_NS}-system" logs deploy/mesh-access-controller
kubectl --kubeconfig /tmp/mesh-access-k134.config --context kind-mesh-access134-b -n "$TEST_NS" get pods
kubectl --kubeconfig /tmp/mesh-access-k134.config --context kind-mesh-access134-b -n "$TEST_NS" logs deploy/ingressgateway -c istio-proxy
```

The script cleans resources even after ordinary assertion failures, so saved
evidence is often the only available post-run view. Do not kill the process to
retain pods: forced termination can bypass cleanup. The script has no supported
keep-resources flag. Diagnose saved evidence, fix the cause and rerun; if you
change test code for deeper inspection, record that change in the report.

Istio 1.13.5 cached failed gateway SDS authorization in the original lab. The
test uses a new namespace each run to avoid carrying that cache into retries.
Do not assume the cache timing is identical in newer Istio versions.

## 9. Cleanup and final stopped state

Successful cleanup restores CoreDNS, removes temporary namespaces, test CRDs and
controller/delegation ClusterRoles and ClusterRoleBinding, and leaves both base
Istio installations running. Confirm no test leftovers:

```sh
for side in a b; do
  kubectl --kubeconfig /tmp/mesh-access-k134.config --context "kind-mesh-access134-$side" get ns
  kubectl --kubeconfig /tmp/mesh-access-k134.config --context "kind-mesh-access134-$side" get crd agentmeshtrustedbundles.agentmesh.io agentmeshegresses.agentmesh.io agentmeshexposes.agentmesh.io --ignore-not-found
done
docker stop mesh-access134-a-control-plane mesh-access134-b-control-plane
docker inspect --format '{{.Name}} running={{.State.Running}} status={{.State.Status}}' mesh-access134-a-control-plane mesh-access134-b-control-plane
```

Expect both nodes `running=false status=exited`. Wait for stop completion before
starting another pair. Stopping preserves cluster state; it does not delete it.

If the process was forcibly interrupted, do not claim cleanup succeeded. Inspect
CoreDNS for the two test names and resources in the exact recorded namespace.
For a disposable lab with no data to preserve, deleting and recreating just
these two clusters is the simplest recovery. Deletion is also the optional full
teardown, instead of preserving stopped clusters:

```sh
compatibility/tools/kind-v0.30.0 delete cluster --name mesh-access134-a --kubeconfig /tmp/mesh-access-k134.config
compatibility/tools/kind-v0.30.0 delete cluster --name mesh-access134-b --kubeconfig /tmp/mesh-access-k134.config
```

## 10. Fast repeat on preserved clusters

Skip downloads and creation. Start nodes, refresh the dedicated kubeconfig,
repeat the readiness checks in section 5, rebuild/load the controller if edited,
then run section 6 and stop as in section 9.

```sh
docker start mesh-access134-a-control-plane mesh-access134-b-control-plane
compatibility/tools/kind-v0.30.0 export kubeconfig --name mesh-access134-a --kubeconfig /tmp/mesh-access-k134.config
compatibility/tools/kind-v0.30.0 export kubeconfig --name mesh-access134-b --kubeconfig /tmp/mesh-access-k134.config
```

The test discovers B's current node IP on every run and issues a new gateway
server certificate. Avoid restoring old DNS entries or expired server Secrets
from a previous test namespace.

## 11. Legacy regression and limits of the claim

`integration_test.py` is the same traffic contract with fixed default-kubeconfig
contexts `kind-cluster-a` and `kind-cluster-b`. It requires already-running
Kubernetes 1.24.17 / Istio 1.13.5 labs with trust domains `cluster-a-mesh` and
`cluster-b-mesh`, independently generated Istio CAs in `istio-ca-secret`, and the
default B gateway Deployment in `istio-system`. Build/load the current controller
and fixture images into both nodes, then run `AGENT_MESH_LEGACY=1 .venv/bin/python -u verify_agent_mesh.py`.
Its evidence goes to `agent-legacy-evidence/`. This legacy shortcut is for the original
preserved labs; use the modern instructions above for a fresh checkout.

This verification covers HTTP application -> requester sidecar mTLS ->
terminating gateway -> local mesh backend, inline trust bundles, and the two
tested version combinations. It does not establish mounted-file trust, CA issuer
rotation automation, ambient mesh, dynamic external TCP DNS, application HTTPS-to-mTLS origination,
TLS passthrough, or every Kubernetes/Istio version. It also does not benchmark CA
bundle size or load-test throughput. Keep those separate from this pass claim.

## 12. Namespace-only ConfigMap verification

For installing into an existing development namespace instead of a disposable lab,
use [NAMESPACE-INSTALL.md](NAMESPACE-INSTALL.md).

Use the same two clean labs, image build and image imports. Run this after the
cluster-mode suite has finished cleanup; never run suites concurrently:

```sh
.venv/bin/python -u verify_namespace.py
# On the preserved legacy pair instead:
AGENT_MESH_LEGACY=1 .venv/bin/python -u verify_namespace.py
```

The namespace suite installs only `install-namespaced.yaml` into fresh application
namespaces. It does not install AgentMesh CRDs, ClusterRoles or ClusterRoleBindings.
The runner acts as administrator when preparing fixtures and publishing roots;
the Go controller operates using its actual namespaced Role. A shared host-side
fixture helper writes egress/expose lists into the declarations ConfigMap and
reads status from the state ConfigMap; it does not emulate the controller.

Required results: no AgentMesh CRDs exist; controller can read local pods/trust but
cannot read other-namespace pods, Secrets, modify trust/settings/declarations or
AuthorizationPolicies. The developer Role can patch declarations but cannot patch
trust/settings. Invalid JSON/declarations retain prior traffic rules and report
failure. All four protocols, same-cluster and cross-cluster gateway MTLS, CA
removal/restoration, owner-auth allow/deny, ordinary HTTPS and deletion pruning
reuse the real traffic contract. Owner gateway resources remain unchanged.

Evidence is `namespace-evidence/` or `namespace-legacy-evidence/`. Expect exit 0,
a `complete` result and final `cleanup`. The full JSON payload/concurrency/rollout
suite runs in CRD mode; the namespace suite tests the same renderer and real
network path without repeating that load workload. Stop the active pair using
section 9 when finished.

### Legacy image archive compatibility

The original legacy containerd rejected a multi-platform image archive with a
missing-content-digest error and retained the previous image under the same tag.
Use explicit platform selection (these commands target the recorded Linux amd64
host and a Docker CLI supporting `save --platform`):

```sh
set -o pipefail
for node in cluster-a-control-plane cluster-b-control-plane; do
  docker save --platform linux/amd64 mesh-access-controller:dev python:3.12-slim |
    docker exec -i "$node" ctr --namespace k8s.io images import --platform linux/amd64 -
done
```

Wait for both imports to exit 0. Installation now checks `/controller --version`
immediately after the controller rollout, before creating application traffic
fixtures. An old Python image cannot pass this check. A failed import or rollout
is not test evidence for the new Go build.
