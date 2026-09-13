# Namespace-only installation, step by step

This installs **one Go controller in one existing application namespace**, using
ConfigMaps and a Role/RoleBinding. No AgentMesh CRD, Namespace, ClusterRole or
ClusterRoleBinding is installed. Existing Istio networking CRDs and sidecar
injection must already be available in the cluster.

Use this guide for a namespace owner who cannot install cluster-scoped resources.
The installer needs permission to create the local Deployment, ServiceAccount,
ConfigMaps, Role and RoleBinding and to delegate the listed Role permissions.
Installing local RBAC does not bypass Kubernetes privilege-escalation checks.
The controller runs with only the supplied namespace Role after installation.

## 1. Choose the namespace, context and image

Run from the repository root in Bash. Replace these values:

```sh
export KUBE_CONTEXT=your-dev-test-context
export APP_NS=example
export CONTROLLER_IMAGE=your-registry/mesh-access-controller:your-version
k() { kubectl --context "$KUBE_CONTEXT" -n "$APP_NS" "$@"; }
k get pods,services
```

The namespace must exist. Do not create or change the cluster's Istio installation
as part of these steps. Applications requiring enrollment must have an Istio
sidecar; native/restartable sidecars are supported. The controller itself is
explicitly excluded from injection by its Deployment annotation.

Use a published image built from this repository. To build it yourself:

```sh
python3 build.py
docker tag mesh-access-controller:dev "$CONTROLLER_IMAGE"
# Publish using your normal approved registry workflow, or load into your local lab.
```

`build.py` needs Python only on the build host. The image contains a static Go
binary with no Python interpreter or shell. Go is supplied by the Docker build
stage. Local unit tests use `go test -race ./...` and `go vet ./...`.

