# Developer egress and cross-cluster exposure

Use `AgentMeshEgress` for one ServiceAccount's captured outbound traffic and
`AgentMeshExpose` to expose an HTTP backend through a selected ingress gateway.
`AgentMeshTrustedBundle` holds administrator-managed public CA trust shared across the cluster.
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
the source namespace's Istio configuration. The controller resolves these service
names through DNS; the resolver does not validate their Service specs through the API.
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
override the administrator's gateway defaults for that exposure. They must identify an
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

## Administrator installation and developer delegation

The cluster administrator installs the three CRDs and one controller per cluster.
AgentMeshTrustedBundle is cluster-scoped. AgentMeshEgress and AgentMeshExpose are
namespaced. The controller runs in `agentmesh-system`, with a ClusterRoleBinding
for discovery, generated resources, workload enrollment and CR status updates.
It can read bundles and patch their status, but cannot create, modify or delete
bundle specs. It cannot read Secrets. Upgrade the CRDs, RBAC and image together.
For an existing namespaced-bundle install, follow the migration section first.

```sh
kubectl apply -f crd.yaml
docker build -t YOUR_REGISTRY/mesh-access-controller:YOUR_TAG .
kubectl apply -f install.yaml
kubectl -n agentmesh-system set image deployment/mesh-access-controller controller=YOUR_REGISTRY/mesh-access-controller:YOUR_TAG
```

Publish or load that image through your existing image workflow before waiting
for the Deployment. `install.yaml`'s `:dev` tag is for the local lab. The image
must include both `controller.py` and `egress.py`; the Dockerfile handles this.

For plain egress only, no trusted bundle or gateway is required. For MTLS or
exposure, the administrator creates one cluster-scoped
`AgentMeshTrustedBundle/mesh-access-trust` containing the complete approved public PEM roots.
For exposure also provide the gateway Service/Deployment, its SDS Role
(`gateway-rbac.yaml`, adapted to the actual gateway SA), and the gateway TLS
credential Secret in the same namespace. The controller itself cannot read
Secrets. Server credential prerequisites are below.

Administrator defaults live in `agentmesh-system/mesh-access-config`, `data.config.json`:

```json
{
  "schemaVersion": 1,
  "forbiddenNamespaces": ["istio-system", "kube-system", "kube-public", "kube-node-lease"],
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

Install the image/config in a dedicated administrator-controlled namespace.
If renaming `agentmesh-system`, update the Namespace, every namespaced install
object and the ClusterRoleBinding subject. Application CRs in the controller's
own namespace are always refused. Add any custom Istio root namespace to
`forbiddenNamespaces`.

The administrator grants developers the supplied role through a **RoleBinding**
in each allowed application namespace:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: agentmesh-developers
  namespace: example
subjects:
- kind: Group
  name: example-developers
  apiGroup: rbac.authorization.k8s.io
roleRef:
  kind: ClusterRole
  name: agentmesh-developer
  apiGroup: rbac.authorization.k8s.io
```

