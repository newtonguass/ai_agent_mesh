#!/usr/bin/env python3
"""Real two-cluster ConfigMap-mode traffic with no AgentMesh CRDs or cluster RBAC."""
import copy
import json
import os
import pathlib
os.environ['AGENT_MESH_NAMESPACE'] = '1'
import verify_agent_mesh as v
t, c = v.t, v.c
t.E = pathlib.Path(__file__).resolve().parent / ('namespace-legacy-evidence' if os.environ.get('AGENT_MESH_LEGACY') == '1' else 'namespace-evidence')
t.E.mkdir(exist_ok=True)

def scope_checks():
    for side in ['a', 'b']:
        assert not t.k(side, 'get', 'crd', *[n + '.' + c.GROUP for n in ['agentmeshegresses', 'agentmeshexposes', 'agentmeshtrustedbundles']], '--ignore-not-found', '-o', 'name').strip()
        for kind in ['clusterrole', 'clusterrolebinding']:
            assert not t.k(side, 'get', kind, c.MANAGER, '--ignore-not-found', '-o', 'name').strip()
        checks = {}
        for verb, resource, ns, expected in [
                ('list', 'pods', t.NS, 'yes'), ('list', 'pods', 'kube-system', 'no'),
                ('get', 'configmap/mesh-access-trust', t.NS, 'yes'),
                ('patch', 'configmap/mesh-access-trust', t.NS, 'no'),
                ('patch', 'configmap/mesh-access-config', t.NS, 'no'),
                ('patch', 'configmap/mesh-access-declarations', t.NS, 'no'),
                ('patch', 'configmap/mesh-access-state', t.NS, 'yes'),
                ('list', 'secrets', t.NS, 'no'),
                ('patch', 'authorizationpolicies.security.istio.io', t.NS, 'no')]:
            result = t.k(side, 'auth', 'can-i', verb, resource,
                '--as=system:serviceaccount:' + t.NS + ':mesh-access-controller', ns=ns, check=False).strip()
            assert result.splitlines()[0] == expected, (verb, resource, ns, result)
            checks[verb + ' ' + resource + ' in ' + ns] = expected
        t.record(side + ' namespace-only RBAC without AgentMesh CRDs', checks)
    t.apply('a', t.obj('ServiceAccount', 'developer'))
    t.apply('a', {'apiVersion':'rbac.authorization.k8s.io/v1','kind':'RoleBinding',
        'metadata':{'name':'developer','namespace':t.NS},
        'subjects':[{'kind':'ServiceAccount','name':'developer','namespace':t.NS}],
        'roleRef':{'apiGroup':'rbac.authorization.k8s.io','kind':'Role','name':'agentmesh-developer'}})
    for resource, expected in [('configmap/mesh-access-declarations','yes'), ('configmap/mesh-access-trust','no'), ('configmap/mesh-access-config','no')]:
        answer=t.k('a','auth','can-i','patch',resource,'--as=system:serviceaccount:'+t.NS+':developer',check=False).strip()
        assert answer.splitlines()[0]==expected,answer
    t.record('developer edits declarations but not administrator trust/settings', True)

def invalid_retention():
    source = t.get('a', 'configmap', 'mesh-access-declarations')
    before = t.get('a', 'destinationrule', c.name('remote', t.HOST))['spec']
    try:
        t.k('a', 'patch', 'configmap', 'mesh-access-declarations', '--type=merge', '--patch-file=/dev/stdin',
            data=json.dumps({'data': {'declarations.json': '{"schemaVersion":1,"egress":[{"name":"bad","serviceAccount":"missing"}]}'}}))
        t.wait('invalid ConfigMap reports failure', lambda: not json.loads(t.get('a','configmap','mesh-access-state')['data']['status.json'])['configured'])
        assert t.get('a','destinationrule',c.name('remote',t.HOST))['spec']==before
        t.expect('malformed namespace plan retains prior working mTLS', 'caller', 200)
    finally:
        t.k('a','patch','configmap','mesh-access-declarations','--type=merge','--patch-file=/dev/stdin',data=json.dumps({'data':source['data']}))
        t.configured('a',['caller','denied'])

if __name__ == '__main__':
    try:
        for side in ['a','b']:
            t.record(side+' Kubernetes version',json.loads(t.k(side,'get','--raw=/version'))['gitVersion'])
            t.record(side+' Istiod image',t.get(side,'deployment','istiod','istio-system')['spec']['template']['spec']['containers'][0]['image'])
        roots, expose = t.setup()
        scope_checks()
        invalid_retention()
        v.extras(roots)
        v.local_gateway(roots, expose)
        t.test(roots, expose)
        t.record('complete', 'Namespace ConfigMaps, local RBAC, all protocols, independent-CA mTLS, owner authorization and pruning passed.')
    finally:
        for side in t.CREATED_NAMESPACES:
            (t.E/(side+'-controller.log')).write_text(t.k(side,'logs','deploy/mesh-access-controller','--tail=100',check=False))
            (t.E/(side+'-state.json')).write_text(t.k(side,'get','configmap','mesh-access-state','-o','json',check=False))
        t.cleanup()
