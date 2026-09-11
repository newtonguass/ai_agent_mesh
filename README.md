# AgentMesh: service-account egress and cross-cluster mTLS

The developer APIs are **AgentMeshEgress** (captured outbound host/port/protocol
whitelists) and **AgentMeshExpose** (HTTP service exposure through a developer
selected gateway, with explicit requester identities and CA trust).

Start with [AGENT-MESH.md](AGENT-MESH.md) for the CR examples, installation,
protocol meanings, namespace trust configuration and enforcement boundaries.
Use [TESTING.md](TESTING.md) to create the two-cluster lab, then run
`python3 -u verify_agent_mesh.py` for the new APIs. The existing MeshAccess API
below is retained for compatibility; it does not itself enable an egress whitelist.

The runtime remains namespaced and standard-library-only. It uses scoped Sidecar
configuration plus outbound HTTP/TCP RBAC EnvoyFilters; this controls captured
traffic and is not a defense against bypassing the sidecar.

Both the new APIs and their negative traffic controls passed on Kubernetes
1.34.0 / Istio 1.31.0 and Kubernetes 1.24.17 / Istio 1.13.5. See
[VERIFICATION.md](VERIFICATION.md) for the completed results and exact source hashes.

## Developer declarations

The namespace owner installs the controller and trust configuration once.
Developers then declare their ServiceAccount's destinations and their exposed
backend. The allowed principal uses the requester's actual Istio trust domain.

```yaml
apiVersion: mesh-access.example.com/v1alpha1
kind: AgentMeshEgress
metadata:
  name: agent
spec:
  serviceAccount: agent-client
  inCluster:
  - host: orders
    port: 8080
    protocol: HTTP
  outCluster:
  - host: orders-agent-mesh.remote.example.com
    port: 443
    protocol: MTLS
  - host: api.example.com
    port: 443
    protocol: HTTPS
---
apiVersion: mesh-access.example.com/v1alpha1
kind: AgentMeshExpose
metadata:
  name: orders
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

Apply the examples in the appropriate application namespace after replacing the
sample names. Full protocol, gateway and lifecycle details are in
[AGENT-MESH.md](AGENT-MESH.md).

## Existing MeshAccess API

This is a small namespace-owned controller for **Istio sidecar meshes** and
independent cluster CAs. A namespace owner installs one controller in each
participating namespace. A platform administrator installs the CRD once per
cluster. The controller runtime uses Python's standard library, with no package
downloads, Kubernetes CLI, CA private keys, or cluster-wide RBAC.

The implementation is in `controller.py`; install manifests are `crd.yaml` and
`install.yaml`. This file is the complete operating guide for an offline agent.
The `v1alpha1` API is specific to this project, not an upstream Istio API.

Run the commands in this guide from the repository root. The controller, CRD,
installation manifests, Dockerfile, and tests are all in this directory.

**New to the project?** Follow [TESTING.md](TESTING.md) for the complete fresh-host
verification lab: exact tools, two clusters, independent CAs, traffic assertions,
troubleshooting, and cleanup.

## Verified version combinations

| Kubernetes | Istio | Proxy placement | Live result |
|---|---|---|---|
| 1.24.17 | 1.13.5 | Regular application sidecar containers | Passed |
| 1.34.0 | 1.31.0 | Native requester/backend sidecars; regular gateway container | Passed |

The modern two-cluster test uses independent real Istio CAs, DNS-based requests,
and a terminating ingress gateway in the backend namespace. It verifies successful
mTLS, wrong-SA rejection, rejection after either CA is removed, restored trust,
authorization updates without a gateway rollout, ordinary HTTPS on the same
listener, unrelated local traffic, RBAC, drift correction, and deletion.

**Compatibility fix:** native sidecars put istio-proxy in spec.initContainers
with restartPolicy: Always. The controller now detects these as well as regular
containers, and resolves named gateway ports from both. A finite init container
does not count as a running proxy. No CRD or generated trust-patch changes were
needed. Rebuild the controller image from this source; an older image lacks this
fix. Fifteen behavior tests include both native and regular container cases.

These are verified combinations, not a promise for every Kubernetes/Istio
release. Ambient mesh remains outside this controller's scope. Modern lab
commands, exact versions, source hash, initial failure and final evidence are in
[compatibility/README.md](compatibility/README.md). The modern integration test
reuses the same full traffic contract as the legacy test.

## 1. The simple declaration

On cluster A, in the requester's namespace:

```yaml
apiVersion: mesh-access.example.com/v1alpha1
kind: MeshAccess
metadata:
  name: agent
