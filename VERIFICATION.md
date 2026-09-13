# AgentMesh delivery verification

## Current Go delivery: two installation modes, trust-only exposure

The runtime is entirely Go. Cluster mode retains exactly three CRDs; namespace
mode uses only local ConfigMaps and a Role/RoleBinding. Expose creates only a
host-and-listener-port trust EnvoyFilter. Owner AuthorizationPolicies are external
inputs to the traffic tests and are never read/written by the controller.

- Source build identity: `af51fbc1f6316baf5ff3a1c7c853eb6de82861539a0018a27adbc1fb6b8d3074`.
- Docker build: `golang:1.27.1` builder, single static `/controller` binary in scratch.
- Tested runnable linux/amd64 image manifest: `sha256:23f2adea0a5cab4bae06abe699e0e7d69035eba6f3208654c3c48f6348d761eb`.
- `go test -race -cover ./...` passed; statement coverage 75.2%. `go vet ./...` passed.
  [Unit evidence](verification/go-unit.json) records 26 passed tests and subtests.
- Unit coverage includes Python traffic-plan parity, bad declarations, regular/native
  gateway listeners, retained SAN/pin restrictions, ownership preflight, ConfigMap
  scope/drift/revocation/invalid retention, cluster namespace isolation and API token
  rotation/pagination. Pin preservation has unit coverage; no live pinning claim.

| Mode / workload | Kubernetes / Istio | Result / evidence |
|---|---|---|
| Cluster CRDs, full JSON application suite | 1.34.0 / 1.31.0 | Passed, 88 records: [results](verification/go-cluster-modern.json) |
| Namespace ConfigMaps, full whitelist/mTLS contract | 1.34.0 / 1.31.0 | Passed, 62 records: [results](verification/go-namespace-modern.json) |
| Cluster CRDs, full JSON application suite | 1.24.17 / 1.13.5 | Passed, 90 records: [results](verification/go-cluster-legacy.json) |
| Namespace ConfigMaps, full whitelist/mTLS contract | 1.24.17 / 1.13.5 | Passed, 64 records: [results](verification/go-namespace-legacy.json) |

Both CRD runs recorded 10/10 DNS-addressed JSON POSTs, 20/20 requests carrying
128 KiB Unicode payloads, 200/200 concurrent requests and 600/600 requests during
backend rolling update as HTTP 200. Both runs completed cleanup. These observations are not an availability
SLO or throughput benchmark. Each kind cluster has a single node.

Positive mTLS, wrong actual requester SA (owner-policy 403), issuer removal (503),
restoration (200), ordinary HTTPS on another SNI, local native-Service gateway
MTLS, whitelist negatives, generated drift correction and pruning are exercised.
Modern sidecars are native/restartable; the legacy suite uses regular sidecars.
The runtime role cannot read Secrets or patch Services, Gateways, AuthorizationPolicies
or cluster TrustedBundle specs. Permission checks use Kubernetes' real authorizer.

Namespace verification additionally requires no AgentMesh CRDs/cluster controller
RBAC, denied cross-namespace discovery, read-only trust/settings/declarations,
state-only ConfigMap writes and developer-only declaration edits. Invalid input
must retain prior traffic until repaired. The runner administers fixtures; it does
not grant those administrator permissions to the tested controller.

Existing gateway listener/route/credential/workload preservation is checked by
snapshot hashes. Owner auth changes are intentional test actions; policies are
restored before comparing snapshots. No private-key or Secret contents are
published. The modern CRD run began before the explicit AuthorizationPolicy
snapshot was added; it still checks owner allow/deny changes and proves controller
auth-policy PATCH permission is denied. Subsequent runs include that snapshot.

A first legacy attempt ran a cached Python image after an archive import failure;
it failed and cleaned up. That attempt is not part of the pass results. Explicit
linux/amd64 image import fixed the older containerd issue, and setup now checks
the Go build identity immediately after rollout. The corrected full run passed.

All four runs exited 0 and recorded cleanup. CoreDNS was restored and temporary
application/operator namespaces, CRDs and installation RBAC removed. The original
legacy passthrough, remote-termination and local-termination paths each still returned
HTTP 200; see [baseline restoration](verification/go-legacy-baseline.json).
All four preserved lab node containers are stopped: [final state](verification/go-lab-state.json).

