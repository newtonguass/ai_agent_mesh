# Developer egress and cross-cluster exposure

Use `AgentMeshEgress` for one ServiceAccount's captured outbound traffic and
`AgentMeshExpose` to expose an HTTP backend through a selected ingress gateway.
The original `MeshAccess` API remains available for existing installations.
Do not combine the old and new APIs for the same ServiceAccount.

## One declaration for the requester

```yaml
apiVersion: mesh-access.example.com/v1alpha1
kind: AgentMeshEgress
metadata:
  name: agent
  namespace: example
spec:
  serviceAccount: agent-client
  inCluster:
  - host: orders
    port: 8080
    protocol: HTTP
  - host: database.other-team.svc.cluster.local
    port: 3306
    protocol: TCP
  outCluster:
  - host: orders-agent-mesh.remote.example.com
    port: 443
    protocol: MTLS
  - host: api.example.com
    port: 443
    protocol: HTTPS
  - host: www.example.com
    port: 80
    protocol: HTTP
  - host: database.example.com
    port: 3306
    protocol: TCP
    addresses: [192.0.2.10]
```

Replace example names, addresses and ports before applying. One egress CR per
ServiceAccount is allowed. All pods using an enrolled ServiceAccount in the
namespace must have sidecars; give unmeshed jobs or fixtures a separate account. An empty `inCluster`/`outCluster` pair blocks captured
application egress after configuration converges. Deleting the CR **unenrolls**
the account and removes these restrictions; use an empty list to revoke all
destinations while retaining the guard.

Internal short names refer to the declaration's namespace. Cross-namespace
names must be full `service.namespace.svc.cluster.local` names and exported into
the source namespace's Istio configuration. The controller resolves other
namespaces' service names through DNS without cross-namespace Kubernetes RBAC.
Same-namespace entries validate the actual Service port and require ClusterIP
Services. Cross-namespace service existence/port/protocol must also be checked
with real traffic because the controller cannot read their Service API objects.

For HTTP, use the short local Service name or its standard Service DNS aliases.
HTTP authority and destination port must both match. A URL using a Service's
numeric IP is not an HTTP hostname grant. Opaque TCP entries explicitly grant
their address and port; their payload is not interpreted as HTTP.

| Protocol | Application sends | Sidecar behavior |
|---|---|---|
| HTTP | Plain HTTP | HTTP routing; local mesh transport can still use automatic mTLS |
| HTTPS | Application TLS | Passes TLS; requires matching SNI and port; application validates the server certificate |
| TCP | Opaque TCP | Allows the destination IP and port |
| MTLS | Plain HTTP | Originates workload-certificate mTLS and checks the gateway DNS SAN |

Protocol names are case insensitive for the spellings accepted in the CRD.
`MTLS` retains the existing HTTP-to-mTLS contract. For the remote example the
application calls **`http://orders-agent-mesh.remote.example.com:443/`**.
`HTTPS` means the application itself calls `https://api.example.com/`.
The returned `status.applicationURLs` records the appropriate origin schemes.

External TCP requires explicit individual `addresses`, not CIDRs. This avoids a
hostname declaration becoming a wildcard listener for every IP on that port.
The client must resolve the hostname to a declared IP or connect to that IP.
Dynamic external TCP DNS/VIP allocation is not implemented. TCP entries do not
validate a peer hostname or certificate. DNS changes require updating addresses.

For HTTP, HTTPS and MTLS, optional `endpoint: gateway-address:port` changes the
upstream network address while retaining the host and TLS identity. This is
useful for a NodePort lab. It does not create application DNS records. Internal
entries do not accept endpoint overrides. External definitions of a hostname
must agree across accounts; this version permits one external protocol/port/
endpoint definition per hostname per namespace. Internal plain protocol entries
can allow multiple ports of the same Service.

## One declaration for the destination

