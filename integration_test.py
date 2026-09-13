#!/usr/bin/env python3
"""Destructive only to isolated mesh-access-poc-* namespaces on the two kind labs.

Requires running cluster-a/b and the controller image loaded in both. Restores
CoreDNS and deletes its namespaces/CRDs in finally. Never use on company contexts.
Signing keys exist only in memory and temporary mode-0600 files; no Secret dumps.
"""
import base64
import copy
import hashlib
import json
import os
import pathlib
import subprocess
import tempfile
import time

import yaml
import lab_helpers as ctl
import build

HERE = pathlib.Path(__file__).resolve().parent
E = HERE / 'evidence'
E.mkdir(exist_ok=True)
NS = 'mesh-access-poc-' + str(int(time.time()))
NAMESPACE_MODE = os.environ.get('AGENT_MESH_NAMESPACE') == '1'
CTL_NS = NS if NAMESPACE_MODE else NS + '-system'
HOST = 'controller.gateway.test'
NORMAL = 'normal.controller.test'
NODEPORT = 31543
RESULTS = []
SAVED_DNS = {}
CREATED_CRDS = []
CREATED_NAMESPACES = []
CREATED_INSTALLS = []
GATEWAY_BASELINE = {}


def gateway_state():
    result = {}
    for kind, name in [('deployment', 'ingressgateway'), ('service', 'ingressgateway'),
                       ('role', 'mesh-access-gateway-sds'), ('rolebinding', 'mesh-access-gateway-sds'),
                       ('secret', 'mesh-access-gateway-tls'), ('gateway', 'owner-mtls'),
                       ('virtualservice', 'owner-mtls'), ('gateway', 'ordinary-https'),
                       ('virtualservice', 'ordinary-https'), ('destinationrule', 'ordinary-https'),
                       ('authorizationpolicy', 'owner-client-policy')]:
        resource = get('b', kind, name)
        resource.pop('status', None)
        resource['metadata'] = {key: resource['metadata'].get(key) for key in ['uid', 'labels', 'annotations']}
        # Compare Secret contents by hash only; never persist or print credentials.
        result[kind + '/' + name] = hashlib.sha256(json.dumps(resource, sort_keys=True).encode()).hexdigest()
    return result


def gateway_unchanged(label):
    assert gateway_state() == GATEWAY_BASELINE, 'Owner-managed gateway resources changed'
    assert not get('b', 'deployment', 'ingressgateway')['spec']['template']['metadata']['labels'].get(ctl.GW_LABEL)
    record(label, True)


def command_namespace(args, ns):
    if ns == NS and 'deploy/mesh-access-controller' in args:
        return CTL_NS
    return ns


def run(args, data=None, check=True, timeout=150):
    p = subprocess.run(args, input=data, text=True, capture_output=True, timeout=timeout)
    if check and p.returncode:
        raise RuntimeError('Command failed: ' + ' '.join(args[:8]) + '\n' + p.stderr[:2000])
    return p.stdout + (p.stderr if p.returncode else '')


def k(c, *args, ns=NS, data=None, check=True):
    return run(['kubectl', '--cache-dir', '/tmp/' + NS + '-kubectl-cache', '--context', 'kind-cluster-' + c, '-n', command_namespace(args, ns), *args], data, check)


def get(c, kind, name, ns=NS):
    if NAMESPACE_MODE and kind in ['agentmeshegress', 'agentmeshexpose']:
        field = 'egress' if kind == 'agentmeshegress' else 'expose'
        source = get(c, 'configmap', 'mesh-access-declarations')
        entry = next(x for x in json.loads(source['data']['declarations.json'])[field] if x['name'] == name)
        spec = {k: v for k, v in entry.items() if k != 'name'}
        resource = obj('AgentMeshEgress' if field == 'egress' else 'AgentMeshExpose', name, spec, api=ctl.VERSION)
        resource['metadata']['generation'] = 1
        state = json.loads(get(c, 'configmap', 'mesh-access-state').get('data', {}).get('status.json', '{}'))
        if state.get('inputResourceVersion') == source['metadata']['resourceVersion']:
            resource['status'] = state.get('declarations', {}).get(resource['kind'] + '/' + name, {})
        return resource
    return json.loads(k(c, 'get', kind, name, '-o', 'json', ns=ns))


