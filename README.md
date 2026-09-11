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

## Architecture

Each participating namespace runs its own controller. Developers declare egress
and exposure; the namespace owner supplies approved public roots. Dashed arrows
below show configuration, not application traffic.

```mermaid
flowchart TB
    subgraph requester["Cluster A · requester namespace"]
        E["AgentMeshEgress<br/>ServiceAccount → host, port, protocol"]
        TA["AgentMeshTrustedBundle<br/>Approved public roots, including gateway CA B"]
        CA["Namespace controller A"]
        OUT["Sidecar + outbound RBAC EnvoyFilters<br/>MTLS: DestinationRule subset + VirtualService<br/>Remote MTLS: ServiceEntry + trust EnvoyFilter"]
        E -.-> CA
        TA -.-> CA
        CA -.-> OUT
    end
    subgraph destination["Cluster B · gateway and backend namespace"]
        X["AgentMeshExpose<br/>Service, port, host SNI, gatewaySelector, allow"]
        TB["AgentMeshTrustedBundle<br/>Approved public roots, including requester CA A"]
        CB["Namespace controller B"]
        IN["MUTUAL Gateway + VirtualService<br/>SNI and listener-port trust EnvoyFilter<br/>Requester AuthorizationPolicy<br/>Backend alias Service + DestinationRule"]
        X -.-> CB
        TB -.-> CB
        CB -.-> IN
    end
```

The namespace ConfigMap selects one TrustedBundle by name and supplies gateway
defaults. Each controller manages Kubernetes/Istio configuration; Istiod delivers
it to Envoy. A namespace can contain both egress and exposure declarations.
Gateway workloads, application Services, DNS and certificate issuance already
exist; the controllers do not create them. The generated backend alias Service
preserves the exposed Service's selector, with no backend ServiceAccount binding.

## Cross-cluster request

This is the `MTLS` protocol path. The gateway terminates the requester's TLS and
opens a separate local mesh mTLS connection to the backend.

```mermaid
flowchart LR
    subgraph A["Cluster A · independent CA A"]
        subgraph P["Requester pod · selected ServiceAccount"]
            APP["Application"]
            SRC["Istio sidecar<br/>Egress whitelist<br/>Trust gateway CA B<br/>Check exact DNS SAN"]
            APP -->|"HTTP to exposed DNS name:443"| SRC
        end
    end
    subgraph B["Cluster B · independent CA B"]
        GW["Selected ingress gateway<br/>SNI + listener-port trust patch<br/>Trust requester CA A<br/>Authorize original requester principal"]
        ALIAS["Generated backend alias Service<br/>Original Service selector"]
        subgraph BP["Backend pod · no SA binding in AgentMeshExpose"]
            DST["Istio sidecar"]
            BACKEND["HTTP application"]
            DST -->|"HTTP"| BACKEND
        end
        GW -->|"Separate local mesh mTLS"| DST
        ALIAS -.->|"Selects backend endpoints"| DST
        GW -.->|"VirtualService routes via alias"| ALIAS
    end
    SRC ==>|"mTLS · SNI = exposed host<br/>Client identity = requester SA"| GW
```

The gateway checks the original requester identity. The backend authenticates
the gateway's identity, so any restrictive backend policy must allow the gateway
SA. Each mesh retains its own CA private keys; only public trust is exchanged.
Gateway server certificates come from its existing `credentialName` Secret via
SDS. The trust EnvoyFilter preserves that certificate configuration.

For other egress protocols, `HTTP` uses HTTP routing, `HTTPS` carries application
TLS with SNI/port checks, and `TCP` grants explicit destination IPs/ports. These
whitelist rules apply to sidecar-captured traffic. See [AGENT-MESH.md](AGENT-MESH.md)
for complete protocol and setup details.
