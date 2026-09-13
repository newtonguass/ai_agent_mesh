# Developer egress and cross-cluster exposure

Use `AgentMeshEgress` for one ServiceAccount's captured outbound traffic and
`AgentMeshExpose` to expose an HTTP backend through a selected ingress gateway.
`AgentMeshTrustedBundle` holds the public CA trust used by that namespace.
These are the only three custom resource types supported by the controller.

## One declaration for the requester

```yaml
apiVersion: agentmesh.io/v1alpha1
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
HTTP authority and destination port must both match. DNS authority matching is
case-insensitive but otherwise exact, including an explicit port when supplied.
A URL using a Service's
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
apiVersion: agentmesh.io/v1alpha1
kind: AgentMeshExpose
metadata:
  name: orders
  namespace: example
spec:
  service: orders
  port: 8080
  host: orders-agent-mesh.company.example.com
  gatewaySelector:
    istio: aspe-ingressgateway
  allow:
  - remote-mesh/ns/example/sa/agent-client
```

`service` is an existing HTTP backend Service in this namespace. `port` is its
Service port, not the backend pod port. Its selected pods must have Istio sidecars.
There is no `serviceAccount` field on exposure: the generated alias Service
preserves the original Service selector, including backends with different
ServiceAccounts. Exposure does not enroll or relabel backend workloads.
`allow` lists exact authenticated
requester principals: trust-domain/ns/namespace/sa/account, without `spiffe://`.
The host must be covered by the gateway server certificate's DNS SANs.

The supplied gateway selector is used for generated Gateway, trust EnvoyFilter
and authorization selection, augmented by a namespace isolation label. It must
select exactly the pods behind the configured gateway Service, in the same
namespace. The controller rejects an empty, unmatched or inconsistent selector.
It resolves gateway Service targetPort, including named ports, and matches the
trust patch by **actual listener port + exact SNI**. It does not patch every TLS
chain on the selected gateway.

Multiple exposure CRs can share a backend Service or gateway. For another gateway in
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
in their namespace. The installation grants namespaced permissions for all three
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

For plain egress only, no trusted bundle or gateway is required. For MTLS or
exposure, create `AgentMeshTrustedBundle/mesh-access-trust` in each participating
namespace with `spec.caBundle` containing the complete approved public PEM roots.
For exposure also provide the gateway Service/Deployment, its SDS Role
(`gateway-rbac.yaml`, adapted to the actual gateway SA), and the gateway TLS
credential Secret in the same namespace. The controller itself cannot read
Secrets. Server credential prerequisites are below.

Namespace defaults live in `mesh-access-config`, `data.config.json`:

```json
{
  "schemaVersion": 1,
  "forbiddenNamespaces": ["istio-system"],
  "trustBundle": {"name": "mesh-access-trust"},
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
planes. Update the TrustedBundle CR with the complete desired bundle; filters
reconcile from it. Issuer/server-certificate rotation remains the owner's job.

### Public trust bundle

```yaml
apiVersion: agentmesh.io/v1alpha1
kind: AgentMeshTrustedBundle
metadata:
  name: mesh-access-trust
  namespace: example
spec:
  caBundle: |
    -----BEGIN CERTIFICATE-----
    REPLACE_WITH_APPROVED_PUBLIC_CA_CERTIFICATE
    -----END CERTIFICATE-----