Fresh installs and local traffic tests do not establish an existing-company-cluster
migration, arbitrary custom SDS/filter compatibility, OPA enforcement, network
bypass containment, mounted trust, automatic CA distribution, cloud load balancing,
HTTP/2/gRPC or multi-node HA. Follow AGENT-MESH.md for owner adoption and mode migration.

## Historical Python gateway-boundary verification (superseded)

This earlier Python candidate generated a trust EnvoyFilter and AuthorizationPolicy.
The current Go implementation above removes exposure authorization entirely.
Both use existing namespace gateway labels and are scoped to the declared host and
actual listener port. Gateway Deployment/labels, Services, RBAC, Secrets, listeners,
VirtualServices and backend TLS rules remain owner-managed. The controller role
has only get/list for Services and Gateways. Egress generation remains supported.

The owner must already supply one exact-SNI HTTPS MUTUAL listener and a route to
the declared backend Service/port. Missing/ambiguous prerequisites report failure
without gateway mutation. credentialName was removed from AgentMeshExpose; it
belongs in the owner's Gateway. The old root gateway-rbac.yaml moved to
fixtures/gateway-rbac.yaml and is used only to provision isolated lab gateways.

**43 unit tests passed**, including unchanged owner resources/legacy labels,
missing/non-mTLS listeners, missing routes, retained legacy generated routes,
read-only Service/Gateway RBAC, and copied owner SAN/SPKI/certificate-hash checks.
The gateway trust patch preserves these certificate checks when replacing the
validation-context oneof; the owner still supplies server credentials and TLS mode.

| Current full application suite | Kubernetes / Istio | Result |
|---|---|---|
| Modern, native sidecars | 1.34.0 / 1.31.0 | Passed: 89 records, complete and cleanup |
| Legacy, regular sidecars | 1.24.17 / 1.13.5 | Passed: 89 records, complete and cleanup |

The lab runner acts as gateway owner and creates listener/routes/credentials
before Expose enrollment. Snapshot hashes compare Deployment/template, Service,
gateway Role/RoleBinding, Secret, Gateway, VirtualService and backend DestinationRule
before/after enrollment, trust/auth updates and Expose deletion. The runner's
explicit scale-out from one gateway replica to two refreshes the owner baseline;
the controller does not perform that action. No Secret contents are published.

The SAN test enrolls a client with a valid trusted issuer but an owner-disallowed
SAN; TLS must fail with 503. A different SAN-accepted but unauthorized requester
still gets authorization 403. Active TLS checks verify the owner's SAN list.
Certificate-pin preservation has unit coverage, not a separate live pinning test.
SelfSubjectAccessReviews using each destination controller's actual token report
Gateway and Service patch permissions as denied, without attempting to change
those resources. See [modern RBAC evidence](verification/owner-gateway-modern-rbac.json)
and [legacy RBAC evidence](verification/owner-gateway-legacy-rbac.json).

Historical Python runtime hashes:

- controller.py: `80552f6398906d79ce3fe01df09107570ceba1a62749cbc2747ed9437b01221e`
- egress.py: `869cb706e0de168c9d1a38ac322e26dbdd3b7f9ccf1cad9369e1593e366a0c74`
- Image: `sha256:ec2c858f777fe5b6785af115430d17b2c0cecb9b08499dcb07ae2a5ba18dbd1f`

Custom validation supplied by other filters or extra SDS data needs separate
compatibility review. Existing-install adoption is documented in AGENT-MESH.md;
the controller leaves legacy generated routes/aliases and gateway labels intact.
This verification uses fresh local labs, not a company gateway migration.

## Previous cluster-wide controller and administrator trust

The current API is `agentmesh.io/v1alpha1`. There is one administrator-controlled
controller and one selected shared bundle per cluster. Egress/Expose remain
namespaced; TrustedBundle is cluster-scoped. That historical controller image was Python.

**38 unit tests passed.** New tests cover automatic namespace ownership anchors,
shared CA updates, namespace error isolation, independent cleanup after final CR
deletion, ignored local trust overrides, forbidden/terminating namespaces and
the separation of developer, trust-administrator and controller permissions.