def apply(c, obj):
    if NAMESPACE_MODE and obj['kind'] in ['AgentMeshEgress', 'AgentMeshExpose']:
        update_declarations(c, obj)
        return
    if NAMESPACE_MODE and obj['kind'] == 'AgentMeshTrustedBundle':
        assert obj['metadata']['name'] == 'mesh-access-trust'
        return k(c, 'apply', '-f', '-', data=json.dumps({'apiVersion':'v1', 'kind':'ConfigMap',
            'metadata': {'name':'mesh-access-trust','namespace':NS}, 'data': {'ca.crt':obj['spec']['caBundle']}}))
    return k(c, 'apply', '-f', '-', data=json.dumps(obj))


def update_declarations(side, resource=None, delete_kind=None, delete_name=None):
    cm = get(side, 'configmap', 'mesh-access-declarations')
    doc = json.loads(cm['data']['declarations.json'])
    if resource:
        field = 'egress' if resource['kind'] == 'AgentMeshEgress' else 'expose'
        name = resource['metadata']['name']
        doc[field] = [x for x in doc[field] if x['name'] != name]
        doc[field].append(dict(name=name, **resource['spec']))
    else:
        fields = ['egress', 'expose'] if delete_kind is None else [delete_kind]
        for field in fields:
            doc[field] = [x for x in doc[field] if delete_name is not None and x['name'] != delete_name]
    k(side, 'patch', 'configmap', 'mesh-access-declarations', '--type=merge', '--patch-file=/dev/stdin',
      data=json.dumps({'data': {'declarations.json': json.dumps(doc)}}))


def delete_declarations(side, kind=None, name=None):
    if NAMESPACE_MODE:
        update_declarations(side, delete_kind=kind, delete_name=name)
    elif kind is None:
        k(side, 'delete', 'agentmeshegress,agentmeshexpose', '--all')
    else:
        k(side, 'delete', 'agentmesh' + kind, name)


def obj(kind, name, spec=None, api='v1'):
    d = {'apiVersion': api, 'kind': kind, 'metadata': {'name': name, 'namespace': NS}}
    if kind in ctl.CLUSTER_KINDS:
        d['metadata'].pop('namespace')
    if spec is not None:
        d['spec'] = spec
    return d


def record(test, result):
    RESULTS.append({'test': test, 'result': result})
    (E / 'results.json').write_text(json.dumps(RESULTS, indent=2) + '\n')
    print(json.dumps(RESULTS[-1]), flush=True)


def wait(label, fn, attempts=60):
    error = ''
    for _ in range(attempts):
        try:
            value = fn()
            if value:
                return value
        except Exception as e:
            error = str(e)
        time.sleep(2)
    raise AssertionError('Timeout: ' + label + ' ' + error)


def ready(c, name):
    k(c, 'rollout', 'status', 'deploy/' + name, '--timeout=120s')


def dump(c, name):
    return json.loads(k(c, 'exec', 'deploy/' + name, '-c', 'istio-proxy', '--',
                        'pilot-agent', 'request', 'GET', 'config_dump'))


def request(name='caller', url=None):
    return k('a', 'exec', 'deploy/' + name, '-c', 'curl', '--', 'curl', '-sS', '--max-time', '6',
             '-w', '\nHTTP %{http_code}\n', url or 'http://' + HOST + ':443/', check=False)


def expect(label, name, code, url=None):
    value = wait(label, lambda: (lambda r: r if 'HTTP ' + str(code) in r else None)(request(name, url)))
    if code == 200:
        assert 'mesh-access-backend' in value or 'local-control' in value, value
    record(label, value)


def fresh():
    for name in ['caller', 'denied']:
        k('a', 'rollout', 'restart', 'deploy/' + name)
        ready('a', name)
        wait('controller labels new ' + name + ' pod', lambda: all(
            p['metadata'].get('labels', {}).get(ctl.LABEL) == ctl.sa_label(name)
            for p in json.loads(k('a', 'get', 'pods', '-l', 'app=' + name, '-o', 'json'))['items']
            if not p['metadata'].get('deletionTimestamp')))
    time.sleep(3)