spec:
  serviceAccount: agent-client
  requests:
  - url: https://orders.remote.example.com
```

On cluster B, in the namespace containing the backend **and its ingress gateway**:

```yaml
apiVersion: mesh-access.example.com/v1alpha1
kind: MeshAccess
metadata:
  name: orders
spec:
  serviceAccount: orders
  exposes:
  - service: orders
    url: https://orders.remote.example.com
    allow:
    - cluster-a-mesh/ns/agent-ns/sa/agent-client
```

**Verified on real Istio 1.13.5 clusters:** authorized caller 200, wrong SA 403,
CA removal failures on both sides, ordinary HTTPS/local traffic preserved,
automatic reconciliation and deletion. The full standalone suite now contains 25 behavior tests.

The namespace and account in `allow` describe the **requester certificate's
identity**. Use the actual trust domain, not the Kubernetes context or cluster
name. Omit the `spiffe://` prefix. Wildcards are deliberately rejected.

Use one MeshAccess per ServiceAccount. A resource can contain both `requests`
and `exposes`, and each list can have multiple entries. Exposed Service pods must
use that ServiceAccount. Requesting accounts in the same namespace can share a
remote URL; the controller combines their routing rules.

**Application request for the example:** `http://orders.remote.example.com:443/`.
The CR's HTTPS URL identifies the remote TLS endpoint. The application sends
HTTP to its local sidecar, which originates mTLS. Application HTTPS, arbitrary
TCP, and TLS passthrough are not implemented by this controller. Do not point
an application HTTPS client at a generated HTTP ServiceEntry and expect it to
work. `status.applicationURLs` gives the HTTP origins to use.

Two optional details cover common setups:

```yaml
spec:
  serviceAccount: agent-client
  requests:
  - url: https://orders.remote.example.com
    endpoint: ingress.remote.example.com:443
```

`endpoint` overrides the network destination, while SNI and the required DNS SAN
remain `orders.remote.example.com`. It accepts a DNS hostname or IPv4 plus a
port. It is useful for a gateway address or lab NodePort. Application DNS must
still resolve the URL hostname; the controller does not create DNS records.

```yaml
spec:
  serviceAccount: orders
  exposes:
  - service: orders
    port: 8080
    url: https://orders.remote.example.com
    allow:
    - cluster-a-mesh/ns/agent-ns/sa/agent-client
```

`exposes.port` is the **existing Service port**, not its pod's listening port.
Omit it if the Service has exactly one port. Backend applications must accept HTTP.
URLs are origins only: no paths, queries, credentials, wildcard hosts, or IP SANs.
This version permits one requested port/endpoint per hostname per namespace.

## 2. What is configured once per namespace

1. Istio sidecar injection and existing application ServiceAccounts/workloads.
2. `mesh-access-trust`, a ConfigMap containing a complete public CA bundle.
3. `mesh-access-config`, the namespace controller settings.
4. For exposure: an existing ingress gateway Deployment and Service **in this
   namespace**, plus its TLS credential Secret. A requester-only namespace does
   not need a gateway or credential Secret.

Namespace settings are in `install.yaml`:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: mesh-access-config
data:
  config.json: |
    {
      "schemaVersion": 1,
      "forbiddenNamespaces": ["istio-system"],
      "trustBundle": {"name": "mesh-access-trust", "key": "ca.crt"},
      "gateway": {
        "service": "ingressgateway",
        "port": 443,
        "credentialName": "mesh-access-gateway-tls"
      }
    }
```

Set `forbiddenNamespaces` to include the actual Istio mesh root configuration
namespace if it differs from `istio-system`. Do not operate this tenant controller
in that root namespace. Namespace-only RBAC cannot stop root-namespace EnvoyFilter
semantics from extending beyond the tenant.

`gateway.port` is the gateway **Service port**. The controller resolves its
`targetPort` and uses the resulting actual Envoy listener port in the generated
SNI+port filter. Numeric and consistently named target ports are supported.
For Service 443 -> targetPort 8443 -> NodePort 31543, set `gateway.port: 443`;
the filter matches 8443 and an external requester may use endpoint port 31543.
Externally advertised URL ports do not create or change gateway Service ports.

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

## 3. Install

From this directory, a platform administrator installs the CRD in each cluster:

```sh
kubectl --context CLUSTER_A apply -f crd.yaml
kubectl --context CLUSTER_B apply -f crd.yaml
```

Build the image and publish it to your company registry, or transport it to your
offline nodes using the existing image distribution process:

```sh
docker build -t YOUR_REGISTRY/mesh-access-controller:v0.1 .
docker push YOUR_REGISTRY/mesh-access-controller:v0.1
```

Edit the image in `install.yaml` to that exact reference and edit its namespace
settings. Use one edited copy per namespace when gateways differ. The namespace
owner then applies it with an explicit context and namespace:

```sh
kubectl --context CLUSTER_A -n agent-ns create configmap mesh-access-trust \
  --from-file=ca.crt=approved-mesh-roots.pem --dry-run=client -o yaml | \
  kubectl --context CLUSTER_A -n agent-ns apply -f -
