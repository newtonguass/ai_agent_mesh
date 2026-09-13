#!/usr/bin/env python3
"""Verify the public API rename on the dedicated modern labs from TESTING.md."""
import hashlib
import json
import pathlib

import verify_agent_mesh as v

t, c = v.t, v.c
t.E = pathlib.Path(__file__).resolve().parent / 'api-group-evidence'
t.E.mkdir(exist_ok=True)


if __name__ == '__main__':
    try:
        roots, expose = t.setup()
        for side in ['a', 'b']:
            t.record(side + ' Kubernetes version', json.loads(t.k(side, 'get', '--raw=/version'))['gitVersion'])
            t.record(side + ' Istiod image', t.get(side, 'deployment', 'istiod', 'istio-system')['spec']['template']['spec']['containers'][0]['image'])
            discovered = json.loads(t.k(side, 'get', '--raw=/apis/' + c.VERSION))
            kinds = {r['kind'] for r in discovered['resources'] if '/' not in r['name']}
            assert {r['kind']: r['namespaced'] for r in discovered['resources'] if '/' not in r['name']} == {
                'AgentMeshEgress': True, 'AgentMeshExpose': True, 'AgentMeshTrustedBundle': False}
            assert discovered['groupVersion'] == c.VERSION
            assert kinds == {'AgentMeshEgress', 'AgentMeshExpose', 'AgentMeshTrustedBundle'}, kinds
            t.record(side + ' API discovery', {'groupVersion': c.VERSION, 'kinds': sorted(kinds)})
            for kind in kinds:
                items = json.loads(t.k(side, 'get', c.KINDS[kind][1] + '.' + c.GROUP, '-o', 'json'))['items']
                for item in items:
                    assert item['apiVersion'] == c.VERSION
                    assert any(x['type'] == 'Configured' and x['status'] == 'True' and
                               x['observedGeneration'] == item['metadata']['generation']
                               for x in item.get('status', {}).get('conditions', [])), item['metadata']['name']
            for module in ['controller.py', 'egress.py']:
                expected = hashlib.sha256((t.HERE / module).read_bytes()).hexdigest()
                code = 'import hashlib; print(hashlib.sha256(open(' + repr('/app/' + module) + ',"rb").read()).hexdigest())'
                actual = t.k(side, 'exec', 'deploy/mesh-access-controller', '--', 'python3', '-c', code).strip()
                assert actual == expected, (module, actual, expected)
                t.record(side + ' deployed ' + module + ' hash', actual)
        t.expect('new API authorized cross-cluster mTLS', 'caller', 200)
        t.expect('new API unauthorized SA rejected', 'denied', 403)
        t.expect('new API local declared HTTP', 'caller', 200, 'http://local-control/')
        code = "import sys; sys.path.insert(0,'/app'); import controller as c; a=c.Kube('" + t.NS + "'); a.call('GET','/api/v1/namespaces/kube-system/pods'); print('cluster workload discovery allowed');\ntry: a.call('GET','/api/v1/namespaces/" + t.NS + "/secrets'); raise AssertionError('unexpected permission')\nexcept c.APIError as e: assert e.code==403; print('Secrets denied', e.code)"
        t.record('controller RBAC boundaries retained', t.k('a', 'exec', 'deploy/mesh-access-controller', '--', 'python3', '-c', code))
        t.record('complete', 'New API discovery, status/RBAC and real traffic verified')
    except Exception as error:
        t.record('failure', str(error))
        raise
    finally:
        v.lab.k = t.k
        v.lab.capture()
        t.cleanup()
