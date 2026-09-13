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
                items = json.loads(t.k(side, 'get', {'AgentMeshEgress': 'agentmeshegresses', 'AgentMeshExpose': 'agentmeshexposes', 'AgentMeshTrustedBundle': 'agentmeshtrustedbundles'}[kind] + '.' + c.GROUP, '-o', 'json'))['items']
                for item in items:
                    assert item['apiVersion'] == c.VERSION
                    assert any(x['type'] == 'Configured' and x['status'] == 'True' and
                               x['observedGeneration'] == item['metadata']['generation']
                               for x in item.get('status', {}).get('conditions', [])), item['metadata']['name']
            t.check_build(side)
        t.expect('new API authorized cross-cluster mTLS', 'caller', 200)
        t.expect('new API unauthorized SA rejected', 'denied', 403)
        t.expect('new API local declared HTTP', 'caller', 200, 'http://local-control/')
        t.check_rbac('a')
        t.record('complete', 'New API discovery, status/RBAC and real traffic verified')
    except Exception as error:
        t.record('failure', str(error))
        raise
    finally:
        v.lab.k = t.k
        v.lab.capture()
        t.cleanup()
