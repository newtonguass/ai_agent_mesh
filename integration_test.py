#!/usr/bin/env python3
"""Destructive only to isolated mesh-access-poc-* namespaces on the two kind labs.

Requires running cluster-a/b and the controller image loaded in both. Restores
CoreDNS and deletes its namespaces/CRDs in finally. Never use on company contexts.
Signing keys exist only in memory and temporary mode-0600 files; no Secret dumps.
"""
import base64
import copy
import json
import os
import pathlib
import subprocess
import tempfile
import time

import yaml
import controller as ctl

HERE = pathlib.Path(__file__).resolve().parent
E = HERE / 'evidence'
E.mkdir(exist_ok=True)
NS = 'mesh-access-poc-' + str(int(time.time()))
HOST = 'controller.gateway.test'
NORMAL = 'normal.controller.test'
NODEPORT = 31543
RESULTS = []
SAVED_DNS = {}
CREATED_CRDS = []
CREATED_NAMESPACES = []


def run(args, data=None, check=True, timeout=150):
    p = subprocess.run(args, input=data, text=True, capture_output=True, timeout=timeout)
    if check and p.returncode:
        raise RuntimeError('Command failed: ' + ' '.join(args[:8]) + '\n' + p.stderr[:2000])
    return p.stdout + (p.stderr if p.returncode else '')


def k(c, *args, ns=NS, data=None, check=True):
    return run(['kubectl', '--context', 'kind-cluster-' + c, '-n', ns, *args], data, check)


def get(c, kind, name, ns=NS):
    return json.loads(k(c, 'get', kind, name, '-o', 'json', ns=ns))


def apply(c, obj):
    return k(c, 'apply', '-f', '-', data=json.dumps(obj))


def obj(kind, name, spec=None, api='v1'):
    d = {'apiVersion': api, 'kind': kind, 'metadata': {'name': name, 'namespace': NS}}
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
            d = get(c, 'meshaccess', name)
            conditions = d.get('status', {}).get('conditions', [])
            if not any(x['type'] == 'Configured' and x['status'] == 'True' and
                       x['observedGeneration'] == d['metadata']['generation'] for x in conditions):
                return False
        return True
    return wait('MeshAccess Configured in ' + c, check)


def bundle(c, pem):
    d = obj('ConfigMap', 'mesh-access-trust')
    d['data'] = {'ca.crt': pem}
    apply(c, d)