```yaml
apiVersion: mesh-access.example.com/v1alpha1
kind: AgentMeshExpose
metadata:
  name: orders
  namespace: example
spec:
  serviceAccount: orders
  service: orders
  port: 8080
  host: orders-agent-mesh.company.example.com
  gatewaySelector:
    istio: aspe-ingressgateway
  allow:
  - remote-mesh/ns/example/sa/agent-client
```

`service` is an existing HTTP backend Service in this namespace. `port` is its
Service port, not the backend pod port. Its selected pods must use the declared
ServiceAccount and have Istio sidecars. `allow` lists exact authenticated
requester principals: trust-domain/ns/namespace/sa/account, without `spiffe://`.
The host must be covered by the gateway server certificate's DNS SANs.

The supplied gateway selector is used for generated Gateway, trust EnvoyFilter
and authorization selection, augmented by a namespace isolation label. It must
select exactly the pods behind the configured gateway Service, in the same
namespace. The controller rejects an empty, unmatched or inconsistent selector.
It resolves gateway Service targetPort, including named ports, and matches the
trust patch by **actual listener port + exact SNI**. It does not patch every TLS
chain on the selected gateway.

Multiple exposure CRs can share a backend SA or gateway. For another gateway in
the namespace, optional `gatewayService`, `gatewayPort` and `credentialName`
override the namespace defaults for that exposure. They must identify an
existing gateway and server certificate Secret; the controller does not issue
certificates or read Secret contents.

The gateway requires client certificates (`MUTUAL`) and rejects requesters not
in `allow`. The gateway then originates ordinary local mesh mTLS to a generated
alias Service for the backend. Existing backend authorization must allow the
gateway's SA. The backend's TLS peer is the gateway, not the original caller.

## Local-cluster gateway MTLS

For a native gateway Service, declare an internal destination with the gateway's
certificate/exposed host as serverName:

```yaml
spec:
  serviceAccount: agent-client
  inCluster:
  - host: local-gateway.example.svc.cluster.local
    port: 443
    protocol: MTLS
    serverName: orders-agent-mesh.company.example.com
```

The app sends HTTP to local-gateway:443. The subset originates TLS with the
declared serverName, and the VirtualService rewrites HTTP authority to that name.
No ServiceEntry is created for this native Service.

**The native Service port must be HTTP-named**, for example
name: http-origination, port: 443, targetPort: 8443. An existing HTTPS-named
gateway Service declares opaque TLS, so its outbound listener cannot perform
this HTTP routing/rewrite. Create a separate ClusterIP Service selecting the
same gateway pods with an HTTP-named port; retain the original HTTPS Service.
The controller validates this for same-namespace Services. For another
namespace, the owner must verify its port naming because the controller uses
DNS rather than cross-namespace Service API access.

## Namespace owner installation and trust

The platform owner applies `crd.yaml` once per cluster; it now contains three
CRDs. Each namespace owner builds/deploys the image and applies `install.yaml`
in their namespace. The installation grants namespaced permissions for both new
APIs, Sidecar resources and the existing generated objects. Upgrade the CRDs,
Role and image together; the new controller expects all three APIs to be served.

```sh
kubectl apply -f crd.yaml
docker build -t YOUR_REGISTRY/mesh-access-controller:YOUR_TAG .
kubectl -n YOUR_NAMESPACE apply -f install.yaml
kubectl -n YOUR_NAMESPACE set image deployment/mesh-access-controller controller=YOUR_REGISTRY/mesh-access-controller:YOUR_TAG
```

Publish or load that image through your existing image workflow before waiting
for the Deployment. `install.yaml`'s `:dev` tag is for the local lab. The image
must include both `controller.py` and `egress.py`; the Dockerfile handles this.

For plain egress only, no CA ConfigMap or gateway is required. For MTLS or
exposure, provide `mesh-access-trust` with `data.ca.crt` containing approved public
roots. For exposure also provide the gateway Service/Deployment, its SDS Role
(`gateway-rbac.yaml`, adapted to the actual gateway SA), and the gateway TLS
credential Secret in the same namespace. The controller itself cannot read
Secrets. See the root README's namespace prerequisites for server credentials.

Namespace defaults live in `mesh-access-config`, `data.config.json`:

```json
{
  "schemaVersion": 1,
  "forbiddenNamespaces": ["istio-system"],
  "trustBundle": {"name": "mesh-access-trust", "key": "ca.crt"},
  "gateway": {
    "service": "ingressgateway",
    "port": 443,
    "credentialName": "mesh-access-gateway-tls"
  },
  "exposurePolicy": {
    "dnsSuffix": "company.example.com",
    "labelSuffix": "-agent-mesh"
  }
}
```

`exposurePolicy` is optional. This example requires names such as
`orders-agent-mesh.company.example.com`. Removing the policy allows any exact
valid DNS host covered by the gateway certificate. Naming is not authorization;
the `allow` list and approved CA bundle remain necessary.

Exchange **public CA certificates only** between independent meshes. Each side
retains its own issuer and private keys. This is explicit service connectivity
with mutual trust, not automatic remote discovery or merging Istio control
planes. Update the trust ConfigMap with the complete desired bundle; filters
reconcile from it. Issuer/server-certificate rotation remains the owner's job.

## What enforces the whitelist

The controller stamps SA-based selectors into workload templates and rolls
enrolled Deployments/StatefulSets/DaemonSets according to their update strategy.
It generates a scoped `Sidecar` with `REGISTRY_ONLY`, plus two outbound RBAC
filters: HTTP authority+port rules before the router, and SNI+port/IP+port rules
before TCP proxy filters. The RBAC checks are needed because importing a
multi-port Service must not implicitly grant every port on that Service.

These controls apply to traffic captured by the sidecar. They do not prevent a
workload from bypassing capture, changing privileged networking, or using an
excluded port/interface. DNS/control-plane traffic is not presented as a
developer application whitelist destination. Strong isolation requires platform
network enforcement. See [Istio's security guidance](https://istio.io/latest/docs/ops/best-practices/security/).

An initial enrollment is not instantaneous: old pods retain their prior policy
until rollout/configuration converges. `Configured=False` reports pending
rollout or validation errors. Invalid updates retain the last successful plan;
they do not revoke its permissions. There is no admission gate that prevents
application startup until policy is installed. Updates are not transactional.
Revocation tests use fresh connections; immediate termination of established
TCP streams is not promised. Deleting an egress CR explicitly removes enrollment.

An unmanaged selector-specific Sidecar overlapping enrolled pods is rejected.
A selector-free namespace default can coexist and is superseded for the selected
workload by Istio. Other manual route/host conflicts are rejected by the existing
controller collision checks. Avoid independent controllers owning the same SA.

## Verification

Run the host/cluster setup in [TESTING.md](TESTING.md), then from the repository
root execute:

```sh
python3 -m unittest discover -s . -p 'test_*.py' -v
python3 -u verify_agent_mesh.py
```

The second command uses only the dedicated modern lab contexts, creates fresh
namespaces and all three CRDs, and restores DNS/removes its fixtures in `finally`.
It refuses existing CRDs. Save `agent-evidence/results.json` and active-config
snapshots before another run overwrites them. Require exit code zero plus
`complete` and `cleanup` results. Do not run this script against company contexts.

The standalone suite contains 25 behavior tests.

The live tests cover declared protocols, undeclared hosts and ports, direct-IP HTTP
attempts, isolation between SAs, mixed HTTPS/MTLS on 443, empty/revoked/restored
whitelists, real independent-CA failures, gateway authorization, reconciliation
and pruning. The old compatibility evidence proves the old API; new API version
claims must refer to a completed new-API run, not merely the old tests.

For the preserved legacy lab pair, stop the modern pair first, start cluster-a/b,
load the same controller image into both nodes, and run:

```sh
AGENT_MESH_LEGACY=1 python3 -u verify_agent_mesh.py
```

This explicitly uses kind-cluster-a/b and saves agent-legacy-evidence/. It assumes
the original Kubernetes 1.24.17 / Istio 1.13.5 lab prerequisites, not a company
cluster. The fresh-environment setup in TESTING.md targets the modern pair.