If a cluster-wide AgentMesh controller exists, its administrator must first add
this namespace to that controller's `forbiddenNamespaces`. Do not run overlapping
controllers. Existing AgentMesh installations need the migration procedure in
[AGENT-MESH.md](AGENT-MESH.md#upgrade-and-migration); this procedure is a fresh install.

## 2. Prepare and review the local installation manifest

The checked-in template uses the explicit namespace `example`. `kubectl -n` alone
does not rewrite those fields. This script rewrites every object namespace and
RoleBinding subject and supplies the chosen image:

```sh
# Host-only dependency if not already available: python3 -m pip install PyYAML==6.0.2
python3 - <<'PY'
import os
from pathlib import Path
import yaml
ns, image = os.environ['APP_NS'], os.environ['CONTROLLER_IMAGE']
resources = list(yaml.safe_load_all(Path('install-namespaced.yaml').read_text()))
for resource in resources:
    assert resource['kind'] in {'ServiceAccount', 'Role', 'RoleBinding', 'Deployment', 'ConfigMap'}
    resource['metadata']['namespace'] = ns
    for subject in resource.get('subjects', []):
        subject['namespace'] = ns
    if resource['kind'] == 'Deployment':
        container = resource['spec']['template']['spec']['containers'][0]
        container['image'] = image
        assert container['args'] == ['--mode=namespace']
Path('/tmp/agentmesh-namespace-install.yaml').write_text(yaml.safe_dump_all(resources, sort_keys=False))
PY
cat /tmp/agentmesh-namespace-install.yaml
k apply --dry-run=server -f /tmp/agentmesh-namespace-install.yaml
k apply -f /tmp/agentmesh-namespace-install.yaml
k rollout status deployment/mesh-access-controller --timeout=120s
k logs deployment/mesh-access-controller --tail=20
```

The deployment uses one replica and Recreate. There is no leader election.
Do not raise replicas. Initial declaration lists are empty, so the controller
creates no application policy and does not require a valid trust bundle yet.

| Installed object | Owner / use |
|---|---|
| Deployment and SA `mesh-access-controller` | Namespace installer; runs Go reconciliation |
| Role/RoleBinding `mesh-access-controller` | Namespace installer; local discovery and generated-policy writes |
| ConfigMap `mesh-access-config` | Administrator/installer; trusted-bundle selection, gateway defaults, restrictions |
| ConfigMap `mesh-access-trust` | Trust administrator; approved public certificates in `data.ca.crt` |
| ConfigMap `mesh-access-declarations` | Developer; `data.declarations.json` |
| ConfigMap `mesh-access-state` | Controller; status and ownership anchor, retain its UID |
| Role `agentmesh-developer` | Optional developer delegation; not bound automatically |

Do not repeatedly apply the whole initial manifest after developers add rules:
it contains empty declarations and placeholder trust. Manage those inputs
separately thereafter. The controller is allowed to PATCH only the state ConfigMap;
it cannot edit settings, declarations or trust.

## 3. Have the trust administrator publish the approved bundle

Plain HTTP/HTTPS/TCP egress can proceed without trust configuration. Before MTLS
or Expose, the administrator publishes the cluster's approved shared public roots
into this namespace. Obtain `approved-mesh-roots.pem` through your PKI/GitOps process:

```sh
k create configmap mesh-access-trust --from-file=ca.crt=approved-mesh-roots.pem \
  --dry-run=client -o yaml | k apply -f -
```

This is **public trust material only**. Do not copy CA private keys or gateway
server private keys. The bundle must contain all roots this matched traffic should
trust, including local roots when needed. It replaces the matched Envoy validation
CA set. Limit: 256 KiB PEM; repeated inline roots can also hit the 900 KiB generated
object limit. There is no mounted-file/SDS bundle implementation in this release.

The namespace controller does not read the cluster-scoped TrustedBundle CR and
does not synchronize namespace copies. The administrator distributes the same
approved bundle to participating namespaces. For rotation: distribute old+new
roots, rotate issuers/certificates, verify fresh connections, then retire old roots.
Istio/cert-manager do not automatically update these ConfigMaps.

## 4. Review administrator defaults

```sh
k get configmap mesh-access-config -o jsonpath='{.data.config\.json}'
```

Defaults select `mesh-access-trust` and gateway Service `ingressgateway`, Service
port `443`. Change gateway defaults to actual existing names if using Expose.
The optional exposurePolicy restricts DNS naming, for example:

```json
{
  "schemaVersion": 1,
  "forbiddenNamespaces": ["istio-system", "kube-system", "kube-public", "kube-node-lease"],
  "trustBundle": {"name": "mesh-access-trust"},
  "gateway": {"service": "ingressgateway", "port": 443},
  "exposurePolicy": {"dnsSuffix": "company.example.com", "labelSuffix": "-agent-mesh"}
}
```

Save the desired JSON to `agentmesh-config.json`, then publish only that ConfigMap:

```sh
k create configmap mesh-access-config --from-file=config.json=agentmesh-config.json \
  --dry-run=client -o yaml | k apply -f -
```

Do not put `credentialName` or authorization in this JSON. If you rename an input
ConfigMap, update the controller arguments/config and Role resourceNames together.

## 5. Optionally delegate declaration editing to developers

The supplied Role grants update/patch/get of the pre-created declarations ConfigMap
and read access to state. It grants no trust/config mutation or ConfigMap deletion.
For an existing Kubernetes-authenticated developer group:

```sh
k create rolebinding agentmesh-developers --role=agentmesh-developer \
  --group=your-namespace-developer-group --dry-run=client -o yaml | k apply -f -
```

The administrator role/process remains responsible for public trust. Kubernetes
RBAC is additive: a developer who already has unrestricted namespace access can
still edit ConfigMaps or workloads through other grants. Use RBAC/admission such
as OPA Gatekeeper to constrain that broader access if required. Admission policies
are outside this controller.

## 6. Start with a simple egress rule

Use an actual application ServiceAccount and existing Service/port. Example:

```sh
cat > agentmesh-declarations.json <<'JSON'
{
  "schemaVersion": 1,
  "egress": [{
    "name": "agent",
    "serviceAccount": "agent-client",
    "inCluster": [{"host": "orders", "port": 8080, "protocol": "HTTP"}],
    "outCluster": [{"host": "api.example.com", "port": 443, "protocol": "HTTPS"}]
  }],
  "expose": []
}
JSON
```

Publish it using PATCH, which works with the supplied developer Role and does not
require permission to create/delete ConfigMaps:

```sh
python3 - <<'PY'
import json
from pathlib import Path
text = Path('agentmesh-declarations.json').read_text()
json.loads(text)
Path('/tmp/agentmesh-declarations-patch.json').write_text(json.dumps({'data': {'declarations.json': text}}))
PY
k patch configmap mesh-access-declarations --type=merge \
  --patch-file=/tmp/agentmesh-declarations-patch.json
```

AgentMesh maps namespace+SA to pods automatically, then stamps supported workload
templates and waits for their normal rollout. Do not manually label live pods.
This can restart requester pods through Deployment/StatefulSet/DaemonSet rollout.
Jobs/bare pods and OnDelete strategies need owner-managed bootstrap/replacement.
Use one egress entry per SA. Empty inCluster/outCluster lists retain deny-all for
captured traffic; removing the entire SA entry unenrolls it instead.

## 7. Add cross-cluster MTLS when ready

Add to the requester's `outCluster` list and republish the JSON using step 6:

```json
{"host":"orders-agent-mesh.remote.example.com","port":443,"protocol":"MTLS"}
```

The requester application sends **HTTP** to
`http://orders-agent-mesh.remote.example.com:443`. Its sidecar originates mTLS,
using its workload certificate, exact remote DNS SAN and approved bundle.
AgentMesh generates ServiceEntry, subset DestinationRule, VirtualService and
requester trust EnvoyFilter, along with the egress whitelist controls.

DNS and routing must reach the remote gateway. If the network endpoint differs,
supply `"endpoint":"gateway-address.example.com:443"` (or an IPv4:port). That
changes network routing without changing logical host/SNI. The remote gateway
owner must supply listener, certificate and authorization; the remote namespace
must configure its approved trust and exposure as in the next step.

HTTPS means the application itself sends HTTPS. MTLS expects application HTTP.
TCP rules require explicit external IP addresses. For same-cluster ingress MTLS,
use a native HTTP-named Service plus `serverName`; see
[the full protocol guide](AGENT-MESH.md#same-cluster-gateway-mtls).

## 8. Expose an existing service: leave the gateway untouched

First ask the gateway owner to confirm existing configuration:

- Gateway workload and Service are healthy **in this namespace**.
- The supplied selector selects exactly the pods behind that Service.
- An exact-host HTTPS **MUTUAL** Gateway listener exists on the gateway Service port.
- A bound VirtualService routes that exact host to the declared backend Service/port.
- Server DNS certificate, SDS credentials/RBAC and native AuthorizationPolicy work.

AgentMesh does not create or repair these owner resources. In particular, do not
apply `fixtures/gateway-rbac.yaml` to an existing gateway; it is a disposable-lab fixture.

Add this entry to the `expose` list in your JSON and republish using step 6:

```json
{
  "name": "orders",
  "service": "orders",
  "port": 8080,
  "host": "orders-agent-mesh.company.example.com",
  "gatewaySelector": {"istio": "aspe-ingressgateway"},
  "gatewayService": "ingressgateway",
  "gatewayPort": 443
}
```

Expose has no serviceAccount, allow or credentialName. It creates only a trust
EnvoyFilter with exact SNI + resolved listener port. Service 443 -> targetPort
8443 means the filter matches **8443**. It preserves owner Gateway SAN/pin checks
and the existing client-certificate requirement. Other SNI HTTPS chains are not
matched by this filter. Custom SDS/filter combinations need separate review.

The gateway owner independently applies Istio AuthorizationPolicy. After gateway
TLS termination the backend sees the gateway SA; authorize the original requester
at the gateway. The full guide includes an
[owner-policy example](AGENT-MESH.md#developer-exposure-declaration-trust-only).

## 9. Verify the real result

```sh
k get configmap mesh-access-state -o jsonpath='{.data.status\.json}'
k logs deployment/mesh-access-controller --tail=100
k get sidecar,serviceentry,destinationrule,virtualservice,envoyfilter \
  -l mesh-access.example.com/managed-by=mesh-access-controller
```

State must report `configured:true` for the current declarations ConfigMap
resourceVersion. A malformed declaration or missing/invalid required trust bundle
reports failure and retains the previous valid configuration; it does not silently
revoke old permissions. Fix the input and verify actual fresh requests.

Required traffic checks: permitted target works; undeclared host/port fails;
MTLS trusted caller works; owner policy rejects a different actual SA; removing a
required CA makes fresh TLS fail; restoration succeeds; ordinary HTTPS on another
SNI continues to work. Inspect active Envoy configuration and verify the owner
gateway Deployment, listeners, routes, credentials and policies were not changed.
`configured:true` alone is not proof of xDS acceptance or successful traffic.

For disposable real-cluster verification, follow [TESTING.md](TESTING.md) and run
`python3 verify_namespace.py` against its fixed modern labs, or
`AGENT_MESH_LEGACY=1 python3 verify_namespace.py` on the preserved legacy pair.
These scripts create/clean isolated fixtures and are **not company-cluster installers**.
See [VERIFICATION.md](VERIFICATION.md) for tested builds and versions.

## 10. Update or uninstall safely

To remove selected permissions, edit the valid JSON and republish. To remove all
declarations, publish `{"schemaVersion":1,"egress":[],"expose":[]}`. Wait for
status, generated-resource pruning and requester template cleanup before removing
the installation. Removing an Egress entry unenrolls its SA; use an empty rule to
retain explicit captured deny-all.

Expose deletion removes only its trust filter, leaving owner gateway routes,
credentials and authorization in place. It does not close the listener. The gateway
owner controls withdrawing service exposure or closing existing connections.

After pruning, stop/remove this controller and its local RBAC, SA and ConfigMaps
through the namespace owner's normal workflow. Keep `mesh-access-state` until its
owned resources are gone. Do not delete the application namespace, Istio resources
or cluster CRDs as part of this controller's uninstall. Do not delete a legacy
ownership ConfigMap until old generated owner resources have been adopted.
