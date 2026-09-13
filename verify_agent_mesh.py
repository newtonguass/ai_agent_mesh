#!/usr/bin/env python3
"""New CRDs and captured-egress contract on the isolated modern two-cluster lab.

Uses compatibility/verify.py contexts. Never runs against company contexts.
"""
import copy
import hashlib
import json
import pathlib
import os
import time

import compatibility.verify as lab

t = lab.t
c = t.ctl
t.E = pathlib.Path(__file__).resolve().parent / 'agent-evidence'
t.E.mkdir(exist_ok=True)
if os.environ.get('AGENT_MESH_LEGACY') == '1':
    t.run = lab.original_run
    def legacy_k(side, *args, ns=t.NS, data=None, check=True):
        return t.run(['kubectl', '--cache-dir', '/tmp/' + t.NS + '-kubectl-cache', '--context', 'kind-cluster-' + side, '-n', t.command_namespace(args, ns), *args], data, check)
    t.k = legacy_k
    t.E = pathlib.Path(__file__).resolve().parent / 'agent-legacy-evidence'
    t.E.mkdir(exist_ok=True)
original_apply = t.apply
original_k = t.k
DECLARATIONS = {}


def k(side, *args, **kwargs):
    return original_k(side, *args, **kwargs)


def apply(side, obj):
    obj = copy.deepcopy(obj)
    if obj['kind'] == 'AgentMeshEgress':
        DECLARATIONS[obj['metadata']['name']] = copy.deepcopy(obj)
    return original_apply(side, obj)


t.k = k
t.apply = apply


def update(a):
    apply('a', a)
    t.configured('a', [a['metadata']['name']])
    time.sleep(5)


def deny(label, url, extra=()):
    def attempt():
        result = k('a', 'exec', 'deploy/caller', '-c', 'curl', '--', 'curl', '-sS', '-k',
                   '--max-time', '4', '-w', '\nHTTP %{http_code}\n', *extra, url, check=False)
        return result if 'HTTP 200' not in result and ('HTTP ' in result) else None
    value = t.wait(label, attempt, attempts=10)
    t.record(label, value)


