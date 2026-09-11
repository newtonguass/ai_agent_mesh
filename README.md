# AgentMesh: egress whitelists and cross-cluster mTLS

A namespace-owned controller for Istio sidecar meshes with independent CAs.
The API has exactly three namespaced CRDs:

| CRD | Purpose | Main fields |
|---|---|---|
| AgentMeshEgress | Whitelist captured outbound traffic for a ServiceAccount | serviceAccount, inCluster/outCluster host + port + protocol |
| AgentMeshExpose | Expose an HTTP Service through a selected mTLS gateway | service, port, host, gatewaySelector, allow |
| AgentMeshTrustedBundle | Supply approved public CA certificates | caBundle |

AgentMeshExpose has **no serviceAccount field**. It preserves the backend
Service selector; selected pods can use different accounts. The allow list
identifies authenticated remote requesters at the gateway.

Read [AGENT-MESH.md](AGENT-MESH.md) for the complete setup guide, all three CR
examples, protocol meanings, trust updates, gateway prerequisites and migration
from the removed API. [examples.yaml](examples.yaml) is a copyable template;
replace placeholders and apply each declaration in its intended namespace.

Run [TESTING.md](TESTING.md) for a fresh two-cluster verification lab, including
independent CAs, DNS, a namespaced ingress gateway, real mTLS, negative controls
and cleanup. [VERIFICATION.md](VERIFICATION.md) records the tested source and results.

The controller generates Sidecar, ServiceEntry, DestinationRule subset,
VirtualService, EnvoyFilter and gateway authorization resources. MTLS means the
application sends HTTP and its sidecar originates TLS with its workload
certificate. HTTPS means application TLS. Egress enforcement applies to traffic
captured by the sidecar; it does not prevent capture bypass.

The runtime uses Python's standard library and namespace-only Kubernetes RBAC.
It does not read Secrets, share CA private keys, issue certificates, or deploy
gateway workloads. The namespace selects one public bundle for its MTLS paths.