kubectl --context CLUSTER_A -n agent-ns apply -f install.yaml
kubectl --context CLUSTER_A -n agent-ns rollout status deploy/mesh-access-controller
```

Repeat for the destination namespace in cluster B, with its gateway configured.
Use your existing namespace; the installation does not create it. Kubernetes
namespaced RoleBindings resolve the omitted ServiceAccount subject namespace to
the RoleBinding's namespace. The runtime is restricted to the namespace obtained
from its own pod's downward API.

Apply each MeshAccess in the intended namespace. `examples.yaml` contains both
examples for reference: **do not apply that whole file to one namespace/cluster**.
Copy the applicable object into your source or destination manifest.

```sh
kubectl --context CLUSTER_A -n agent-ns apply -f requester.yaml
kubectl --context CLUSTER_B -n orders-ns apply -f exposed-service.yaml
kubectl --context CLUSTER_A -n agent-ns get meshaccess
kubectl --context CLUSTER_B -n orders-ns get meshaccess
```

The Deployment uses one replica and `Recreate`. There is no leader election.
Do not install competing controllers or increase its replica count.

## 4. Generated resources and traffic isolation

| Declaration | Generated configuration |
|---|---|
| Each remote hostname | Namespace-local ServiceEntry, DestinationRule with `mesh-access-mtls` subset, and VirtualService |
| Each requester SA | EnvoyFilter with per-host subset trust patches; bootstrap labels on its workload templates |
| Each exposed hostname | Alias backend Service, backend DestinationRule using ISTIO_MUTUAL, MUTUAL Gateway, gateway VirtualService, SNI+listener-port trust EnvoyFilter, host-scoped DENY AuthorizationPolicy |

The requester DestinationRule intentionally has **no workloadSelector**, which
is unavailable in Istio 1.13.5. Its mTLS settings apply only to the named subset.
The VirtualService selects declared source pod labels and routes those requests
into the subset. Its fallback route uses the default destination without this
mTLS policy. Unrelated hosts retain their existing configuration. The declared hostname/port
is reserved for HTTP-to-mTLS origination; application HTTPS to that same origin
is not covered by the noninterference claim.

The controller maps `spec.serviceAccountName` to a reserved pod label
`mesh-access.example.com/service-account`. It stamps labels and a bootstrap annotation on matching Deployment,
StatefulSet and DaemonSet pod templates. **Initial enrollment triggers their
normal rollout** so Istio sees the labels when the proxy starts. Removing the
last declaration for an account removes these template fields and can also
roll the workload. Later URL, CA, and authorization changes do not change the
template and do not require a rollout.

This is required by the real Istio 1.13.5 test: patching labels on an already
running pod did not activate the proxy selectors. The controller waits with
Configured=False while existing pods lack the expected bootstrap stamp.
Paused Deployments and OnDelete StatefulSets/DaemonSets require the owner to
complete replacement. Jobs, CronJobs and bare pods are not automatically
enrolled in this first version; they require labels and the annotation present
before proxy startup. Use supported workload controllers for the simple path.
The controller does not modify volumes or application containers. Wait and retry
traffic during rollout/xDS propagation; it is not an admission webhook or an
egress security boundary. Source labels choose routing; **destination certificate validation
and AuthorizationPolicy perform authentication and authorization**.

The inline requester patch replaces Envoy's validation-context oneof, so it
**explicitly reinstates the exact remote hostname SAN in every target patch**.
It leaves the ISTIO_MUTUAL client certificate SDS reference untouched. This
differs from the earlier shared mounted-file proof of concept, which changed
only the root SDS name and inherited per-target SAN checks. Do not simplify this
inline patch to a subset-only patch that drops SAN validation.

The controller uses a namespace-unique gateway pod label in addition to the
gateway Service selector. This constrains old Istio Gateway selection behavior
without requiring a mesh-wide selector setting. Gateway EnvoyFilters use the
same labels, **SNI + actual listener port**, and no `transportProtocol: tls`
condition. The tested Istio 1.13.5 terminating chains lack that match field.

Each backend gets a separate generated ClusterIP Service selecting its original
labels plus the expected SA label. The original Service and backend policies
are not modified. Gateway-to-backend TLS uses native local ISTIO_MUTUAL.
If the backend has a restrictive AuthorizationPolicy, it must already allow
the namespace gateway's identity. It sees the **gateway SA**, not the original
requester. Backend sidecars and local mesh trust are prerequisites.

The gateway DENY policy rejects callers outside `exposes.allow` only for that
HTTP hostname (including its port-qualified Host header). A DENY policy is used
to avoid making an ALLOW policy that denies unrelated HTTPS hosts by default.
Existing mesh/namespace/gateway ALLOW and DENY policies still apply and can
further restrict requests. This implementation is for HTTP/HTTPS gateways;
do not reuse its HTTP-host DENY policy on a mixed raw-TCP gateway without review
of Istio's missing-attribute DENY behavior.

ServiceEntry, DestinationRule and VirtualService use `exportTo: ["."]`.
Generated Services carry the equivalent Istio export annotation. Generated
objects have deterministic names, a managed label, and an owner reference to
`mesh-access-config`. The controller refuses to overwrite unowned names and
rejects overlapping unmanaged ServiceEntry/VirtualService/DestinationRule/Gateway
hosts found **in its namespace**, including leading wildcard overlaps.
Platform owners must reserve tenant hostnames and prevent cross-namespace host
conflicts; the namespace controller cannot inspect or police other namespaces.

## 5. CA and certificate updates

Maintain the complete public CA bundle in `mesh-access-trust.data["ca.crt"]`.
It is used for both remote gateway validation and exposed-chain requester
validation. Include all intended remote and local roots. Update it with the same
ConfigMap command used at install; within the reconciliation interval the
controller replaces generated inline trust. No volume mount or pod restart is
required. Gateway server certificates continue to come from credentialName SDS.

Use an overlapping old+new root bundle during a planned root rotation, then
remove the old root after issuers/workloads have migrated. The controller does
not discover roots, synchronize clusters, bind one CA to one trust domain, or
decide when an old root is safe to remove. Anyone controlling a trusted signing
CA can issue an identity accepted by this trust model; exact SA/SAN matching
does not cryptographically bind a CA to a trust domain.

This implementation caps the public bundle at **256 KiB** and each generated
object at **900 KiB**, rejecting larger plans. Inline trust repeats per target;
large installations should add the previously tested mounted-file/SDS profile.
These size guards are implementation limits, not a universal safe CA count.
Inbound TLS CertificateRequest CA-name limits still apply. Files do not remove
that protocol limit.

## 6. Reconciliation, deletion and failure handling

The controller polls the namespace every five seconds and retries transient API
failures. It uses resourceVersion/UID checks for mutations, does not force
ownership of manual objects, and replaces generated specs so removed CAs,
principals and destinations actually disappear. Projected API tokens are read
on every request so token rotation does not require restarting the controller.

Deleting a MeshAccess removes its routes and trust filter, and shared objects
remain while another declaration references them. Deleting the final resource
prunes all generated objects and reserved workload-template labels while leaving original
Services, workloads and Secrets intact. The config ConfigMap owns shared
resources; **keep it until ordinary cleanup has completed**.

The full namespace input is validated before mutation. Invalid/conflicting input
marks MeshAccess `Configured=False` and retains the last applied configuration.
Transient failures can also leave a partially updated plan until the next retry;
Kubernetes and xDS updates are not atomic. Do not mistake an invalid edit or
`Configured=False` for immediate access revocation. Correct the invalid input
and check generated policies, or perform an explicit gateway policy revocation
under the namespace owner's control. Existing pooled TLS/HTTP connections may
outlive a trust change; validate changes using fresh connections.

Uninstall in this order:

```sh
kubectl --context CLUSTER_A -n agent-ns delete meshaccess --all
kubectl --context CLUSTER_A -n agent-ns get service,serviceentry,destinationrule,virtualservice,envoyfilter,gateway,authorizationpolicy \
  -l mesh-access.example.com/managed-by=mesh-access-controller