def workload(name, image, command, args=(), sa=None, port=None):
    container = {'name': 'curl' if 'curl' in image else 'app', 'image': image,
                 'command': command, 'args': list(args)}
    if port:
        container['ports'] = [{'containerPort': port}]
    return obj('Deployment', name, {'replicas': 1, 'selector': {'matchLabels': {'app': name}},
        'template': {'metadata': {'labels': {'app': name}}, 'spec': {'serviceAccountName': sa or name,
            'terminationGracePeriodSeconds': 0, 'containers': [container]}}}, api='apps/v1')


def nginx(name, body):
    return workload(name, 'nginx:alpine', ['/bin/sh', '-c'], [
        'printf \'server { listen 8080; location / { return 200 "' + body +
        '\\n"; } }\' > /etc/nginx/conf.d/default.conf; nginx -g "daemon off;"'], port=8080)


def configured(c, names):
    def check():
        for name in names:
            d = get(c, 'agentmeshegress' if c == 'a' else 'agentmeshexpose', name)
            conditions = d.get('status', {}).get('conditions', [])
            if not any(x['type'] == 'Configured' and x['status'] == 'True' and
                       x['observedGeneration'] == d['metadata']['generation'] for x in conditions):
                return False
        return True
    return wait('AgentMesh Configured in ' + c, check)


def bundle(c, pem):
    d = obj('AgentMeshTrustedBundle', 'mesh-access-trust', {'caBundle': pem}, api=ctl.VERSION)
    apply(c, d)


def owner_auth(principals=None):
    if principals is None:
        principals = ['cluster-a-mesh/ns/' + NS + '/sa/caller']
    policy = obj('AuthorizationPolicy', 'owner-client-policy', {
        'selector': {'matchLabels': {'app': 'mesh-access-ingress'}}, 'action': 'DENY',
        'rules': [{'from': [{'source': {'notPrincipals': principals}}],
                   'to': [{'operation': {'hosts': [HOST, HOST + ':*'], 'ports': ['8443']}}]}]}, api=ctl.SEC)
    apply('b', policy)


def check_build(side):
    expected = build.source_id()
    actual = k(side, 'exec', 'deploy/mesh-access-controller', '--', '/controller', '--version').strip()
    assert actual == expected, (actual, expected)
    record(side + ' deployed Go source identity', actual)


def check_rbac(side):
    checks = {}
    for verb, resource, expected in [('list', 'pods', 'yes'), ('get', 'secrets', 'no'),
            ('patch', 'agentmeshtrustedbundles.' + ctl.GROUP, 'no'),
            ('patch', 'gateways.networking.istio.io', 'no'), ('patch', 'services', 'no'),
            ('patch', 'authorizationpolicies.security.istio.io', 'no')]:
        if NAMESPACE_MODE and resource.startswith('agentmeshtrustedbundles'):
            continue
        result = k(side, 'auth', 'can-i', verb, resource,
                   '--as=system:serviceaccount:' + CTL_NS + ':mesh-access-controller', check=False).strip()
        assert result.splitlines()[0] == expected, (verb, resource, result)
        checks[verb + ' ' + resource] = expected
    record(side + ' controller RBAC boundaries', checks)


