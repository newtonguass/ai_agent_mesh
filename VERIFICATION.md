# AgentMesh delivery verification

Verified on 2026-09-11 using the final controller source on two independent-CA
clusters per run. The test ran real namespace-owned controller pods, native
gateway Services, a terminating namespaced ingress gateway, and application
traffic; it did not stop at accepting manifests.

| Lab | Kubernetes | Istio | Result |
|---|---|---|---|
| Modern | 1.34.0 | 1.31.0 | Complete and cleanup passed |
| Legacy | 1.24.17 | 1.13.5 | Complete and cleanup passed |

Both runs used the same source hashes, checked inside both controller pods:

- controller.py: `e654114878b8fcf6f62a6bc16f993bbae5d149e5206e19ad36ee64b7f91025b3`
- egress.py: `a670bd5a586fb43d2e04cfdb7f2bd41a30dbbae8ef6689bfb2f20951bb6f6d0b`

The API contains exactly AgentMeshEgress, AgentMeshExpose and
AgentMeshTrustedBundle. AgentMeshExpose does not accept serviceAccount; backend
selection preserves the existing Service selector. Both live suites create these
APIs directly, without a legacy declaration adapter.

The standalone suite passed **28 tests**, including the three-API schema,
reconciliation, trust validation/missing bundles, alternate bundle selection,
mixed-SA backends without backend enrollment, namespace RBAC paths, native
sidecars, drift, revocation and ownership.

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

Follow TESTING.md to create the modern pair, then run verify_agent_mesh.py.
AGENT_MESH_LEGACY=1 selects the preserved legacy contexts as described in
AGENT-MESH.md. Each run installs all three CRDs in clean labs, creates temporary
namespaces, and restores CoreDNS/removes the fixtures in finally.

Raw evidence stays local and is excluded from Git:
agent-evidence/results.json and agent-legacy-evidence/results.json, plus active
configuration snapshots and controller logs. They contain complete and cleanup
records. This document is the public result summary.

## Boundaries

These results cover captured sidecar traffic, HTTP-to-MTLS origination, the
tested API/version combinations, and the declared test fixtures. They do not
claim protection against bypassing the sidecar, automatic mesh federation,
ambient mesh, CA issuer rotation automation, or load/CA-size benchmarking.
External TCP needs explicit IPs; native local gateway HTTP origination needs an
HTTP-named Service port. The removed API is no longer served or reconciled.

Final lab state: all four node containers were confirmed exited/running=false.
Temporary test namespaces and all three CRDs were removed. The original legacy
passthrough and local/remote termination demos each returned 200 after cleanup and verification of the current destination node address. Cluster data is preserved. Nothing was deployed in company clusters.