def setup():
    for c in ['a', 'b']:
        existing = k(c, 'get', 'namespace', NS, '--ignore-not-found', '-o', 'name')
        assert not existing.strip(), 'Refusing existing namespace ' + NS
        crd_existing = k(c, 'get', 'crd', *[p + '.' + ctl.GROUP for p in ['meshaccesses', 'agentmeshegresses', 'agentmeshexposes']], '--ignore-not-found', '-o', 'name')
        assert not crd_existing.strip(), 'Refusing existing lab MeshAccess CRD'
        k(c, 'apply', '-f', str(HERE / 'crd.yaml'))
        CREATED_CRDS.append(c)
        k(c, 'wait', '--for=condition=Established', 'crd/meshaccesses.' + ctl.GROUP, '--timeout=60s')
        apply(c, {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {
            'name': NS, 'labels': {'istio-injection': 'enabled'}}})
        CREATED_NAMESPACES.append(c)
    roots = {c: get(c, 'cm', 'istio-ca-root-cert', 'istio-system')['data']['root-cert.pem'] for c in ['a', 'b']}
    assert roots['a'] != roots['b']
    for c in ['a', 'b']:
        bundle(c, roots['a'] + roots['b'])
        k(c, 'apply', '-f', str(HERE / 'install.yaml'))
        ready(c, 'mesh-access-controller')
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
    k('b', 'apply', '-f', str(HERE / 'gateway-rbac.yaml'))
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
        apply('a', obj('MeshAccess', name, {'serviceAccount': name,
            'requests': [{'url': 'https://' + HOST, 'endpoint': node + ':' + str(NODEPORT)}]}, api=ctl.VERSION))
    expose = obj('MeshAccess', 'backend', {'serviceAccount': 'backend', 'exposes': [{
        'service': 'backend', 'url': 'https://' + HOST,
        'allow': ['cluster-a-mesh/ns/' + NS + '/sa/caller']}]}, api=ctl.VERSION)
    apply('b', expose)
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
    configured('a', ['caller', 'denied'])
    configured('b', ['backend'])
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
    # HTTPS directly from non-injected controller pod: validates gateway certificate
    # against B root and deliberately supplies no client certificate.
    code = 'import ssl,urllib.request; c=ssl.create_default_context(cadata=' + repr(roots['b']) + '); print(urllib.request.urlopen(' + repr('https://' + NORMAL + ':' + str(NODEPORT) + '/') + ',context=c,timeout=8).read().decode())'
    ordinary = k('a', 'exec', 'deploy/mesh-access-controller', '--', 'python3', '-c', code)
    assert 'mesh-access-backend' in ordinary
    record('ordinary HTTPS same listener without client certificate', ordinary)
    record('active TLS context checks', {'sni': HOST, 'server_san': HOST, 'client_sds': ['default'],
                                        'gateway_requires_client_cert': True, 'ordinary_https_requires_client_cert': False})
    # Actual pod controller RBAC cannot read another namespace or any Secrets.
    code = "import sys; sys.path.insert(0,'/app'); import controller as c; a=c.Kube('" + NS + "');\nfor p in ['/api/v1/namespaces/kube-system/pods','/api/v1/namespaces/" + NS + "/secrets']:\n try: a.call('GET',p); raise AssertionError('unexpected permission')\n except c.APIError as e: assert e.code==403; print(p, e.code)"
    record('controller RBAC boundaries', k('a', 'exec', 'deploy/mesh-access-controller', '--', 'python3', '-c', code))
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
    wrong = copy.deepcopy(expose)
    wrong['spec']['exposes'][0]['allow'] = ['cluster-a-mesh/ns/' + NS + '/sa/denied']
    apply('b', wrong)
    configured('b', ['backend'])
    fresh()
    expect('authorization update revokes original caller', 'caller', 403)
    expect('authorization update allows other real caller', 'denied', 200)
    apply('b', expose)
    configured('b', ['backend'])
    fresh()
    expect('original authorization restored', 'caller', 200)
    unchanged = gateway_uid == json.loads(k('b', 'get', 'pods', '-l', 'app=mesh-access-ingress', '-o', 'json'))['items'][0]['metadata']['uid']
    assert unchanged, 'Gateway unexpectedly rolled during trust/auth updates'
    record('gateway did not roll during trust/auth updates', unchanged)
    k('a', 'delete', 'meshaccess', 'denied')
    wait('deleted caller filter pruned', lambda: not k('a', 'get', 'envoyfilter', ctl.name('requester', 'denied'), '--ignore-not-found', '-o', 'name').strip())
    wait('deleted caller route pruned', lambda: len(get('a', 'virtualservice', ctl.name('remote', HOST))['spec']['http'][0]['match']) == 1)
    expect('remaining caller works after shared destination deletion', 'caller', 200)
    record('request deletion prunes only revoked caller', True)
    for c in ['a', 'b']:
        k(c, 'delete', 'meshaccess', '--all')
        wait('all generated resources pruned in ' + c, lambda c=c: not json.loads(k(c, 'get',
             'service,serviceentry,destinationrule,virtualservice,envoyfilter,gateway,authorizationpolicy',
             '-l', ctl.MANAGED + '=' + ctl.MANAGER, '-o', 'json'))['items'])
    record('last CR deletion prunes generated resources; originals retained', {
        'original_backend_service': get('b', 'service', 'backend')['metadata']['name'],
        'ordinary_gateway': get('b', 'gateway', 'ordinary-https')['metadata']['name']})


def cleanup():
    for c, data in SAVED_DNS.items():
        k(c, 'patch', 'cm', 'coredns', '--type=json', '--patch-file=/dev/stdin', ns='kube-system',
          data=json.dumps([{'op': 'replace', 'path': '/data', 'value': data}]))
        k(c, 'rollout', 'restart', 'deploy/coredns', ns='kube-system')
        k(c, 'rollout', 'status', 'deploy/coredns', '--timeout=90s', ns='kube-system')
        assert get(c, 'cm', 'coredns', 'kube-system')['data'] == data
    for c in CREATED_NAMESPACES:
        k(c, 'delete', 'namespace', NS, '--wait=true', '--timeout=90s')
    for c in CREATED_CRDS:
        k(c, 'delete', 'crd', *[p + '.' + ctl.GROUP for p in ['meshaccesses', 'agentmeshegresses', 'agentmeshexposes']], '--wait=true', '--timeout=60s')
    record('cleanup', 'CoreDNS restored; isolated namespaces and test CRDs removed. Existing PoC fixtures preserved.')


if __name__ == '__main__':
    try:
        roots, expose = setup()
        test(roots, expose)
        for c in CREATED_NAMESPACES:
            (E / (c + '-controller.log')).write_text(k(c, 'logs', 'deploy/mesh-access-controller', '--tail=100', check=False))
            (E / (c + '-meshaccess.json')).write_text(k(c, 'get', 'meshaccess', '-o', 'json', check=False))
            (E / (c + '-pods.txt')).write_text(k(c, 'get', 'pods', '-o', 'wide', check=False))
        record('complete', 'Live namespace controllers reconciled real independent-CA mTLS and passed negative controls.')
    except Exception:
        for c in CREATED_NAMESPACES:
            (E / (c + '-controller.log')).write_text(k(c, 'logs', 'deploy/mesh-access-controller', '--tail=100', check=False))
            (E / (c + '-meshaccess.json')).write_text(k(c, 'get', 'meshaccess', '-o', 'json', check=False))
            (E / (c + '-pods.txt')).write_text(k(c, 'get', 'pods', '-o', 'wide', check=False))
        raise
    finally:
        cleanup()