```

Wait until the generated list is empty, then delete that namespace's installation
using its edited `install.yaml`. Keep or remove the public trust ConfigMap as
desired. Remove the cluster-wide CRD only after every namespace has uninstalled;
deleting it affects all namespaces.

## 7. Required checks and troubleshooting

`Configured=True` means the controller reconciled Kubernetes resources. It does
**not** mean Envoy ACKed them, TLS succeeded, or the backend is healthy.

```sh
kubectl --context CLUSTER_A -n agent-ns get meshaccess agent -o yaml
kubectl --context CLUSTER_A -n agent-ns logs deploy/mesh-access-controller --tail=100
kubectl --context CLUSTER_A -n agent-ns get pods --show-labels
kubectl --context CLUSTER_A -n agent-ns get serviceentry,destinationrule,virtualservice,envoyfilter \
  -l mesh-access.example.com/managed-by=mesh-access-controller -o yaml
```

Inspect the active source Envoy config using `pilot-agent request GET config_dump`
inside `istio-proxy`. For every target expect:

- `outbound|443|mesh-access-mtls|orders.remote.example.com` (adapt URL port).
- SNI and exact SAN `orders.remote.example.com`.
- Exactly one client certificate SDS entry named `default`.
- Inline approved bundle in `common_tls_context.validation_context`.
- Actual source route selects the subset only for declared requester pods.

At the gateway expect the actual listener, correct SNI filter chain,
`require_client_certificate: true`, complete inline requester roots, and the
original single credential server-certificate SDS entry. Confirm ordinary HTTPS
hosts use separate chains with their original client-authentication settings.
Inspect rejected/warming configurations if the intended filter is not active.

Then test a real request, an unauthorized real SA, missing source CA, missing
destination CA, and ordinary local/HTTPS traffic. Use fresh requester connections
and restore every negative control. Check gateway/backend policy separately:
gateway rejects unauthorized original SA; backend permits gateway SA.

If source DNS resolves the remote hostname to a **local Kubernetes Service
ClusterIP**, Istio may select that native Service cluster instead of the custom
ServiceEntry. Same-cluster gateway requests need a rule on the actual gateway
Service FQDN, as established by the earlier PoC. This controller's requester
path is deliberately for remote gateways; it does not implement that local case.

## 8. Tests and evidence

Run standalone behavior tests (Python 3.12 and an OS CA store):

```sh
python3 -m unittest discover -s . -p test_controller.py -v
```

The real-cluster test needs PyYAML, kubectl, Docker and OpenSSL on the test host,
and the preserved **kind-cluster-a / kind-cluster-b** fixtures with Istio 1.13.5.
It explicitly refuses preexisting test namespaces or this CRD, uses
fresh `mesh-access-poc-<timestamp>` namespaces to avoid stale SDS authorization
cache entries between retries, signs a temporary gateway certificate using the real B CA,
temporarily adds DNS records, and cleans its fixtures in `finally`.
It must not be run unchanged in company clusters.

```sh
docker start cluster-a-control-plane cluster-b-control-plane
docker build -t mesh-access-controller:dev .
docker save mesh-access-controller:dev | docker exec -i cluster-a-control-plane ctr --namespace k8s.io images import -
docker save mesh-access-controller:dev | docker exec -i cluster-b-control-plane ctr --namespace k8s.io images import -
python3 integration_test.py
docker stop cluster-a-control-plane cluster-b-control-plane
```

The image-stream commands avoid this environment's kind-loader temporary-path
problem. Controller images remain local; nothing is published by the lab test.
`evidence/results.json` and active configs hold the latest completed run's
results. No Secret objects or private signing keys are saved as evidence.

Design references, optional for an offline agent: Istio 1.13.5
[VirtualService API](https://github.com/istio/api/blob/1.13.5/networking/v1alpha3/virtual_service.proto),
[AuthorizationPolicy API](https://github.com/istio/api/blob/1.13.5/security/v1beta1/authorization_policy.proto),
[Istio 1.13.5 SDS authorization source](https://github.com/istio/istio/blob/1.13.5/pilot/pkg/credentials/kube/secrets.go),
and Kubernetes [CRD/status API](https://kubernetes.io/docs/tasks/extend-kubernetes/custom-resources/custom-resource-definitions/).

The same updated controller source also passed the complete legacy regression
on Kubernetes 1.24.17 / Istio 1.13.5 on 2026-09-11. Both legacy controller pods'
source hashes matched the modern run. Legacy evidence is saved in
`evidence/results.json` and `evidence/legacy-regression-versions.json` in the
original lab workspace (generated artifacts are excluded from Git).
