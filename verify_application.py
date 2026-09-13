#!/usr/bin/env python3
"""Real JSON API, lifecycle and existing AgentMesh contracts on isolated kind labs.

Default modern contexts; AGENT_MESH_LEGACY=1 selects preserved legacy contexts.
No company contexts. No change to the controller's security architecture.
"""
import copy
import json
import os
import pathlib
import threading
import time

import verify_agent_mesh as v

t, c = v.t, v.c
HERE = pathlib.Path(__file__).resolve().parent
t.E = HERE / ('application-legacy-evidence' if os.environ.get('AGENT_MESH_LEGACY') == '1'
              else 'application-evidence')
t.E.mkdir(exist_ok=True)
CATALOG = t.NS + '-catalog'
CREATED_CATALOG = False
EXPECT_FIXED = os.environ.get('APP_DISCOVERY_ONLY') != '1'


def deploy(side, name, sa, mode, replicas=1, namespace=None):
    namespace = namespace or t.NS
    config = t.obj('ConfigMap', 'application-fixture')
    config['metadata']['namespace'] = namespace
    config['data'] = {'application.py': (HERE / 'application_fixture.py').read_text()}
    t.k(side, 'apply', '-f', '-', ns=namespace, data=json.dumps(config))
    obj = t.workload(name, 'mesh-access-controller:dev', ['python3'],
                     ['/fixture/application.py', 'server'] if mode == 'server' else
                     ['-c', 'import time; time.sleep(86400)'], sa=sa, port=8080 if mode == 'server' else None)
    obj['metadata']['namespace'] = namespace
    obj['spec']['replicas'] = replicas
    obj['spec']['strategy'] = {'type': 'RollingUpdate', 'rollingUpdate': {'maxUnavailable': 0, 'maxSurge': 1}}
    template = obj['spec']['template']
    template['metadata']['annotations'] = {'proxy.istio.io/config': '{"holdApplicationUntilProxyStarts":true}'}
    spec = template['spec']
    spec.update(automountServiceAccountToken=False, shareProcessNamespace=False, terminationGracePeriodSeconds=30)
    spec['volumes'] = [{'name': 'fixture', 'configMap': {'name': 'application-fixture'}}]
    app = spec['containers'][0]
    app['imagePullPolicy'] = 'IfNotPresent'
    app['securityContext'] = {'runAsNonRoot': True, 'runAsUser': 10001, 'runAsGroup': 10001,
        'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
        'capabilities': {'drop': ['ALL']}, 'seccompProfile': {'type': 'RuntimeDefault'}}
    app['resources'] = {'requests': {'cpu': '25m', 'memory': '32Mi'}, 'limits': {'cpu': '500m', 'memory': '128Mi'}}
    app['volumeMounts'] = [{'name': 'fixture', 'mountPath': '/fixture', 'readOnly': True}]
    app['env'] = [{'name': 'PYTHONDONTWRITEBYTECODE', 'value': '1'}, {'name': 'APP_VERSION', 'value': 'v1'}]
    if mode == 'server':
        app['ports'] = [{'name': 'http-api', 'containerPort': 8080}]
        app['readinessProbe'] = {'httpGet': {'path': '/health', 'port': 8080}, 'periodSeconds': 2}
        app['livenessProbe'] = {'httpGet': {'path': '/health', 'port': 8080}, 'periodSeconds': 10}
    t.k(side, 'apply', '-f', '-', ns=namespace, data=json.dumps(obj))
    t.k(side, 'rollout', 'status', 'deploy/' + name, '--timeout=120s', ns=namespace)


def call(host=None, port=443, name='app-caller', count=10, workers=1, extra=()):
    result = t.k('a', 'exec', 'deploy/' + name, '-c', 'app', '--', 'python3',
                 '/fixture/application.py', 'client', '--host', host or t.HOST, '--port', str(port),
                 '--count', str(count), '--workers', str(workers), *extra)
    return json.loads(result)


def success(label, **kwargs):
    result = t.wait(label, lambda: (lambda r: r if r['statuses'] == {'200': r['requests']} else None)(call(**kwargs)))
    t.record(label, result)
    return result


