# AgentMesh delivery verification

## Current API group rename

On 2026-09-13 the public API changed to
`agentmesh.newtonguass.github.io/v1alpha1` for all three resource kinds. CRD names,
controller API paths, namespace RBAC and examples use the new group. Persistent
enrollment/ownership metadata keeps its previous keys to preserve existing
selectors and resource ownership. See AGENT-MESH.md for migration steps.

**32 unit tests passed.** `verify_api_group.py` also exited zero on the dedicated
Kubernetes 1.34.0 / Istio 1.31.0 cluster pair. It verified discovery of exactly the
three kinds, new-group CR status updates, matching source hashes in both controller
pods, cross-cluster authorized mTLS (200), unauthorized SA rejection (403), local
declared HTTP (200), and controller RBAC denials for Secrets/other-namespace pods.
Evidence is in `api-group-evidence/`, including complete and cleanup records.

Current verified source:

- controller.py: `67a61e90ec551c6092dbadd06207add8698d10e6902164000726e26a7efb8e5e`
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
