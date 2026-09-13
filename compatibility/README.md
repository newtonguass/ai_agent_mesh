# Kubernetes 1.34 compatibility lab

Follow [TESTING.md](../TESTING.md) from the repository root for exact tools,
cluster creation, independent Istio installations, image loading and cleanup.
The modern contexts are kind-mesh-access134-a/b in
/tmp/mesh-access-k134.config (override with MESH_ACCESS_KUBECONFIG).

Run `python3 -u verify_application.py` from the root for shared cluster trust,
multi-namespace RBAC, JSON traffic, whitelist and mTLS verification. Evidence goes
to application-evidence/. `verify_agent_mesh.py` is the smaller protocol-only suite.
`python3 -u compatibility/verify.py` runs the smaller shared mTLS contract and
records native-sidecar placement and Envoy version in compatibility/evidence/.
Both use the same current APIs. Run only one verification at a time.

The lab manifests pin Kubernetes 1.34.0 and Istio 1.31.0. The preserved legacy
pair tests Kubernetes 1.24.17 and Istio 1.13.5 separately. No other version
combination is implied. See [VERIFICATION.md](../VERIFICATION.md) for current
source hashes and completed results.