def exercise():
    global CREATED_CATALOG
    # Preserve the original backend app label and policy, but exercise real HTTP.
    deploy('b', 'backend', 'backend', 'server', replicas=2)
    service = t.get('b', 'service', 'backend')
    service['spec']['ports'][0]['targetPort'] = 'http-api'
    t.k('b', 'patch', 'service', 'backend', '--type=merge', '-p',
        json.dumps({'spec': {'ports': service['spec']['ports']}}))
    t.configured('b', ['backend'])
    alias_name = c.name('expose', t.HOST) + '-backend'
    t.wait('named backend target port reconciled', lambda: t.get('b', 'service', alias_name)['spec']['ports'][0]['targetPort'] == 'http-api')
    t.k('b', 'scale', 'deploy/ingressgateway', '--replicas=2')
    t.ready('b', 'ingressgateway')
    for name, sa in [('app-caller', 'caller'), ('app-denied', 'denied')]:
        deploy('a', name, sa, 'client')
    t.configured('a', ['caller', 'denied'])
    for name in ['app-caller', 'app-denied']:
        t.ready('a', name)
    t.apply('a', {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {
        'name': CATALOG, 'labels': {'istio-injection': 'enabled'}}})
    CREATED_CATALOG = True
    deploy('a', 'catalog', 'default', 'server', namespace=CATALOG)
    catalog = t.obj('Service', 'catalog', {'selector': {'app': 'catalog'},
        'ports': [{'name': 'http', 'port': 8080, 'targetPort': 'http-api'}]})
    catalog['metadata']['namespace'] = CATALOG
    t.k('a', 'apply', '-f', '-', ns=CATALOG, data=json.dumps(catalog))
    strict = t.obj('PeerAuthentication', 'strict', {'mtls': {'mode': 'STRICT'}}, api=c.SEC)
    strict['metadata']['namespace'] = CATALOG
    t.k('a', 'apply', '-f', '-', ns=CATALOG, data=json.dumps(strict))
    native = 'catalog.' + CATALOG + '.svc.cluster.local'
    for name in ['caller', 'denied']:
        declaration = t.obj('AgentMeshEgress', name, t.get('a', 'agentmeshegress', name)['spec'], api=c.VERSION)
        declaration['spec']['outCluster'][0]['endpoint'] = t.HOST + ':' + str(t.NODEPORT)
        if name == 'caller':
            declaration['spec']['inCluster'].append({'host': native, 'port': 8080, 'protocol': 'HTTP'})
        t.apply('a', declaration)
    t.configured('a', ['caller', 'denied'])
    success('JSON POST through independent-CA gateway using DNS endpoint', count=10)
    success('HTTP/1.1 reused connection with 128 KiB Unicode JSON payload', count=20, extra=['--padding', '131072'])
    success('concurrent JSON API requests', count=25, workers=8)
    replicas = success('fresh connections cover backend replicas', count=30, workers=4, extra=['--fresh'])
    assert len(replicas['pods']) == 2, replicas
    success('cross-namespace native Service using DNS and local mesh mTLS', host=native, port=8080)
    wrong = call(name='app-denied')
    assert wrong['statuses'] == {'403': wrong['requests']}, wrong
    t.record('real application with unauthorized SA rejected at gateway', wrong)
    blocked = call(host=native, port=8080, name='app-denied')
    assert '200' not in blocked['statuses'], blocked
    t.record('other SA cannot access undeclared cross-namespace Service', blocked)
    state = json.loads(t.k('a', 'exec', 'deploy/app-caller', '-c', 'app', '--', 'python3', '-c',
        "import os,json; print(json.dumps({'uid':os.getuid(),'apiTokenMounted':os.path.exists('/var/run/secrets/kubernetes.io/serviceaccount/token'),'sdsMounted':os.path.exists('/var/run/secrets/workload-spiffe-uds/socket') or os.path.exists('/etc/istio/proxy/SDS')}))"))
    assert state == {'uid': 10001, 'apiTokenMounted': False, 'sdsMounted': False}, state
    t.record('hardened application works without Kubernetes API token or SDS mount', state)
    # Record conservative false negatives before fixing HTTP authority handling.
    case = call(count=1, extra=['--authority', t.HOST.upper() + ':443'])
    t.record('case-insensitive DNS authority compatibility', case)
    if EXPECT_FIXED:
        assert case['statuses'] == {'200': 1}, case
    for authority in [t.HOST.upper() + '.UNDECLARED.TEST:443', t.HOST.upper() + ':444']:
        rejected = call(count=3, extra=['--authority', authority])
        assert rejected['statuses'] == {'403': 3}, rejected
        t.record('unlisted HTTP authority rejected: ' + authority, rejected)
    # Rolling backend rollout while persistent clients send application requests.
    outcome = {}
    def traffic():
        try:
            outcome['result'] = call(count=150, workers=4, extra=['--delay', '0.1'])
        except Exception as e:
            outcome['error'] = str(e)
    thread = threading.Thread(target=traffic)
    thread.start()
    time.sleep(2)
    t.k('b', 'set', 'env', 'deploy/backend', 'APP_VERSION=v2')
    t.ready('b', 'backend')
    thread.join(timeout=120)
    assert not thread.is_alive() and 'error' not in outcome, outcome
    t.record('backend rolling update during persistent application traffic', outcome['result'])
    after = success('fresh requests converge to backend v2 after rollout', extra=['--fresh'])
    assert after['versions'] == ['v2'], after
    # A staged, unused bad bundle must not freeze active permission revocation.
    bad = t.obj('AgentMeshTrustedBundle', 'staged-next-root', {'caBundle': 'not-a-certificate'}, api=c.VERSION)
    # Replay desired spec only, never an old resourceVersion/status snapshot.
    original = t.obj('AgentMeshEgress', 'caller', t.get('a', 'agentmeshegress', 'caller')['spec'], api=c.VERSION)
    try:
        t.apply('a', bad)
        revoked = copy.deepcopy(original)
        revoked['spec']['inCluster'] = [d for d in revoked['spec']['inCluster'] if d['host'] != native]
        t.apply('a', revoked)
        time.sleep(12)
        result = call(host=native, port=8080, count=3, extra=['--fresh'])
        t.record('revocation while an unused CA bundle is invalid', result)
        if EXPECT_FIXED:
            assert '200' not in result['statuses'], result
            t.configured('a', ['caller'])
            condition = t.get('a', 'agentmeshtrustedbundle', 'staged-next-root')['status']['conditions'][0]
            assert condition['status'] == 'False', condition
            t.record('unused invalid bundle reports its own failure without blocking active policy', condition['status'])
    finally:
        t.k('a', 'delete', 'agentmeshtrustedbundle', 'staged-next-root', '--ignore-not-found')
        t.apply('a', original)
        t.configured('a', ['caller'])
    success('catalog access restored after valid policy update', host=native, port=8080)
    # Keep the comprehensive regression contract's fixtures, clean extra clients.
    t.k('a', 'delete', 'deployment', 'app-caller', 'app-denied', '--wait=true')


if __name__ == '__main__':
    try:
        for side in ['a', 'b']:
            t.record(side + ' Kubernetes version', json.loads(t.k(side, 'get', '--raw=/version'))['gitVersion'])
            t.record(side + ' Istiod image', t.get(side, 'deployment', 'istiod', 'istio-system')['spec']['template']['spec']['containers'][0]['image'])
        roots, expose = t.setup()
        if os.environ.get('APP_DISCOVERY_ONLY') != '1':
            v.extras(roots)
        exercise()
        if os.environ.get('APP_DISCOVERY_ONLY') != '1':
            v.local_gateway(roots, expose)
            t.test(roots, expose)
        t.record('complete', 'Application scenarios and requested regression checks finished')
    except Exception as error:
        t.record('failure', str(error))
        raise
    finally:
        v.lab.k = t.k
        v.lab.capture()
        if CREATED_CATALOG:
            t.k('a', 'delete', 'namespace', CATALOG, '--wait=true', '--timeout=90s')
        t.cleanup()
