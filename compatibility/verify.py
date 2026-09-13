#!/usr/bin/env python3
"""Run the existing integration contract on isolated K8s 1.34 compatibility labs.

Does not start/stop clusters or change legacy contexts. Saves separate evidence.
Requires mesh-access134-a/b contexts in MESH_ACCESS_KUBECONFIG.
"""
import hashlib
import json
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import integration_test as t

CONFIG = os.environ.get('MESH_ACCESS_KUBECONFIG', '/tmp/mesh-access-k134.config')
t.E = HERE / 'evidence'
t.E.mkdir(exist_ok=True)
original_run = t.run


def run(args, *a, **kw):
    args = list(args)
    if args[:3] == ['docker', 'inspect', 'cluster-b-control-plane']:
        args[2] = 'mesh-access134-b-control-plane'
    return original_run(args, *a, **kw)


def k(c, *args, ns=t.NS, data=None, check=True):
    return run(['kubectl', '--cache-dir', '/tmp/' + t.NS + '-kubectl-cache', '--kubeconfig', CONFIG, '--context', 'kind-mesh-access134-' + c,
                '-n', t.command_namespace(args, ns), *args], data=data, check=check)


t.run = run
t.k = k


def capture():
    for c in t.CREATED_NAMESPACES:
        for label, args in [
            ('controller.log', ['logs', 'deploy/mesh-access-controller', '--tail=100']),
            ('agentmesh.json', ['get', 'agentmeshegress,agentmeshexpose,agentmeshtrustedbundle', '-o', 'json']),
            ('pods.txt', ['get', 'pods', '-o', 'wide'])]:
            (t.E / (c + '-' + label)).write_text(k(c, *args, check=False))


if __name__ == '__main__':
    try:
        versions = {}
        for c in ['a', 'b']:
            versions[c] = {
                'kubernetes': json.loads(k(c, 'get', '--raw=/version'))['gitVersion'],
                'istiod_images': [x['image'] for x in t.get(c, 'deployment', 'istiod', 'istio-system')['spec']['template']['spec']['containers']]}
            assert versions[c]['kubernetes'].startswith('v1.34.'), versions[c]
        t.record('actual compatibility cluster versions', versions)
        roots, expose = t.setup()
        local_hash = hashlib.sha256((HERE.parent / 'controller.py').read_bytes()).hexdigest()
        for c in ['a', 'b']:
            code = "import hashlib; print(hashlib.sha256(open('/app/controller.py','rb').read()).hexdigest())"
            actual = k(c, 'exec', 'deploy/mesh-access-controller', '--', 'python3', '-c', code).strip()
            assert actual == local_hash, 'Controller image does not match workspace source'
        t.record('running controller source sha256', local_hash)
        placement = {}
        for label, c, app in [('requester', 'a', 'caller'), ('backend', 'b', 'backend'),
                              ('gateway', 'b', 'mesh-access-ingress')]:
            pods = json.loads(k(c, 'get', 'pods', '-l', 'app=' + app, '-o', 'json'))['items']
            pod = next(p for p in pods if not p['metadata'].get('deletionTimestamp'))
            placement[label] = {
                'containers': [x['name'] for x in pod['spec'].get('containers', [])],
                'restartableInitContainers': [x['name'] for x in pod['spec'].get('initContainers', [])
                                              if x.get('restartPolicy') == 'Always']}
        assert 'istio-proxy' in placement['requester']['restartableInitContainers']
        assert 'istio-proxy' in placement['backend']['restartableInitContainers']
        t.record('actual native sidecar placement', placement)
        info = json.loads(k('a', 'exec', 'deploy/caller', '-c', 'istio-proxy', '--',
                            'pilot-agent', 'request', 'GET', 'server_info'))
        t.record('actual Envoy version', info['version'])
        t.test(roots, expose)
        capture()
        t.record('complete', 'Kubernetes 1.34 compatibility run passed the full real-cluster controller contract.')
    except Exception:
        capture()
        raise
    finally:
        t.cleanup()