| Fresh-install lab | Kubernetes / Istio | Current full application suite |
|---|---|---|
| Modern, native sidecars | 1.34.0 / 1.31.0 | Passed: 83 records, complete and cleanup |
| Legacy, regular sidecars | 1.24.17 / 1.13.5 | Passed: 83 records, complete and cleanup |

The suite runs `verify_cluster_scope.py` before the whitelist and JSON application
checks. Evidence is committed in
[verification/cluster-scope-modern.json](verification/cluster-scope-modern.json) and
[verification/cluster-scope-legacy.json](verification/cluster-scope-legacy.json).
Both runs exited zero. Temporary namespaces, CRDs and cluster RBAC were removed,
and CoreDNS was restored. The preserved legacy passthrough, remote-termination
and local-termination paths all still returned 200; see
[verification/legacy-baseline.json](verification/legacy-baseline.json).
All four lab node containers were stopped after verification.

| Verified behavior | Observed result |
|---|---|
| API discovery | Egress/Expose namespaced; TrustedBundle cluster-scoped |
| Same SA name in two application namespaces | Independent enrollment, both use the shared trust bundle |
| Namespace-local setup | Ownership anchors created automatically; no local config.json or bundle needed |
| Administrator removes remote CA from the shared source bundle | Fresh mTLS requests from both namespaces return 503 |
| Administrator restores remote CA | Both namespaces return 200 |
| Developer RoleBinding | Can create Egress/Expose in its namespace; cannot create Egress in another namespace or patch trust/controller config |
| Actual controller credentials | Cross-namespace workload reads succeed; Secret read and TrustedBundle spec patch return 403 |
| Last Egress deleted in second namespace | Generated resources pruned; first namespace still returns 200 |
| Remote/local terminating gateway mTLS | Authorized requester succeeds; wrong remote SA returns 403 |
| Ordinary HTTPS on separate SNI, same listener | Succeeds without a client certificate |
| JSON API and rollout | 200 concurrent requests and 600 requests during backend rollout succeed |
| Whitelist and trust revocation | Undeclared/revoked destinations blocked; source and gateway CA removal reject mTLS |

Runtime hashes were checked inside both controller pods:

- controller.py: `02a727b43a290252e6364efc23775f88802b608114415c69daadc3fad81f5e16`
- egress.py: `16e99dba4143af055245839658bea0a012a41bb021b3564e0268d595432ed780`
- Loaded image: `sha256:11ef27602e3bc295a3d4b54b96831a8f6359450b7c17f6dfb3e1b3a56a0e496f`

The first modern attempt encountered stale kubectl discovery for the former
namespaced bundle endpoint and was cleaned up. The harness now gives each run a
fresh discovery cache. The subsequent complete run used the current cluster scope.
The first legacy attempt started before Istio's validation webhook was reachable
after node resume; it also cleaned up. The repeat reached the application tests.
A server-side dry-run readiness probe was added to the harness and checked on
both legacy clusters; validation is never bypassed.

These tests use dedicated local kind clusters, independent real Istio CAs and
namespaced ingress gateways. They do not verify a company deployment, in-place
CRD migration, large-cluster capacity, HA leader election or capture-bypass
prevention. Scope migration is documented in AGENT-MESH.md; external hardening
remains assigned to RBAC, admission/OPA, CNI and platform owners.

## Historical API group rename

On 2026-09-13 the public API changed to
`agentmesh.io/v1alpha1` for all three resource kinds. CRD names,
controller API paths, namespace RBAC and examples use the new group. Persistent
enrollment/ownership metadata keeps its previous keys to preserve existing
selectors and resource ownership. See AGENT-MESH.md for migration steps.

**32 unit tests passed.** `verify_api_group.py` also exited zero on the dedicated
Kubernetes 1.34.0 / Istio 1.31.0 cluster pair. It verified discovery of exactly the
three kinds, new-group CR status updates, matching source hashes in both controller
pods, cross-cluster authorized mTLS (200), unauthorized SA rejection (403), local
declared HTTP (200), and controller RBAC denials for Secrets/other-namespace pods.
Evidence is in `api-group-evidence/`, including complete and cleanup records.
The earlier intermediate-group run is archived in
`api-group-evidence/previous-f87398e/`.

Verified source for that earlier run:

- controller.py: `1c4f6295bdb41b1a13efd568ba3da0ecd0bf992ddb9ab99c0f445bae4772c2bb`
- egress.py: `16e99dba4143af055245839658bea0a012a41bb021b3564e0268d595432ed780`

The rename was tested as a fresh installation on the modern pair. Existing-group
migration and the full application/legacy suites were not rerun for this rename;
the earlier results below remain tied to their recorded source. No company
installation was migrated. Temporary resources were cleaned and lab nodes stopped.

## Earlier full application verification

Verified on 2026-09-13 using source from commit `ec98528` on two independent-CA
clusters per run. The test ran real namespace-owned controller pods, native
gateway Services, a terminating namespaced ingress gateway, and application
traffic; it did not stop at accepting manifests.

| Lab | Kubernetes | Istio | Result |
|---|---|---|---|
| Modern | 1.34.0 | 1.31.0 | Complete and cleanup passed |
| Legacy | 1.24.17 | 1.13.5 | Complete and cleanup passed |

Both runs used the same source hashes, checked inside both controller pods:

- controller.py: `d961395e96708f6045dad178041f3393f78a8c641696a63d92c7fae245fa5cc0`
- egress.py: `16e99dba4143af055245839658bea0a012a41bb021b3564e0268d595432ed780`

Image loaded into all four lab nodes:
`sha256:8b741a3d7af66061973be0cb978dce86aca2d05fa2b82ac430213d526f4cd979`.

The API contains exactly AgentMeshEgress, AgentMeshExpose and
AgentMeshTrustedBundle. AgentMeshExpose does not accept serviceAccount; backend
selection preserves the existing Service selector. Both live suites create these
APIs directly, without a legacy declaration adapter.

The standalone suite passed **31 tests**, including the three-API schema,
reconciliation, trust validation/missing bundles, alternate bundle selection,
mixed-SA backends without backend enrollment, namespace RBAC paths, native
sidecars, drift, revocation, ownership, exact case-insensitive HTTP authority
matching and invalid-unused-bundle isolation.

## JSON application and lifecycle results

`verify_application.py` includes the original full whitelist/mTLS suite plus a
JSON quote API, two backend replicas, two namespaced gateway replicas and a
cross-namespace catalog Service. The application uses a named backend targetPort
and a DNS upstream gateway endpoint. Detailed reproduction and the requested
external-component hardening recommendation table are in
[APPLICATION-VERIFICATION.md](APPLICATION-VERIFICATION.md).

| Application check | Kubernetes 1.34 / Istio 1.31 | Kubernetes 1.24 / Istio 1.13.5 |
|---|---|---|
| JSON POST through independent-CA gateway | 10/10 returned 200 | 10/10 returned 200 |
| Reused HTTP/1.1 connection, Unicode JSON with 128 KiB padding | 20/20 returned 200 | 20/20 returned 200 |
| Eight concurrent clients | 200/200 returned 200 | 200/200 returned 200 |
| Fresh connections across backend replicas | 120/120 returned 200; both replicas served | Same |
| Native DNS Service in a different namespace, STRICT local mTLS | 10/10 returned 200 | 10/10 returned 200 |
| Wrong actual SA at gateway | 10/10 returned 403 | 10/10 returned 403 |
| Another SA accesses undeclared catalog | 10/10 blocked | 10/10 blocked |
| Allowed uppercase HTTP authority | 200 | 200 |
| Unlisted authority / mismatched authority port | 3/3 returned 403 for each | Same |
| Backend rolling update with concurrent persistent traffic | 600/600 returned 200 | 600/600 returned 200 |
| Fresh requests after rollout | 10/10 reached v2 | 10/10 reached v2 |
| Revoke catalog while unused bundle is invalid | 3/3 blocked; active CR configured, bad bundle failed | Same |
| Restore catalog declaration | 10/10 returned 200 | 10/10 returned 200 |
| Hardened application container | UID 10001; no default API-token or SDS socket mount | Same |

Two controller bugs were reproduced on the unchanged image before fixing them:
uppercase allowed authority returned 403, and an invalid unused bundle froze a
revocation while all three fresh catalog requests still returned 200. The fixed
controller uses exact case-insensitive HTTP matching and validates unused bundles
independently. Invalid required trust still preserves the last good plan.