def setup():
    for c in ['a', 'b']:
        # A recently resumed node can briefly report stale Deployment readiness.
        # Exercise the real validation webhook before creating any test fixtures.
        probe = {'apiVersion': ctl.SEC, 'kind': 'PeerAuthentication', 'metadata': {
            'name': 'agentmesh-webhook-readiness', 'namespace': 'default'},
            'spec': {'mtls': {'mode': 'STRICT'}}}
        wait('Istio admission webhook ready in ' + c, lambda c=c: k(c, 'apply',
            '--dry-run=server', '-f', '-', ns='default', data=json.dumps(probe)))
        existing = k(c, 'get', 'namespace', NS, '--ignore-not-found', '-o', 'name')
        assert not existing.strip(), 'Refusing existing namespace ' + NS
        crd_existing = k(c, 'get', 'crd', *[p + '.' + ctl.GROUP for p in ['agentmeshtrustedbundles', 'agentmeshegresses', 'agentmeshexposes']], '--ignore-not-found', '-o', 'name')
        assert not crd_existing.strip(), 'Refusing existing lab AgentMesh CRD'
        if not NAMESPACE_MODE:
            k(c, 'apply', '-f', str(HERE / 'crd.yaml'))
            CREATED_CRDS.append(c)
            k(c, 'wait', '--for=condition=Established', 'crd/agentmeshtrustedbundles.' + ctl.GROUP, '--timeout=60s')
        apply(c, {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {
            'name': NS, 'labels': {'istio-injection': 'enabled'}}})
        CREATED_NAMESPACES.append(c)
    roots = {c: get(c, 'cm', 'istio-ca-root-cert', 'istio-system')['data']['root-cert.pem'] for c in ['a', 'b']}
    assert roots['a'] != roots['b']
    for c in ['a', 'b']:
        if NAMESPACE_MODE:
            for resource in yaml.safe_load_all((HERE / 'install-namespaced.yaml').read_text()):
                resource['metadata']['namespace'] = NS
                for subject in resource.get('subjects', []):
                    subject['namespace'] = NS
                k(c, 'apply', '-f', '-', data=json.dumps(resource))
            bundle(c, roots['a'] + roots['b'])
        else:
            bundle(c, roots['a'] + roots['b'])
            for kind, names in [('namespace', [CTL_NS]), ('clusterrole', [ctl.MANAGER, 'agentmesh-developer', 'agentmesh-trust-admin']),
                                ('clusterrolebinding', [ctl.MANAGER])]:
                assert not k(c, 'get', kind, *names, '--ignore-not-found', '-o', 'name').strip(), 'Refusing existing installation resources'
            CREATED_INSTALLS.append(c)
            for resource in yaml.safe_load_all((HERE / 'install.yaml').read_text()):
                if resource['kind'] == 'Namespace':
                    resource['metadata']['name'] = CTL_NS
                if 'namespace' in resource['metadata']:
                    resource['metadata']['namespace'] = CTL_NS
                for subject in resource.get('subjects', []):
                    subject['namespace'] = CTL_NS
                k(c, 'apply', '-f', '-', ns=CTL_NS, data=json.dumps(resource))
        ready(c, 'mesh-access-controller')
        check_build(c)
    probe = workload('https-probe', 'python:3.12-slim', ['sleep', 'infinity'], sa='default')
    probe['spec']['template']['metadata']['annotations'] = {'sidecar.istio.io/inject': 'false'}
    probe['spec']['template']['spec']['automountServiceAccountToken'] = False
    apply('a', probe)
    ready('a', 'https-probe')
    for name in ['caller', 'denied', 'plain', 'local-control']:
        apply('a', obj('ServiceAccount', name))
        apply('a', nginx(name, 'local-control') if name == 'local-control' else
              workload(name, 'curlimages/curl:8.10.1', ['sleep', 'infinity']))
    apply('a', obj('Service', 'local-control', {'selector': {'app': 'local-control'},
        'ports': [{'name': 'http', 'port': 80, 'targetPort': 8080}]}))
    apply('a', obj('PeerAuthentication', 'strict', {'mtls': {'mode': 'STRICT'}}, api=ctl.SEC))
    for name in ['backend', 'ingressgateway']:
        apply('b', obj('ServiceAccount', name))
    apply('b', nginx('backend', 'mesh-access-backend'))
    apply('b', obj('Service', 'backend', {'selector': {'app': 'backend'},
        'ports': [{'name': 'http', 'port': 80, 'targetPort': 8080}]}))
    apply('b', obj('PeerAuthentication', 'strict', {'mtls': {'mode': 'STRICT'}}, api=ctl.SEC))
    apply('b', obj('AuthorizationPolicy', 'backend-gateway-only', {'selector': {'matchLabels': {'app': 'backend'}},
        'action': 'ALLOW', 'rules': [{'from': [{'source': {'principals': [
            'cluster-b-mesh/ns/' + NS + '/sa/ingressgateway']}}]}]}, api=ctl.SEC))
    original = get('b', 'deployment', 'istio-ingressgateway', 'istio-system')
    gateway = obj('Deployment', 'ingressgateway', copy.deepcopy(original['spec']), api='apps/v1')
    labels = {'app': 'mesh-access-ingress', 'istio': 'mesh-access-ingress'}
    gateway['spec']['selector']['matchLabels'] = labels
    gateway['spec']['template']['metadata']['labels'] = labels
    gateway['spec']['template']['spec']['serviceAccountName'] = 'ingressgateway'
    gateway['spec']['template']['spec']['terminationGracePeriodSeconds'] = 0
    for env in gateway['spec']['template']['spec']['containers'][0].get('env', []):
        if env['name'] == 'ISTIO_META_WORKLOAD_NAME':
            env['value'] = 'ingressgateway'
        if env['name'] == 'ISTIO_META_OWNER':
            env['value'] = 'kubernetes://apis/apps/v1/namespaces/' + NS + '/deployments/ingressgateway'
    k('b', 'apply', '-f', str(HERE / 'fixtures/gateway-rbac.yaml'))
    apply('b', gateway)
    apply('b', obj('Service', 'ingressgateway', {'type': 'NodePort', 'selector': labels,
        'ports': [{'name': 'https', 'port': 443, 'targetPort': 8443, 'nodePort': NODEPORT}]}))
    # Sign gateway DNS certificate using real B CA. Never persist/log private data.
    ca = get('b', 'secret', 'istio-ca-secret', 'istio-system')['data']
    with tempfile.TemporaryDirectory(prefix='mesh-access-cert-') as td:
        td = pathlib.Path(td)
        for key in ['ca-key.pem', 'ca-cert.pem']:
            (td / key).write_bytes(base64.b64decode(ca[key]))
            os.chmod(td / key, 0o600)
        run(['openssl', 'req', '-new', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(td / 'key.pem'),
             '-out', str(td / 'csr.pem'), '-subj', '/CN=' + HOST])
        os.chmod(td / 'key.pem', 0o600)
        (td / 'ext.cnf').write_text('subjectAltName=DNS:' + HOST + ',DNS:' + NORMAL +
                                   '\nextendedKeyUsage=serverAuth\nbasicConstraints=CA:FALSE\n')
        run(['openssl', 'x509', '-req', '-in', str(td / 'csr.pem'), '-CA', str(td / 'ca-cert.pem'),
             '-CAkey', str(td / 'ca-key.pem'), '-CAcreateserial', '-days', '3',
             '-extfile', str(td / 'ext.cnf'), '-out', str(td / 'cert.pem')])
        secret = obj('Secret', 'mesh-access-gateway-tls')
        secret['type'] = 'kubernetes.io/tls'
        # Secret deliberately does NOT trust A; generated gateway filter must work.
        secret['stringData'] = {'tls.crt': (td / 'cert.pem').read_text(),
                                'tls.key': (td / 'key.pem').read_text(), 'ca.crt': roots['b']}
        apply('b', secret)
    del ca, secret
    node = run(['docker', 'inspect', 'cluster-b-control-plane', '--format',
                '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}']).strip()
    dns = get('a', 'cm', 'coredns', 'kube-system')
    SAVED_DNS['a'] = copy.deepcopy(dns['data'])
    core = dns['data']['Corefile']
    assert HOST not in core and NORMAL not in core
    if '    hosts {' in core:
        core = core.replace('    hosts {', '    hosts {\n        ' + node + ' ' + HOST + ' ' + NORMAL)
    else:
        core = core.replace('    ready', '    hosts {\n        ' + node + ' ' + HOST + ' ' + NORMAL + '\n        fallthrough\n    }\n    ready')
    k('a', 'patch', 'cm', 'coredns', '--type=merge', '--patch-file=/dev/stdin', ns='kube-system',
      data=json.dumps({'data': dict(dns['data'], Corefile=core)}))
    k('a', 'rollout', 'restart', 'deploy/coredns', ns='kube-system')
    k('a', 'rollout', 'status', 'deploy/coredns', '--timeout=90s', ns='kube-system')
    for name in ['caller', 'denied', 'plain', 'local-control']:
        ready('a', name)
    for name in ['ingressgateway', 'backend']:
        ready('b', name)
    for name in ['caller', 'denied']:
        apply('a', obj('AgentMeshEgress', name, {'serviceAccount': name,
            'inCluster': [{'host': 'local-control', 'port': 80, 'protocol': 'HTTP'}],
            'outCluster': [{'host': HOST, 'port': 443, 'protocol': 'MTLS',
                            'endpoint': node + ':' + str(NODEPORT)}]}, api=ctl.VERSION))
    expose = obj('AgentMeshExpose', 'backend', {
        'service': 'backend', 'port': 80, 'host': HOST, 'gatewaySelector': labels}, api=ctl.VERSION)
    owner_auth()
    # Unmanaged ordinary HTTPS host on the same gateway listener is a regression
    # control: no client certificate, same server Secret, distinct filter chain.
    apply('b', obj('Gateway', 'ordinary-https', {'selector': labels, 'servers': [{
        'hosts': [NORMAL], 'port': {'number': 443, 'name': 'https-normal', 'protocol': 'HTTPS'},
        'tls': {'mode': 'SIMPLE', 'credentialName': 'mesh-access-gateway-tls'}}]}, api=ctl.NET))
    apply('b', obj('VirtualService', 'ordinary-https', {'hosts': [NORMAL], 'gateways': ['ordinary-https'],
        'exportTo': ['.'], 'http': [{'route': [{'destination': {
            'host': 'backend.' + NS + '.svc.cluster.local', 'port': {'number': 80}}}]}]}, api=ctl.NET))
    apply('b', obj('DestinationRule', 'ordinary-https', {'host': 'backend.' + NS + '.svc.cluster.local',
        'exportTo': ['.'], 'trafficPolicy': {'tls': {'mode': 'ISTIO_MUTUAL'}}}, api=ctl.NET))
    # Gateway owner prepares listener/routing before AgentMeshExpose exists.
    apply('b', obj('Gateway', 'owner-mtls', {'selector': labels, 'servers': [{
        'hosts': [HOST], 'port': {'number': 443, 'name': 'https-owner-mtls', 'protocol': 'HTTPS'},
        'tls': {'mode': 'MUTUAL', 'credentialName': 'mesh-access-gateway-tls', 'subjectAltNames': [
            'spiffe://cluster-a-mesh/ns/' + NS + '/sa/caller',
            'spiffe://cluster-a-mesh/ns/' + NS + '/sa/denied',
            'spiffe://cluster-a-mesh/ns/' + NS + '-second/sa/caller',
            'spiffe://cluster-b-mesh/ns/' + NS + '/sa/local-caller']}}]}, api=ctl.NET))
    apply('b', obj('VirtualService', 'owner-mtls', {'hosts': [HOST], 'gateways': ['owner-mtls'],
        'exportTo': ['.'], 'http': [{'route': [{'destination': {
            'host': 'backend.' + NS + '.svc.cluster.local', 'port': {'number': 80}}}]}]}, api=ctl.NET))
    GATEWAY_BASELINE.update(gateway_state())
    apply('b', expose)
    configured('a', ['caller', 'denied'])
    configured('b', ['backend'])
    gateway_unchanged('initial exposure leaves owner-managed gateway resources unchanged')
    record('controllers running with namespaced ServiceAccounts', {'namespace': NS, 'clusters': ['a', 'b']})
    return roots, expose


