"""Additional live multi-namespace and RBAC contract used by application suite."""
import copy
import json


def exercise(t, roots, expose):
    c = t.ctl
    second = t.NS + '-second'
    created = False
    try:
        for side in ['a', 'b']:
            resources = json.loads(t.k(side, 'get', '--raw=/apis/' + c.VERSION))['resources']
            scopes = {r['kind']: r['namespaced'] for r in resources if '/' not in r['name']}
            assert scopes == {'AgentMeshEgress': True, 'AgentMeshExpose': True, 'AgentMeshTrustedBundle': False}, scopes
            t.record(side + ' API resource scopes', scopes)
        assert not t.k('a', 'get', 'namespace', second, '--ignore-not-found', '-o', 'name').strip()
        t.apply('a', {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {
            'name': second, 'labels': {'istio-injection': 'enabled'}}})
        created = True
        for resource in [t.obj('ServiceAccount', 'caller'),
                         t.workload('caller', 'curlimages/curl:8.10.1', ['sleep', 'infinity']),
                         t.obj('AgentMeshEgress', 'caller', t.get('a', 'agentmeshegress', 'caller')['spec'], api=c.VERSION)]:
            resource['metadata']['namespace'] = second
            if resource['kind'] == 'AgentMeshEgress':
                resource['spec'].pop('inCluster', None)
            t.k('a', 'apply', '-f', '-', ns=second, data=json.dumps(resource))
        def configured():
            item = t.get('a', 'agentmeshegress', 'caller', ns=second)
            return any(x['status'] == 'True' and x['observedGeneration'] == item['metadata']['generation']
                       for x in item.get('status', {}).get('conditions', []))
        t.wait('second namespace automatically enrolled', configured)
        t.k('a', 'rollout', 'status', 'deploy/caller', '--timeout=120s', ns=second)
        allowed = copy.deepcopy(expose)
        allowed['spec']['allow'].append('cluster-a-mesh/ns/' + second + '/sa/caller')
        t.apply('b', allowed)
        t.configured('b', ['backend'])
        def expect(code):
            def request():
                value = t.k('a', 'exec', 'deploy/caller', '-c', 'curl', '--', 'curl', '-sS', '--max-time', '6',
                    '-w', '\nHTTP %{http_code}\n', 'http://' + t.HOST + ':443/', ns=second, check=False)
                return value if 'HTTP ' + str(code) in value else None
            t.record('second namespace cross-cluster mTLS ' + str(code), t.wait('second namespace HTTP ' + str(code), request))
        expect(200)
        t.expect('first namespace unchanged by second enrollment', 'caller', 200)
        for ns in [t.NS, second]:
            anchor = t.get('a', 'configmap', 'mesh-access-config', ns=ns)
            assert 'config.json' not in anchor['data']
            assert anchor['metadata']['labels'][c.STATE] == c.MANAGER
        t.record('one central config; automatic namespace ownership anchors', True)
        # Real API authorizer checks: developer delegation is limited to one namespace.
        for name in ['developer', 'trust-admin']:
            t.apply('a', t.obj('ServiceAccount', name))
        binding = {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'RoleBinding',
            'metadata': {'name': 'agentmesh-developer', 'namespace': t.NS},
            'subjects': [{'kind': 'ServiceAccount', 'name': 'developer', 'namespace': t.NS}],
            'roleRef': {'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'ClusterRole', 'name': 'agentmesh-developer'}}
        t.apply('a', binding)
        def can(sa, verb, resource, ns, expected):
            answer = t.k('a', 'auth', 'can-i', verb, resource,
                '--as=system:serviceaccount:' + t.NS + ':' + sa, ns=ns, check=False).strip()
            assert answer.splitlines()[0] == expected, (verb, resource, ns, answer)
            return expected
        checks = {}
        for verb, resource, ns, expected in [
            ('create', 'agentmeshegresses.' + c.GROUP, t.NS, 'yes'),
            ('create', 'agentmeshexposes.' + c.GROUP, t.NS, 'yes'),
            ('create', 'agentmeshegresses.' + c.GROUP, second, 'no'),
            ('patch', 'agentmeshtrustedbundles.' + c.GROUP, t.NS, 'no'),
            ('patch', 'configmaps', t.CTL_NS, 'no')]:
            checks[verb + ' ' + resource + ' in ' + ns] = can('developer', verb, resource, ns, expected)
        t.record('developer namespace delegation and administrator trust boundary', checks)
        # One administrator update must reach both enrolled namespaces.
        t.bundle('a', roots['a'])
        for ns in [t.NS, second]:
            t.wait('shared bundle update in ' + ns, lambda ns=ns: roots['b'] not in json.dumps(
                t.get('a', 'envoyfilter', c.name('requester', 'caller'), ns=ns)).replace('\\n', '\n'))
        t.fresh()
        t.k('a', 'rollout', 'restart', 'deploy/caller', ns=second)
        t.k('a', 'rollout', 'status', 'deploy/caller', '--timeout=120s', ns=second)
        t.expect('shared CA removal rejects first namespace', 'caller', 503)
        expect(503)
        t.bundle('a', roots['a'] + roots['b'])
        expect(200)
        t.expect('shared CA restoration restores first namespace', 'caller', 200)
        t.k('a', 'delete', 'agentmeshegress', 'caller', ns=second)
        t.wait('last declaration pruned through namespace anchor', lambda: not json.loads(t.k('a', 'get',
            'sidecar,serviceentry,destinationrule,virtualservice,envoyfilter', '-l', c.MANAGED + '=' + c.MANAGER,
            '-o', 'json', ns=second))['items'])
        t.expect('second namespace deletion preserves first namespace', 'caller', 200)
        t.record('cluster scope contract complete', True)
    finally:
        t.bundle('a', roots['a'] + roots['b'])
        t.apply('b', expose)
        if created:
            t.k('a', 'delete', 'namespace', second, '--wait=true', '--timeout=90s')
