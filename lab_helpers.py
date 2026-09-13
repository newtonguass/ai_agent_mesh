"""Host-only test constants. No controller/reconciliation code executes in Python."""
import hashlib
GROUP = 'agentmesh.io'
VERSION = GROUP + '/v1alpha1'
METADATA_GROUP = 'mesh-access.example.com'
LABEL = METADATA_GROUP + '/service-account'
GW_LABEL = METADATA_GROUP + '/gateway'
STAMP = METADATA_GROUP + '/bootstrap-labels'
MANAGED = METADATA_GROUP + '/managed-by'
STATE = METADATA_GROUP + '/namespace-state'
MANAGER = 'mesh-access-controller'
SUBSET = 'mesh-access-mtls'
NET = 'networking.istio.io/v1alpha3'
SEC = 'security.istio.io/v1beta1'
CLUSTER_KINDS = {'Namespace', 'AgentMeshTrustedBundle'}
def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()[:16]
def name(prefix, value):
    return 'ma-' + prefix + '-' + digest(value)
def sa_label(sa):
    return 'sa-' + digest(sa)