def extras(roots):
    for side in ('a', 'b'):
        for file in ('controller.py', 'egress.py'):
            local = hashlib.sha256((pathlib.Path(__file__).resolve().parent / file).read_bytes()).hexdigest()
            code = "import hashlib; print(hashlib.sha256(open('/app/" + file + "','rb').read()).hexdigest())"
            actual = k(side, 'exec', 'deploy/mesh-access-controller', '--', 'python3', '-c', code).strip()
            assert actual == local
            t.record(side + ' deployed ' + file + ' hash', actual)
    t.fresh()
    t.expect('new CRD authorized mTLS', 'caller', 200)
    exposed = t.get('b', 'agentmeshexpose', 'backend')
    assert 'serviceAccount' not in exposed['spec']
    alias = t.get('b', 'service', c.name('expose', t.HOST) + '-backend')
    assert alias['spec']['selector'] == {'app': 'backend'}
    backend = t.get('b', 'deployment', 'backend')
    assert c.LABEL not in backend['spec']['template']['metadata']['labels']
    original_apply('b', t.obj('ServiceAccount', 'backend-alternate'))
    changed = copy.deepcopy(backend)
    changed['spec']['template']['spec']['serviceAccountName'] = 'backend-alternate'
    original_apply('b', changed)
    t.ready('b', 'backend')
    t.configured('b', ['backend'])
    t.expect('exposure survives backend ServiceAccount change without CR edit', 'caller', 200)
    t.record('exposure preserves Service selector without backend SA labels', True)
    # Two ports on one Service: importing the Service must not allow port 81.
    svc = t.get('a', 'service', 'local-control')
    svc['spec']['ports'].append({'name': 'http-other', 'port': 81, 'targetPort': 8080})
    original_apply('a', svc)
    t.expect('declared internal HTTP', 'caller', 200, 'http://local-control/')
    t.expect('unselected control reaches Service port 81', 'plain', 200, 'http://local-control:81/')
    deny('undeclared port on declared internal Service', 'http://local-control:81/')
    ip = svc['spec']['clusterIP']
    deny('direct IP cannot bypass HTTP authority allowlist', 'http://' + ip + '/')
    # Independent unmeshed external HTTP/TCP fixture. Its content is identical
    # through both TCP sockets; external TCP deliberately has no hostname policy.
    fixture = t.nginx('external', 'mesh-access-backend')
    fixture['spec']['template']['metadata']['annotations'] = {'sidecar.istio.io/inject': 'false'}
    fixture['spec']['template']['spec']['serviceAccountName'] = 'default'
    original_apply('b', fixture)
    original_apply('b', t.obj('Service', 'external', {'type': 'NodePort', 'selector': {'app': 'external'},
        'ports': [{'name': 'http', 'port': 8080, 'targetPort': 8080, 'nodePort': 31680},
                  {'name': 'tcp', 'port': 9090, 'targetPort': 8080, 'nodePort': 31681}]}))
    t.ready('b', 'external')
    node = t.run(['docker', 'inspect', 'cluster-b-control-plane', '--format',
                  '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}']).strip()
    dns = t.get('a', 'cm', 'coredns', 'kube-system')
    core = dns['data']['Corefile'].replace('    hosts {', '    hosts {\n        ' + node + ' external.agent.test wrong.agent.test')
    k('a', 'patch', 'cm', 'coredns', '--type=merge', '--patch-file=/dev/stdin', ns='kube-system',
      data=json.dumps({'data': dict(dns['data'], Corefile=core)}))
    k('a', 'rollout', 'restart', 'deploy/coredns', ns='kube-system')
    k('a', 'rollout', 'status', 'deploy/coredns', '--timeout=90s', ns='kube-system')
    original = copy.deepcopy(DECLARATIONS['caller'])
    a = copy.deepcopy(original)
    a['spec']['outCluster'] += [
        {'host': 'external.agent.test', 'port': 8080, 'protocol': 'HTTP', 'endpoint': node + ':31680'},
        {'host': t.NORMAL, 'port': 443, 'protocol': 'HTTPS', 'endpoint': node + ':' + str(t.NODEPORT)},
        {'host': 'tcp.agent.test', 'port': 31681, 'protocol': 'TCP', 'addresses': [node]}]
    original_apply('a', t.obj('Service', 'local-tcp', {'selector': {'app': 'local-control'},
        'ports': [{'name': 'tcp', 'port': 9090, 'targetPort': 8080}]}))
    a['spec']['inCluster'].append({'host': 'local-tcp', 'port': 9090, 'protocol': 'TCP'})
    update(a)
    t.fresh()
    t.expect('declared external HTTP', 'caller', 200, 'http://external.agent.test:8080/')
    t.expect('declared external opaque TCP', 'caller', 200, 'http://' + node + ':31681/')
    t.expect('declared internal opaque TCP', 'caller', 200, 'http://local-tcp:9090/')
    # Another reachable IP on the same TCP port must not inherit the first IP's grant.
    original_apply('a', fixture)
    original_apply('a', t.obj('Service', 'external-peer', {'type': 'NodePort', 'selector': {'app': 'external'},
        'ports': [{'name': 'tcp', 'port': 9090, 'targetPort': 8080, 'nodePort': 31681}]}))
    t.ready('a', 'external')
    source_node = 'cluster-a-control-plane' if os.environ.get('AGENT_MESH_LEGACY') == '1' else 'mesh-access134-a-control-plane'
    source_ip = t.run(['docker', 'inspect', source_node, '--format',
                       '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}']).strip()
    t.expect('unselected control reaches second IP on TCP port', 'plain', 200, 'http://' + source_ip + ':31681/')
    deny('TCP grant does not include another reachable IP on same port', 'http://' + source_ip + ':31681/')

    # Pass B's actual public root to curl, so HTTPS checks server identity too.
    k('a', 'exec', '-i', 'deploy/caller', '-c', 'curl', '--', 'sh', '-c', 'cat > /tmp/gateway-ca.pem', data=roots['b'])
    https = k('a', 'exec', 'deploy/caller', '-c', 'curl', '--', 'curl', '-sS', '--max-time', '8',
              '--cacert', '/tmp/gateway-ca.pem', '-w', '\nHTTP %{http_code}\n', 'https://' + t.NORMAL + '/')
    assert 'HTTP 200' in https
    t.record('application HTTPS and originated MTLS coexist on 443', https)
    t.expect('MTLS preserved alongside application HTTPS', 'caller', 200)
    deny('undeclared HTTP host on declared HTTP port', 'http://wrong.agent.test:8080/')
    # Registry contains external.agent.test for caller; denied SA must not inherit it.
    blocked = t.request('denied', 'http://external.agent.test:8080/')
    assert 'HTTP 200' not in blocked
    t.record('other SA does not inherit external whitelist', blocked)
    t.expect('unselected control reaches external port 31680', 'plain', 200, 'http://' + node + ':31680/')
    deny('undeclared external TCP port', 'http://' + node + ':31680/')
    update(original)
    t.fresh()
    deny('revoked external HTTP blocked', 'http://external.agent.test:8080/')
    deny('revoked opaque TCP blocked', 'http://' + node + ':31681/')
    empty = copy.deepcopy(original)
    empty['spec'] = {'serviceAccount': 'caller'}
    update(empty)
    t.fresh()
    deny('empty whitelist blocks internal destination', 'http://local-control/')
    deny('empty whitelist blocks remote MTLS destination', 'http://' + t.HOST + ':443/')
    update(original)
    t.fresh()
    t.expect('whitelist restored', 'caller', 200)
    source = t.dump('a', 'caller')
    (t.E / 'egress-active.json').write_text(json.dumps(source, indent=2))
    text = json.dumps(source)
    assert 'agent_mesh.egress.http' in text and 'agent_mesh.egress.tcp' in text
    t.record('HTTP and TCP guards present in active Envoy config', True)