def test(roots, expose):
    fresh()
    expect('authorized cross-cluster requester', 'caller', 200)
    expect('different real requester SA rejected at gateway', 'denied', 403)
    expect('normal local mesh traffic from selected requester', 'caller', 200, 'http://local-control/')
    expect('normal local mesh traffic from unselected requester', 'plain', 200, 'http://local-control/')
    source, gateway = dump('a', 'caller'), dump('b', 'ingressgateway')
    (E / 'source-active.json').write_text(json.dumps(source, indent=2))
    (E / 'gateway-active.json').write_text(json.dumps(gateway, indent=2))
    cluster = next(x['cluster'] for d in source['configs'] for x in d.get('dynamic_active_clusters', [])
                   if x['cluster']['name'] == 'outbound|443|' + ctl.SUBSET + '|' + HOST)
    tls = cluster['transport_socket']['typed_config']
    assert tls['sni'] == HOST
    common = tls['common_tls_context']
    assert [s['name'] for s in common['tls_certificate_sds_secret_configs']] == ['default']
    assert common['validation_context']['match_subject_alt_names'] == [{'exact': HOST}]
    assert common['validation_context']['trusted_ca']['inline_string'] == roots['a'] + roots['b']
    chains = {h: fc for d in gateway['configs'] for x in d.get('dynamic_listeners', [])
              for fc in x.get('active_state', {}).get('listener', {}).get('filter_chains', [])
              for h in fc.get('filter_chain_match', {}).get('server_names', [])}
    assert chains[HOST]['transport_socket']['typed_config']['require_client_certificate']
    assert not chains[NORMAL]['transport_socket']['typed_config'].get('require_client_certificate', False)
    gateway_validation = chains[HOST]['transport_socket']['typed_config']['common_tls_context']['validation_context']
    expected_sans = get('b', 'gateway', 'owner-mtls')['spec']['servers'][0]['tls']['subjectAltNames']
    assert gateway_validation['match_subject_alt_names'] == [{'exact': s} for s in expected_sans]
    record('owner client SAN checks retained in active gateway TLS context', True)
    # HTTPS directly from a separate non-injected test client: validates gateway certificate
    # against B root and deliberately supplies no client certificate.
    code = 'import ssl,urllib.request; c=ssl.create_default_context(cadata=' + repr(roots['b']) + '); print(urllib.request.urlopen(' + repr('https://' + NORMAL + ':' + str(NODEPORT) + '/') + ',context=c,timeout=8).read().decode())'
    ordinary = k('a', 'exec', 'deploy/https-probe', '--', 'python3', '-c', code)
    assert 'mesh-access-backend' in ordinary
    record('ordinary HTTPS same listener without client certificate', ordinary)
    record('active TLS context checks', {'sni': HOST, 'server_san': HOST, 'client_sds': ['default'],
                                        'gateway_requires_client_cert': True, 'ordinary_https_requires_client_cert': False})
    check_rbac('a')
    check_rbac('b')
    gateway_uid = get('b', 'pods', json.loads(k('b', 'get', 'pods', '-l', 'app=mesh-access-ingress', '-o', 'json'))['items'][0]['metadata']['name'])['metadata']['uid']
    # Reconciler must overwrite generated drift, not silently adopt it.
    drname = ctl.name('remote', HOST)
    k('a', 'patch', 'destinationrule', drname, '--type=json', '--patch-file=/dev/stdin', data=json.dumps([
        {'op': 'replace', 'path': '/spec/subsets/0/trafficPolicy/tls/mode', 'value': 'DISABLE'}]))
    wait('DestinationRule drift corrected', lambda: get('a', 'destinationrule', drname)['spec']['subsets'][0]['trafficPolicy']['tls']['mode'] == 'ISTIO_MUTUAL')
    record('generated DestinationRule drift corrected', True)
    bundle('a', roots['a'])
    wait('source bundle update reconciled', lambda: roots['b'] not in get('a', 'envoyfilter', ctl.name('requester', 'caller'))['spec']['configPatches'][0]['patch']['value']['transport_socket']['typed_config']['common_tls_context']['validation_context']['trusted_ca']['inline_string'])
    fresh()
    expect('requester rejects gateway after B CA removed', 'caller', 503)
    bundle('a', roots['a'] + roots['b'])
    wait('source bundle restored', lambda: roots['b'] in get('a', 'envoyfilter', ctl.name('requester', 'caller'))['spec']['configPatches'][0]['patch']['value']['transport_socket']['typed_config']['common_tls_context']['validation_context']['trusted_ca']['inline_string'])
    bundle('b', roots['b'])
    wait('gateway bundle update reconciled', lambda: roots['a'] not in get('b', 'envoyfilter', ctl.name('expose', HOST))['spec']['configPatches'][0]['patch']['value']['transport_socket']['typed_config']['common_tls_context']['validation_context']['trusted_ca']['inline_string'])
    fresh()
    expect('gateway rejects requester after A CA removed', 'caller', 503)
    bundle('b', roots['a'] + roots['b'])
    time.sleep(7)
    fresh()
    expect('both trust bundles restored through controller', 'caller', 200)
    owner_auth(['cluster-a-mesh/ns/' + NS + '/sa/denied'])
    fresh()
    expect('authorization update revokes original caller', 'caller', 403)
    expect('authorization update allows other real caller', 'denied', 200)
    owner_auth()
    fresh()
    expect('original authorization restored', 'caller', 200)
    unchanged = gateway_uid == json.loads(k('b', 'get', 'pods', '-l', 'app=mesh-access-ingress', '-o', 'json'))['items'][0]['metadata']['uid']
    assert unchanged, 'Gateway unexpectedly rolled during trust/auth updates'
    record('gateway did not roll during trust/auth updates', unchanged)
    gateway_unchanged('trust and authorization updates leave owner-managed gateway resources unchanged')
    delete_declarations('a', 'egress', 'denied')
    wait('deleted caller filter pruned', lambda: not k('a', 'get', 'envoyfilter', ctl.name('requester', 'denied'), '--ignore-not-found', '-o', 'name').strip())
    wait('deleted caller route pruned', lambda: len(get('a', 'virtualservice', ctl.name('remote', HOST))['spec']['http'][0]['match']) == 1)
    expect('remaining caller works after shared destination deletion', 'caller', 200)
    record('request deletion prunes only revoked caller', True)
    for c in ['a', 'b']:
        delete_declarations(c)
        wait('all generated resources pruned in ' + c, lambda c=c: not json.loads(k(c, 'get',
             'sidecar,service,serviceentry,destinationrule,virtualservice,envoyfilter,gateway,authorizationpolicy',
             '-l', ctl.MANAGED + '=' + ctl.MANAGER, '-o', 'json'))['items'])
    record('last CR deletion prunes generated resources; originals retained', {
        'original_backend_service': get('b', 'service', 'backend')['metadata']['name'],
        'ordinary_gateway': get('b', 'gateway', 'ordinary-https')['metadata']['name']})
    gateway_unchanged('exposure deletion retains all owner-managed gateway resources')