```

The example PEM is a placeholder; use real certificates. To create or update
the CR directly from an approved PEM file, this command needs only Python's
standard library and kubectl (replace the context and namespace):

```sh
python3 - <<'PY' | kubectl --context CLUSTER_A -n example apply -f -
import json
from pathlib import Path
print(json.dumps({
    "apiVersion": "agentmesh.io/v1alpha1",
    "kind": "AgentMeshTrustedBundle",
    "metadata": {"name": "mesh-access-trust"},
    "spec": {"caBundle": Path("approved-mesh-roots.pem").read_text()}
}))
PY
```

Repeat in the destination namespace with its approved bundle. All MTLS requests
and exposures in one namespace share the bundle named by
`mesh-access-config.data.config.json.trustBundle.name`; the default is
`mesh-access-trust`. This version does not select a different bundle per target.
Other TrustedBundle CRs can be staged, but are unused until selected by the
namespace setting. An invalid unused bundle reports `Configured=False` on that
bundle without blocking active declarations or their revocation. No ConfigMap CA fallback
is read. The normal config ConfigMap remains namespace settings and the owner of
generated resources; it is not another CRD.

Bundles are limited to 256 KiB and generated objects to 900 KiB. Only public PEM
certificates are accepted. Include local roots as well when local gateway MTLS
needs them. During planned CA rotation, distribute old+new roots, migrate
issuers/certificates, verify fresh connections, then remove old roots. Updates
replace the generated inline validation context and preserve certificate SDS;
they do not require a gateway restart. Existing connections can outlive updates.
The controller does not discover, rotate, or synchronize CA certificates.

If an active MTLS declaration or exposure requires the selected bundle, a missing
or invalid selected bundle makes reconciliation fail and retain the last good
configuration. Plain egress alone does not require valid trust. Deleting a bundle is therefore **not** a way to revoke existing
access. Update the allow list or egress destinations with a valid declaration
and verify fresh connections. A trusted issuer can mint identities accepted by
this model; a principal string does not bind a particular CA to a trust domain.

### Gateway server credentials

The gateway Secret must contain a server certificate/key covering every exposed
hostname, for example a certificate with explicit DNS SANs for all those hostnames:

- `tls.crt`: gateway server certificate and necessary intermediate chain.
- `tls.key`: its matching private key.
- `ca.crt`: keep a valid issuer/controller-provided value as required by your
  Istio MUTUAL credential setup. Client trust for generated exposed chains is
  replaced by the EnvoyFilter's separately managed bundle.

The gateway's own ServiceAccount also needs its normal Istio credential/SDS
permission to read namespace Secrets. The new-namespace live test initially
failed with a warming credential and an Istiod `not authorized to read secrets`
warning because that role was missing. For a gateway without this permission,
adapt the ServiceAccount name in `gateway-rbac.yaml` and apply it in the gateway
namespace. It grants `get/list secrets` to the gateway account, **not the controller**.
Existing gateway Helm installations may already provide this permission.
Istio 1.13.5 specifically performs a namespace-wide `list secrets`
SubjectAccessReview; `get` alone is insufficient. Its source caches denied checks
for one minute and allowed checks for five minutes. An RBAC fix may need the
denial cache to expire and a new SDS request before the certificate appears.
A resourceNames-only permission does not satisfy that namespace-wide list check.
Keep gateway credentials and their RBAC within the namespace owner's boundary.

Certificate issuance and renewal belong to your existing certificate owner.
The controller does not read, create, modify, or renew Secrets. It also does not
create a gateway Deployment, LoadBalancer, NodePort, external DNS record, Istio
control plane, or ServiceAccount for an application.

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

The standalone suite contains 32 behavior tests. For JSON application traffic,
connection reuse, rolling updates and unused-bundle revocation checks, use
`verify_application.py` instead; it includes the full live suite below. See
[APPLICATION-VERIFICATION.md](APPLICATION-VERIFICATION.md).

The live tests cover declared protocols, undeclared hosts and ports, direct-IP HTTP
attempts, isolation between SAs, mixed HTTPS/MTLS on 443, empty/revoked/restored
whitelists, real independent-CA failures, gateway authorization, reconciliation
and pruning. They also change the backend ServiceAccount without changing the
exposure CR, and check that no backend SA label is required. Bundle removal and
restoration tests update AgentMeshTrustedBundle directly on both sides.

For the preserved legacy lab pair, stop the modern pair first, start cluster-a/b,
load the same controller image into both nodes, and run:

```sh
AGENT_MESH_LEGACY=1 python3 -u verify_agent_mesh.py
```

This explicitly uses kind-cluster-a/b and saves agent-legacy-evidence/. It assumes
the original Kubernetes 1.24.17 / Istio 1.13.5 lab prerequisites, not a company
cluster. The fresh-environment setup in TESTING.md targets the modern pair.

## Migrate from the placeholder API group

All three CRDs now use `agentmesh.io/v1alpha1`. The API group uses the project
name; `v1alpha1` still reflects the API's maturity. Resource kinds and specs are
unchanged. These steps also apply to the intermediate API group introduced in
commit `f87398e`: set `OLD_API_GROUP=agentmesh.newtonguass.github.io` for that
installation, instead of the original placeholder shown below.

Changing the group creates distinct Kubernetes resources. Applying the new CRD
manifest does not rename or migrate stored CRs. The controller watches only the
new group. For an existing installation, migrate each participating namespace:

1. Save its declarations from the old group and its `mesh-access-config` ConfigMap.
   Use explicitly qualified resource names while both groups exist:

   ```sh
   TEAM_NS=your-namespace
   OLD_API_GROUP=mesh-access.example.com
   kubectl -n "$TEAM_NS" get "agentmeshegresses.$OLD_API_GROUP,agentmeshexposes.$OLD_API_GROUP,agentmeshtrustedbundles.$OLD_API_GROUP" -o json > old-agentmesh.json
   kubectl -n "$TEAM_NS" get configmap mesh-access-config -o yaml > old-agentmesh-config.yaml
   kubectl -n "$TEAM_NS" scale deployment mesh-access-controller --replicas=0
   ```

2. Wait for the old controller pod to terminate. Keep the config ConfigMap,
   generated Istio resources and gateway credentials in place. Keep the old
   controller image available for rollback.
3. Install the new `crd.yaml`, then convert and apply all saved declarations
   before starting the new controller. This copies only desired resource data,
   without stale resourceVersion, UID or status:

   ```sh
   kubectl apply -f crd.yaml
   python3 - <<'PY' > new-agentmesh.json
   import json
   with open('old-agentmesh.json') as f:
       saved = json.load(f)
   kinds = {'AgentMeshEgress', 'AgentMeshExpose', 'AgentMeshTrustedBundle'}
   assert saved['items'] and all(x['kind'] in kinds for x in saved['items'])
   print(json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': [
       {'apiVersion': 'agentmesh.io/v1alpha1', 'kind': x['kind'],
        'metadata': {'name': x['metadata']['name'], 'namespace': x['metadata']['namespace']},
        'spec': x['spec']} for x in saved['items']]}))
   PY
   kubectl apply -f new-agentmesh.json
   ```

4. Update RBAC and deploy the newly built controller image using `install.yaml`.
   Preserve the existing namespace settings when applying it: its sample ConfigMap
   is not a replacement for your configured gateway/bundle settings. Do not run
   old and new controllers concurrently. Update developer RBAC, GitOps manifests
   and any admission policies that reference the old API group.
5. Verify new-group Configured status, active Envoy configuration, permitted and
   denied traffic. The controller keeps existing internal label/annotation keys
   under `mesh-access.example.com` and the existing ConfigMap ownership, so this
   API rename alone does not require pod relabeling or a rollout. Users still do
   not add enrollment labels manually.
6. After **every namespace** has migrated and rollback is no longer needed, the
   platform owner may remove the three old-group CRDs. Deleting a CRD destroys
   all its stored CRs cluster-wide; the controller never does this automatically.
   The installed current API contains only the three new-group CRDs.

For rollback before retiring the old group, stop the new controller, ensure the
old-group declarations reflect the desired current configuration, restore its
old Role/image and then start it. Keep the same config ConfigMap. Do not delete
the namespace installation to switch versions.

## Upgrade from the removed API

This historical API-shape cleanup also requires the group migration above when
upgrading to the current release. The controller no longer watches `MeshAccess`;
applying the new `crd.yaml` does not delete a previously installed CRD.

1. Save the existing declarations and namespace settings. Pause the old namespace
   controller by scaling it to zero while preserving its configuration and generated
   objects. Do not delete the config ConfigMap.
2. Apply the new three-CRD manifest. Convert each old request list to one
   AgentMeshEgress per requester SA, specifying host, port and protocol MTLS.
   Explicitly include required local/plain egress destinations: the new API
   enforces a whitelist. Convert each exposure to AgentMeshExpose with Service,
   port, host, gatewaySelector and allow; omit the backend serviceAccount.
   If already using AgentMeshEgress/Expose, keep egress declarations and remove
   spec.serviceAccount from every exposure manifest before reapplying it.
3. Copy the complete public CA PEM from the previous trust ConfigMap into the
   selected AgentMeshTrustedBundle. Keep `trustBundle.name` or set the new name;
   remove the obsolete `trustBundle.key` setting. Apply all declarations before
   starting the new controller image with the updated namespace Role.
4. Verify Configured status, active Envoy config, successful and rejected traffic.
   Old backend enrollment labels may be removed in a migration rollout. Subsequent
   exposure changes do not enroll backend SAs. Existing generated objects retain
   the same config ConfigMap ownership and can be reconciled in place.
5. After **every namespace** using the old API is migrated and verified, the
   platform owner can delete `meshaccesses.mesh-access.example.com` and retire
   unused old trust ConfigMaps. CRD deletion destroys its stored CRs across the
   cluster. The controller never performs this cluster-wide deletion.

## Reconciliation and uninstall

One controller replica with Recreate strategy runs per namespace. It polls every
five seconds, validates the namespace plan before writes, corrects owned drift,
and prunes obsolete owned objects. Updates use resourceVersion/UID preconditions.
Namespace owners must reserve hostnames across namespaces; this controller only
checks conflicts inside its own namespace. Do not install it in the Istio root
configuration namespace; include a custom root namespace in forbiddenNamespaces.

Egress enrollment and gateway isolation labels are stamped into Deployment,
StatefulSet and DaemonSet templates, triggering normal rollout. Paused or OnDelete
workloads need owner-managed replacement. Jobs and bare pods require the reserved
labels and bootstrap annotation before proxy startup; use supported workload
controllers for the simple path. Backend templates are not enrolled by exposure.

To uninstall, delete AgentMeshEgress and AgentMeshExpose declarations first,
wait until objects labeled mesh-access.example.com/managed-by=mesh-access-controller
are pruned, then remove the namespace installation and TrustedBundle CRs. Keep
mesh-access-config until pruning finishes. Remove the three cluster-wide CRDs
only after all participating namespaces have uninstalled.