def local_gateway(roots, expose):
    # An HTTP-named native Service lets Istio parse the application's HTTP,
    # select the TLS subset and rewrite authority to the exposed SNI hostname.
    original_apply('b', t.obj('ServiceAccount', 'local-caller'))
    original_apply('b', t.workload('local-caller', 'curlimages/curl:8.10.1', ['sleep', 'infinity']))
    original_apply('b', t.obj('Service', 'local-gateway', {'selector': {'app': 'mesh-access-ingress'},
        'ports': [{'name': 'http-origination', 'port': 443, 'targetPort': 8443}]}))
    t.ready('b', 'local-caller')
    a = t.obj('AgentMeshEgress', 'local-caller', {'serviceAccount': 'local-caller',
        'inCluster': [{'host': 'local-gateway', 'port': 443, 'protocol': 'MTLS', 'serverName': t.HOST}]}, api=c.VERSION)
    original_apply('b', a)
    changed = copy.deepcopy(expose)
    changed['spec']['allow'].append('cluster-b-mesh/ns/' + t.NS + '/sa/local-caller')
    apply('b', changed)
    def ready_local():
        d = t.get('b', 'agentmeshegress', 'local-caller')
        return any(x['type']=='Configured' and x['status']=='True' for x in d.get('status', {}).get('conditions', []))
    t.wait('local gateway requester configured', ready_local)
    t.ready('b', 'local-caller')
    def call():
        return k('b', 'exec', 'deploy/local-caller', '-c', 'curl', '--', 'curl', '-sS', '--max-time', '5',
                 '-w', '\nHTTP %{http_code}\n', 'http://local-gateway:443/', check=False)
    good = t.wait('same cluster gateway mTLS', lambda: (lambda r: r if 'HTTP 200' in r else None)(call()))
    t.record('same cluster native-Service gateway MTLS', good)
    native = 'local-gateway.' + t.NS + '.svc.cluster.local'
    entries = json.loads(k('b', 'get', 'serviceentry', '-o', 'json'))['items']
    assert not any(native in d['spec']['hosts'] for d in entries)
    t.record('local gateway uses native Service without ServiceEntry', True)
    source = t.dump('b', 'local-caller')
    (t.E / 'local-gateway-source-active.json').write_text(json.dumps(source, indent=2))
    original_k('b', 'delete', 'agentmeshegress', 'local-caller')
    apply('b', expose)
    t.wait('local gateway generated route pruned', lambda: not k('b', 'get', 'virtualservice',
        c.name('remote', native), '--ignore-not-found', '-o', 'name').strip())


if __name__ == '__main__':
    try:
        for side in ('a', 'b'):
            t.record(side + ' Kubernetes version', json.loads(k(side, 'get', '--raw=/version'))['gitVersion'])
            t.record(side + ' Istiod image', t.get(side, 'deployment', 'istiod', 'istio-system')['spec']['template']['spec']['containers'][0]['image'])
        roots, expose = t.setup()
        extras(roots)
        local_gateway(roots, expose)
        # Full independent-CA, gateway identity/RBAC, drift, updates and pruning contract.
        t.test(roots, expose)
        t.record('complete', 'AgentMesh APIs, captured-egress guards and full mTLS contract passed')
    finally:
        lab.k = t.k
        lab.capture()
        t.cleanup()