def cleanup():
    for c, data in SAVED_DNS.items():
        k(c, 'patch', 'cm', 'coredns', '--type=json', '--patch-file=/dev/stdin', ns='kube-system',
          data=json.dumps([{'op': 'replace', 'path': '/data', 'value': data}]))
        k(c, 'rollout', 'restart', 'deploy/coredns', ns='kube-system')
        k(c, 'rollout', 'status', 'deploy/coredns', '--timeout=90s', ns='kube-system')
        assert get(c, 'cm', 'coredns', 'kube-system')['data'] == data
    for c in CREATED_INSTALLS:
        k(c, 'delete', 'namespace', CTL_NS, '--ignore-not-found', '--wait=true', '--timeout=90s')
        k(c, 'delete', 'clusterrolebinding', ctl.MANAGER, '--ignore-not-found')
        k(c, 'delete', 'clusterrole', ctl.MANAGER, 'agentmesh-developer', 'agentmesh-trust-admin', '--ignore-not-found')
    for c in CREATED_NAMESPACES:
        k(c, 'delete', 'namespace', NS, '--wait=true', '--timeout=90s')
    for c in CREATED_CRDS:
        k(c, 'delete', 'crd', *[p + '.' + ctl.GROUP for p in ['agentmeshtrustedbundles', 'agentmeshegresses', 'agentmeshexposes']], '--wait=true', '--timeout=60s')
    record('cleanup', 'CoreDNS restored; isolated namespaces and test CRDs removed. Existing PoC fixtures preserved.')


if __name__ == '__main__':
    try:
        roots, expose = setup()
        test(roots, expose)
        for c in CREATED_NAMESPACES:
            (E / (c + '-controller.log')).write_text(k(c, 'logs', 'deploy/mesh-access-controller', '--tail=100', check=False))
            (E / (c + '-agentmesh.json')).write_text(k(c, 'get', 'agentmeshegress,agentmeshexpose,agentmeshtrustedbundle', '-o', 'json', check=False))
            (E / (c + '-pods.txt')).write_text(k(c, 'get', 'pods', '-o', 'wide', check=False))
        record('complete', 'Live namespace controllers reconciled real independent-CA mTLS and passed negative controls.')
    except Exception:
        for c in CREATED_NAMESPACES:
            (E / (c + '-controller.log')).write_text(k(c, 'logs', 'deploy/mesh-access-controller', '--tail=100', check=False))
            (E / (c + '-agentmesh.json')).write_text(k(c, 'get', 'agentmeshegress,agentmeshexpose,agentmeshtrustedbundle', '-o', 'json', check=False))
            (E / (c + '-pods.txt')).write_text(k(c, 'get', 'pods', '-o', 'wide', check=False))
        raise
    finally:
        cleanup()