This role grants management of Egress/Expose only. It grants no trust, workload,
Secret, generated Istio-resource or controller-configuration management. Existing
permissions are additive; audit other bindings if developers already have broader
access. The optional `agentmesh-trust-admin` ClusterRole can be delegated through
a ClusterRoleBinding to trusted administrators; no such binding is created by default.
Cluster-scoped resources require cluster-scoped permission grants; a namespace
RoleBinding does not grant them. See [Kubernetes RBAC](https://kubernetes.io/docs/reference/access-authn-authz/rbac/).

Developers apply their two CR kinds in their namespaces. For supported workload
controllers, AgentMesh finds pods by namespace plus `spec.serviceAccount`, stamps
workload templates and waits for rollout; no manual enrollment labels or local
controller install are needed. Expose selects the Service and gateway directly.

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
spec:
  caBundle: |
    -----BEGIN CERTIFICATE-----
    REPLACE_WITH_APPROVED_PUBLIC_CA_CERTIFICATE
    -----END CERTIFICATE-----
```

The example PEM is a placeholder; use real certificates. To create or update
the CR directly from an approved PEM file, this command needs only Python's
standard library and kubectl using administrator credentials (replace the context):

```sh
python3 - <<'PY' | kubectl --context CLUSTER_A apply -f -
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

Repeat once in cluster B with its administrator-approved bundle. All managed MTLS
requests and exposures within a cluster share the bundle named by the central
`agentmesh-system/mesh-access-config.data.config.json.trustBundle.name`; the default is
`mesh-access-trust`. This version does not select a different bundle per target.
Other TrustedBundle CRs can be staged, but are unused until selected by the
administrator setting. An invalid unused bundle reports `Configured=False` on that
bundle without blocking active declarations or their revocation. No ConfigMap CA fallback
is read. Bundle status describes public PEM validation only, not successful xDS
delivery or TLS. An application-namespace error does not change valid bundle status
or stop other namespaces from reconciling.

The controller creates a local `mesh-access-config` ConfigMap in each enrolled
namespace as an ownership anchor for generated resources. It carries no trust or
developer-editable settings. Existing ConfigMap UIDs are retained during migration;
their old `config.json` values are ignored. Only central administrator settings are
used. The anchor lets the controller find and clean a namespace after its final
declaration is deleted. Do not delete it while managed resources remain.

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
python3 -u verify_application.py
```

The second command uses only the dedicated modern lab contexts, creates fresh
namespaces and all three CRDs, and restores DNS/removes its fixtures in `finally`.
It refuses existing CRDs. Save `application-evidence/results.json` and active-config
snapshots before another run overwrites them. Require exit code zero plus
`complete` and `cleanup` results. Do not run this script against company contexts.

The standalone suite contains 38 behavior tests. The application suite includes
multi-namespace shared-trust/RBAC verification, JSON traffic, connection reuse,
rolling updates and unused-bundle revocation checks, plus the full live suite
below. `verify_agent_mesh.py` is a smaller protocol/mTLS-only check. See
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
AGENT_MESH_LEGACY=1 python3 -u verify_application.py
```

This explicitly uses kind-cluster-a/b and saves agent-legacy-evidence/. It assumes
the original Kubernetes 1.24.17 / Istio 1.13.5 lab prerequisites, not a company
cluster. The fresh-environment setup in TESTING.md targets the modern pair.

## Upgrade from namespaced trust and controllers

This is a breaking installation-scope change, even when the API group is already
`agentmesh.io`. Kubernetes makes an established CRD's scope immutable; applying
`crd.yaml` over a namespaced TrustedBundle CRD cannot convert it.
See [Kubernetes CRD update validation](https://github.com/kubernetes/apiextensions-apiserver/blob/master/pkg/apis/apiextensions/validation/validation.go).
The controller never deletes or migrates CRDs automatically.

Administrator procedure for an existing installation:

1. Inventory all participating namespaces and controller deployments. Save every
   Egress/Expose, every namespaced TrustedBundle, all local mesh-access-config
   ConfigMaps, the old CRD YAML, RBAC and controller image version. Use fully
   qualified resource names and `-A` when exporting namespaced resources.
2. Select the approved shared trust set. Do not blindly concatenate tenant bundles:
   an administrator must approve each issuer now trusted across managed namespaces.
   Save the selected public PEM independently. Review old namespace gateway defaults;
   move common values to the central config and express differing values using
   each Expose's gatewayService/gatewayPort/credentialName overrides.
3. Stop **all old namespace controllers** and wait for their pods to terminate.
   Preserve namespace ConfigMaps, generated resources, workloads and gateway
   credentials. The same ConfigMap UID is needed for existing resource ownership.
4. If the TrustedBundle CRD in the current group is Namespaced, export its CRs first,
   then delete **only** that CRD and recreate it from the current manifest. This
   deletes its stored namespaced bundles; it is an explicit administrator migration
   operation. Do not delete the namespaced Egress/Expose CRDs or the local ConfigMaps.
   Commands below assume backups and controller shutdown are complete:

   ```sh
   kubectl get agentmeshtrustedbundles.agentmesh.io -A -o yaml > old-namespaced-bundles.yaml
   kubectl get crd agentmeshtrustedbundles.agentmesh.io -o yaml > old-bundle-crd.yaml
   kubectl delete crd agentmeshtrustedbundles.agentmesh.io --wait=true
   kubectl apply -f crd.yaml
   kubectl wait --for=condition=Established crd/agentmeshtrustedbundles.agentmesh.io --timeout=60s
   ```

5. Create the one approved cluster bundle using the public-PEM command above.
   Its metadata must **omit namespace**. Preserve existing Egress/Expose specs.
   Use a fresh kubectl discovery cache after the scope change, for example
   `kubectl --cache-dir=/tmp/agentmesh-scope-migration ...`, if a client still sends
   requests to the old namespaced bundle endpoint.
6. Install the current ClusterRoles/ClusterRoleBinding, central ConfigMap and one
   controller using the installation section. Do not start old and new controllers
   together. Configure developer RoleBindings in their application namespaces.
   The controller adopts existing ownership anchors without changing their UID.
7. Verify current-generation Configured conditions, active Envoy configuration,
   positive and negative traffic, and shared-bundle propagation in two namespaces.
   Retire old namespace controller Deployments, SAs and Roles/RoleBindings after
   success. **Do not delete their retained mesh-access-config ownership anchors.**

If upgrading from `mesh-access.example.com` or the intermediate
`agentmesh.newtonguass.github.io` group, installing the new group creates distinct
resources. Export old-group CRs first. Copy only kind, desired spec, name and
namespace for Egress/Expose; change apiVersion to `agentmesh.io/v1alpha1`.
Build the administrator-approved cluster TrustedBundle separately, without namespace.
Do not copy resourceVersion, UID or status. The new controller watches only the
new group. Retire old-group CRDs only after every namespace has migrated and
rollback is no longer required; deleting a CRD deletes all of its stored CRs.

For the still older removed MeshAccess API, additionally convert request lists
to one AgentMeshEgress per requester SA, explicitly listing required local/plain
egress. Convert exposures to Service, port, host, gatewaySelector and allow;
remove backend serviceAccount. Only the three current CRDs should remain after
migration completes.

Rollback of the scope change also needs administrator action: stop the new
controller, restore the old namespaced bundle CRD and saved bundles (recreating
that CRD), restore old namespace settings/RBAC/image and then start the old
controllers. Keep the same local ConfigMap UIDs throughout. Existing-install
migration is documented but is not covered by the fresh-install traffic suite.

## Reconciliation and uninstall

One controller replica with Recreate strategy runs per cluster. It polls every
five seconds, obtains one shared trust snapshot and validates each namespace plan
before writes. A failed namespace does not stop later namespaces; changes across
namespaces are eventually applied, not atomic. It corrects owned drift and prunes
obsolete owned objects using resourceVersion/UID preconditions. Do not run multiple
active replicas: leader election is not implemented. Large-cluster capacity and
availability have not been benchmarked.

Namespace owners must reserve hostnames across namespaces; conflict checks remain
local to each namespace. Include a custom Istio root configuration namespace in
forbiddenNamespaces. The controller installation namespace is always forbidden
for application CRs; terminating namespaces are skipped.

Egress enrollment and gateway isolation labels are stamped into Deployment,
StatefulSet and DaemonSet templates, triggering normal rollout. Paused or OnDelete
workloads need owner-managed replacement. Jobs and bare pods require reserved
labels and the bootstrap annotation before proxy startup; use supported workload
controllers for the simple path. Backend templates are not enrolled by exposure.

To remove one namespace's participation, delete its Egress/Expose declarations
first and wait until objects labeled
mesh-access.example.com/managed-by=mesh-access-controller are pruned. Then remove
its local ownership ConfigMap if desired. Leave the shared bundle and controller
running for other namespaces. To uninstall the whole cluster, finish this cleanup
in every namespace first, then the administrator removes the installation,
ClusterRoles/ClusterRoleBinding and the three CRDs. Removing a CRD destroys its CRs;
deleting the shared bundle alone does not revoke existing Envoy trust.