The active gateway authorization host matchers were also confirmed
case-insensitive on both versions. An additional legacy live control returned
200 for the authorized SA's uppercase Host header and 403 for the unauthorized
SA with either case. No admission, RBAC or network-isolation hardening component
was added to the controller; those remain recommendations.

The 600-request rows are client-visible results with no client POST retries;
normal proxy retry behavior remains. Positive tests wait for convergence. These
are functional results, not an availability SLO, throughput benchmark or proof of
same-pod security isolation.

## Live results on both version combinations

| Scenario | Observed |
|---|---|
| Declared internal HTTP and TCP | 200 with expected body |
| Declared external HTTP and TCP | 200 with expected body |
| Application HTTPS alongside originated MTLS on port 443 | Both 200 |
| Undeclared port on a declared internal Service | 403; unselected control on the same port returned 200 |
| Direct-IP HTTP attempt | 403 |
| Undeclared HTTP host on a declared HTTP port | 403 |
| Other SA to another account's external destination | Blocked |
| Undeclared external TCP port | Reset; unselected control returned 200 |
| Different reachable IP on the same TCP port | Reset; unselected control returned 200 |
| Revoked HTTP/TCP entries | Blocked on fresh connections |
| Empty whitelist | Internal and remote application traffic blocked |
| Restored whitelist | 200 |
| Local gateway MTLS through an HTTP-named native Service | 200; no ServiceEntry for the native Service |
| Remote gateway MTLS with independent cluster CAs | 200 |
| Change backend ServiceAccount without editing exposure | 200; alias selector unchanged, no backend SA label |
| Wrong actual requester ServiceAccount | Gateway RBAC 403 |
| Remove B CA from requester TrustedBundle CR | 503 |
| Remove A CA from gateway TrustedBundle CR | 503 |
| Restore trust bundles | 200 |
| Change allowed requester identity | Old caller 403, new caller 200 |
| Trust/authorization updates | Gateway pod UID unchanged |
| Ordinary HTTPS on another SNI of the same gateway listener | Succeeded without client certificate |
| Controller reads other-namespace pods or its own Secrets | Kubernetes 403 |
| Generated DestinationRule drift | Corrected automatically |
| Delete one caller | Its rules pruned; remaining caller succeeded |
| Delete all declarations | Generated resources pruned; original resources retained |

Active Envoy configuration confirmed both outbound RBAC guards, exact gateway
SNI/SAN, preserved default client certificate SDS, and gateway client-certificate
enforcement. Requesters were restarted during trust/revocation checks to avoid
proving success with an already-established TLS connection.

The modern CA failure bodies report connection failure/termination; legacy Envoy
reports certificate verification failure / unknown CA. The expected invariant is
rejection and restoration, not identical Envoy error strings.

## Reproduce and inspect

Follow TESTING.md to create the modern pair, then run verify_application.py.
AGENT_MESH_LEGACY=1 selects the preserved legacy contexts as described in
AGENT-MESH.md. Each run installs all three CRDs in clean labs, creates temporary
namespaces, and restores CoreDNS/removes the fixtures in finally.

Raw evidence stays local and is excluded from Git:
application-evidence/results.json and application-legacy-evidence/results.json,
plus active configuration snapshots and controller logs. Each contains 71 records,
including complete and cleanup, without a failure record. Both processes exited
zero. The earlier 2026-09-11 runs remain in agent-evidence/ and
agent-legacy-evidence/; diagnostic failures are in application-discovery-evidence/.
This document is the public result summary.

## Boundaries

These results cover captured sidecar traffic, HTTP-to-MTLS origination, the
tested API/version combinations, and the declared test fixtures. They do not
claim protection against bypassing the sidecar, automatic mesh federation,
ambient mesh, CA issuer rotation automation, or load/CA-size benchmarking. The
labs have one kind node per cluster and use NodePort, not a cloud load balancer;
multi-node availability and CNI/admission enforcement were not tested.
External TCP needs explicit IPs; native local gateway HTTP origination needs an
HTTP-named Service port. The removed API is no longer served or reconciled.

Final lab state: all four node containers were confirmed exited/running=false.
Temporary test namespaces and all three CRDs were removed. The original legacy
passthrough and local/remote termination demos each returned 200 after cleanup and verification of the current destination node address. Cluster data is preserved. Nothing was deployed in company clusters.
