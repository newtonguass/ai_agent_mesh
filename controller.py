#!/usr/bin/env python3
"""Namespace-scoped AgentMesh reconciler. Python standard library only.

Targets Istio 1.13.5 sidecars and MUTUAL termination gateways. Configuration
convergence is not evidence of xDS acceptance or successful application traffic.
"""
import argparse
import copy
import datetime
import hashlib
import ipaddress
import json
import logging
import os
import re
import signal
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request

GROUP = 'agentmesh.io'
VERSION = GROUP + '/v1alpha1'
# Persisted workload/ownership metadata is independent of the public API group.
# Preserve it during the API rename to avoid forced rollouts or losing ownership
# of existing generated resources. Users do not set these labels manually.
METADATA_GROUP = 'mesh-access.example.com'
LABEL = METADATA_GROUP + '/service-account'
GW_LABEL = METADATA_GROUP + '/gateway'
STAMP = METADATA_GROUP + '/bootstrap-labels'
MANAGED = METADATA_GROUP + '/managed-by'
MANAGER = 'mesh-access-controller'
SUBSET = 'mesh-access-mtls'
NET = 'networking.istio.io/v1alpha3'
SEC = 'security.istio.io/v1beta1'
KINDS = {
    'ConfigMap': ('v1', 'configmaps'), 'Service': ('v1', 'services'),
    'Pod': ('v1', 'pods'), 'ServiceAccount': ('v1', 'serviceaccounts'),
    'AgentMeshTrustedBundle': (VERSION, 'agentmeshtrustedbundles'),
    'AgentMeshEgress': (VERSION, 'agentmeshegresses'),
    'AgentMeshExpose': (VERSION, 'agentmeshexposes'),
    'Sidecar': (NET, 'sidecars'),
    'Deployment': ('apps/v1', 'deployments'),
    'StatefulSet': ('apps/v1', 'statefulsets'),
    'DaemonSet': ('apps/v1', 'daemonsets'),
    **{k: (NET, p) for k, p in [('ServiceEntry', 'serviceentries'),
       ('DestinationRule', 'destinationrules'), ('VirtualService', 'virtualservices'),
       ('EnvoyFilter', 'envoyfilters'), ('Gateway', 'gateways')]},
    'AuthorizationPolicy': (SEC, 'authorizationpolicies'),
}
# Apply trust and authorization before publishing gateway/routes. Withdraw routes
# before pruning their dependencies. Kubernetes/xDS updates are not transactional.
ORDER = ['Sidecar', 'Service', 'ServiceEntry', 'DestinationRule', 'EnvoyFilter',
         'AuthorizationPolicy', 'Gateway', 'VirtualService']


class Invalid(ValueError):
    pass


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def sa_label(sa):
    return 'sa-' + digest(sa)


def name(prefix, value):
    return 'ma-' + prefix + '-' + digest(value)


def require(ok, message):
    if not ok:
        raise Invalid(message)


def dns(value, what='hostname'):
    require(isinstance(value, str) and len(value) <= 253 and
            re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', value) and
            all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', x)
                for x in value.split('.')), 'Invalid ' + what + ': ' + str(value))
    return value


def origin(value):
    try:
        u = urllib.parse.urlsplit(value)
        require(u.scheme == 'https' and u.hostname and not u.username and
                not u.password and u.path in ('', '/') and not u.query and not u.fragment,
                'url must be an HTTPS origin, without credentials, path, query or fragment')
        host = dns(u.hostname)
        require('.' in host and value == value.strip() and u.netloc == u.netloc.lower(),
                'url requires a lowercase fully qualified DNS hostname')
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise Invalid('url must use a DNS name for SNI/server SAN, not an IP')
        port = 443 if u.port is None else u.port
        require(1 <= port <= 65535, 'Invalid URL port')
        return host, port
    except (ValueError, TypeError) as e:
        raise Invalid(str(e)) from e


def endpoint(value, host, port):
    if not value:
        return host, port
    try:
        u = urllib.parse.urlsplit('//' + value)
        require(u.hostname and u.port and not u.username and not u.password and
                not u.path and not u.query and not u.fragment,
                'endpoint must be DNS-name:port or IPv4:port')
        address = dns(u.hostname, 'endpoint')
        require(1 <= u.port <= 65535, 'Invalid endpoint port')
        return address, u.port
    except (ValueError, TypeError) as e:
        raise Invalid(str(e)) from e


def public_bundle(pem):
    require(isinstance(pem, str) and 0 < len(pem.encode()) <= 256 * 1024,
            'Trust bundle must contain 1..262144 bytes; larger bundles need a file/SDS implementation')
    pattern = r'-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----'
    certs = re.findall(pattern, pem)
    require(certs and not re.sub(pattern, '', pem).strip(),
            'Trust bundle must contain only PEM certificates, never private keys')
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(cadata=pem)
    except ssl.SSLError as e:
        raise Invalid('Invalid trust bundle certificates: ' + str(e)) from e
    return pem


def selected(pod, selector):
    return all(pod['metadata'].get('labels', {}).get(k) == v for k, v in selector.items())


def workload_containers(pod):
    # Kubernetes native sidecars are restartable init containers. A one-shot
    # init container must not count as a running proxy or a gateway listener.
    return pod['spec'].get('containers', []) + [
        c for c in pod['spec'].get('initContainers', []) if c.get('restartPolicy') == 'Always']


def has_proxy(pod):
    return any(c['name'] == 'istio-proxy' for c in workload_containers(pod))


def render(namespace, config, owner, bundle, accesses, services, pods, accounts):
    """Pure, deterministic namespace plan; validates all input before writes."""
    require(namespace not in config.get('forbiddenNamespaces', ['istio-system']),
            'Refusing Istio root configuration namespace; install in an application namespace')
    require(config.get('schemaVersion') == 1, 'config.json schemaVersion must be 1')
    objects, labels, applications = {}, {}, {}
    bundle = public_bundle(bundle) if any(a['spec'].get('requests') or a['spec'].get('exposes') for a in accesses) else bundle
    gateway = config.get('gateway', {})
    callers, incoming, seen_sa = {}, {}, set()

    def obj(kind, resource_name, spec):
        key = (kind, resource_name)
        require(key not in objects, 'Generated resource collision: ' + str(key))
        d = {'apiVersion': KINDS[kind][0], 'kind': kind,
             'metadata': {'name': resource_name, 'namespace': namespace,
                          'labels': {MANAGED: MANAGER},
                          'ownerReferences': [{'apiVersion': 'v1', 'kind': 'ConfigMap',
                            'name': owner['name'], 'uid': owner['uid'], 'controller': True,
                            'blockOwnerDeletion': False}]}, 'spec': spec}
        if kind == 'Service':
            d['metadata']['annotations'] = {'networking.istio.io/exportTo': '.'}
        objects[key] = d
        return d

    for access in sorted(accesses, key=lambda a: a['metadata']['name']):
        spec, crname = access['spec'], access['metadata']['name']
        sa = spec.get('serviceAccount')
        applications[crname] = []
        if sa is not None:
            dns(sa, 'serviceAccount')
            require(sa in accounts, crname + ': ServiceAccount does not exist: ' + sa)
            require(sa not in seen_sa, 'Use one AgentMeshEgress per serviceAccount: ' + sa)
            seen_sa.add(sa)
            for pod in pods:
                if pod['spec'].get('serviceAccountName', 'default') == sa:
                    require(has_proxy(pod), crname + ': pod lacks istio-proxy: ' + pod['metadata']['name'])
                    labels.setdefault(pod['metadata']['name'], {})[LABEL] = sa_label(sa)
        for request in spec.get('requests', []):
            host, port = origin(request['url'])
            require(not host.endswith('.svc.cluster.local') or request.get('_internal'), 'requests supports remote DNS origins only')
            server = request.get('_serverName', host)
            internal = request.get('_internal', False)
            ep = endpoint(request.get('endpoint'), host, port)
            if host in callers:
                require(callers[host]['port'] == port and callers[host]['endpoint'] == ep and callers[host]['server'] == server and callers[host]['internal'] == internal,
                        'Conflicting port/endpoint for requested host ' + host)
            item = callers.setdefault(host, {'port': port, 'endpoint': ep, 'server': server, 'internal': internal, 'sas': set()})
            require(sa not in item['sas'], 'Duplicate request URL for ' + sa + ': ' + host)
            item['sas'].add(sa)
            applications[crname].append('http://' + host + ':' + str(port))
        for exposure in spec.get('exposes', []):
            host, _ = origin(exposure['url'])
            require(host not in incoming, 'Duplicate exposed hostname: ' + host)
            svcname = dns(exposure['service'], 'service')
            require(svcname in services, 'Backend Service does not exist: ' + svcname)
            svc = services[svcname]
            require(svc['spec'].get('selector') and svc['spec'].get('type') != 'ExternalName',
                    'Exposed Service must select local pods')
            svcports = svc['spec'].get('ports', [])
            port = exposure.get('port')
            require(port is not None or len(svcports) == 1,
                    'Specify port for multi-port Service ' + svcname)
            matches = [p for p in svcports if port is None or p['port'] == port]
            require(len(matches) == 1, 'Backend Service port not found: ' + svcname)
            sp = matches[0]
            require(sp.get('protocol', 'TCP') == 'TCP', 'Only HTTP over TCP backends supported')
            for pod in pods:
                if selected(pod, svc['spec']['selector']):
                    require(has_proxy(pod), 'Backend requires istio-proxy: ' + svcname)
            allow = sorted(set(exposure.get('allow', [])))
            require(allow, 'exposes.allow must explicitly list allowed requester SPIFFE principals')
            for principal in allow:
                require(isinstance(principal, str) and re.fullmatch(
                    r'[a-z0-9.-]+/ns/[a-z0-9-]+/sa/[a-z0-9.-]+', principal),
                    'Use exact trust-domain/ns/namespace/sa/account principals without spiffe:// or wildcards')
            incoming[host] = (svc, sp, allow, exposure.get('_gateway', gateway))

    require(not set(callers).intersection(incoming),
            'Same-namespace gateway calls require native Service routing; do not also request an exposed host')
    # One registry/route/rule per remote host, shared safely by all declared SAs.
    patches = {}
    for host, item in sorted(callers.items()):
        port, (address, epport) = item['port'], item['endpoint']
        resource_name = name('remote', host)
        try:
            ipaddress.ip_address(address)
            resolution = 'STATIC'
        except ValueError:
            resolution = 'DNS'
        if not item['internal']:
            obj('ServiceEntry', resource_name, {'hosts': [host], 'exportTo': ['.'],
                'location': 'MESH_EXTERNAL', 'resolution': resolution,
                'ports': [{'number': port, 'name': 'http-mtls', 'protocol': 'HTTP'}],
                'endpoints': [{'address': address, 'ports': {'http-mtls': epport}}]})
        obj('DestinationRule', resource_name, {'host': host, 'exportTo': ['.'],
            'subsets': [{'name': SUBSET, 'trafficPolicy': {'tls': {
                'mode': 'ISTIO_MUTUAL', 'sni': item['server'], 'subjectAltNames': [item['server']]}}}]})
        obj('VirtualService', resource_name, {'hosts': [host], 'exportTo': ['.'],
            'gateways': ['mesh'], 'http': [{'match': [
                {'sourceLabels': {LABEL: sa_label(sa)}, 'port': port} for sa in sorted(item['sas'])],
                'route': [{'destination': {'host': host, 'port': {'number': port}, 'subset': SUBSET}}]},
                {'route': [{'destination': {'host': host, 'port': {'number': port}}}]}]})
        if item['server'] != host:
            objects[('VirtualService', resource_name)]['spec']['http'][0]['rewrite'] = {'authority': item['server']}
        for sa in sorted(item['sas']):
            patches.setdefault(sa, []).append({'applyTo': 'CLUSTER', 'match': {
                'context': 'SIDECAR_OUTBOUND', 'cluster': {'service': host, 'subset': SUBSET}},
                'patch': {'operation': 'MERGE', 'value': {'transport_socket': {
                    'name': 'envoy.transport_sockets.tls', 'typed_config': {
                        '@type': 'type.googleapis.com/envoy.extensions.transport_sockets.tls.v3.UpstreamTlsContext',
                        # Replaces the validation oneof. Re-add the exact target SAN
                        # explicitly; preserve ISTIO_MUTUAL's client certificate SDS.
                        'common_tls_context': {'validation_context': {
                            'trusted_ca': {'inline_string': bundle},
                            'match_subject_alt_names': [{'exact': item['server']}]}}}}}}})
    for sa, config_patches in sorted(patches.items()):
        obj('EnvoyFilter', name('requester', sa), {'workloadSelector': {'labels': {LABEL: sa_label(sa)}},
                                               'configPatches': config_patches})

    for host, (svc, sp, allow, gateway) in sorted(incoming.items()):
        require(gateway.get('service') in services, 'Configure gateway.service in the namespace config')
        dns(gateway.get('credentialName'), 'gateway credentialName')
        gsvc = services[gateway['service']]
        selector = gateway.get('_selector', gsvc['spec'].get('selector'))
        require(selector, 'Gateway Service must select local gateway pods')
        port = gateway.get('port', 443)
        gps = [p for p in gsvc['spec'].get('ports', []) if p['port'] == port]
        require(len(gps) == 1, 'gateway.port not found in gateway Service')
        gp = gps[0]
        require(gp.get('protocol', 'TCP') == 'TCP', 'Gateway port must be TCP')
        listener_port = gp.get('targetPort', port)
        gpodos = [p for p in pods if selected(p, selector)]
        require(gpodos, 'No gateway pods match gateway Service selector')
        if isinstance(listener_port, str):
            numbers = {p['containerPort'] for pod in gpodos for c in workload_containers(pod)
                       for p in c.get('ports', []) if p.get('name') == listener_port}
            require(len(numbers) == 1, 'Cannot resolve gateway named targetPort consistently')
            listener_port = numbers.pop()
        # Unique namespace label also constrains old Istio Gateway selectors that
        # otherwise can select workloads across namespace boundaries.
        gw_selector = dict(selector, **{GW_LABEL: digest(namespace)})
        for pod in gpodos:
            require(has_proxy(pod), 'Gateway pod missing istio-proxy')
            labels.setdefault(pod['metadata']['name'], {})[GW_LABEL] = digest(namespace)
        n = name('expose', host)
        backend = n + '-backend'
        backend_host = backend + '.' + namespace + '.svc.cluster.local'
        obj('Service', backend, {'selector': copy.deepcopy(svc['spec']['selector']),
            'ports': [{'name': 'http-mesh', 'port': sp['port'],
                       'targetPort': sp.get('targetPort', sp['port']), 'protocol': 'TCP'}]})
        obj('DestinationRule', n, {'host': backend_host, 'exportTo': ['.'],
                                   'trafficPolicy': {'tls': {'mode': 'ISTIO_MUTUAL'}}})
        obj('EnvoyFilter', n, {'workloadSelector': {'labels': gw_selector}, 'configPatches': [{
            'applyTo': 'FILTER_CHAIN', 'match': {'context': 'GATEWAY', 'listener': {
                'portNumber': listener_port, 'filterChain': {'sni': host}}},
            'patch': {'operation': 'MERGE', 'value': {'transport_socket': {
                'name': 'envoy.transport_sockets.tls', 'typed_config': {
                    '@type': 'type.googleapis.com/envoy.extensions.transport_sockets.tls.v3.DownstreamTlsContext',
                    'require_client_certificate': True, 'common_tls_context': {
                        'validation_context': {'trusted_ca': {'inline_string': bundle}}}}}}}}]})
        # DENY with a host condition doesn't introduce an ALLOW policy that
        # would deny unrelated gateway hosts. Existing ALLOW policies still apply.
        obj('AuthorizationPolicy', n, {'selector': {'matchLabels': gw_selector}, 'action': 'DENY',
            'rules': [{'from': [{'source': {'notPrincipals': allow}}],
                       'to': [{'operation': {'hosts': [host, host + ':*']}}]}]})
        obj('Gateway', n, {'selector': gw_selector, 'servers': [{
            'hosts': [namespace + '/' + host],
            'port': {'number': port, 'name': 'https-' + digest(host), 'protocol': 'HTTPS'},
            'tls': {'mode': 'MUTUAL', 'credentialName': gateway['credentialName']}}]})
        obj('VirtualService', n, {'hosts': [host], 'exportTo': ['.'], 'gateways': [n],
            'http': [{'route': [{'destination': {'host': backend_host, 'port': {'number': sp['port']}}}]}]})

    for d in objects.values():
        require(len(json.dumps(d).encode()) < 900 * 1024,
                'Generated object exceeds 900 KiB; reduce destination count/bundle size')
    return objects, labels, applications


class APIError(RuntimeError):
    def __init__(self, code, message):
        super().__init__('Kubernetes HTTP ' + str(code) + ': ' + message[:1000])
        self.code = code


class Kube:
    def __init__(self, namespace, api_url=None, token_file=None, ca_file=None):
        self.namespace = namespace
        self.url = api_url or 'https://kubernetes.default.svc'
        self.token_file = token_file or '/var/run/secrets/kubernetes.io/serviceaccount/token'
        ca_file = ca_file or '/var/run/secrets/kubernetes.io/serviceaccount/ca.crt'
        if self.url.startswith('https://'):
            self.context = ssl.create_default_context(cafile=ca_file)
        else:
            require(self.url.startswith('http://127.0.0.1:'), 'HTTP API allowed only for local kubectl proxy')
            self.context = None

    def path(self, kind, resource_name=None):
        api, plural = KINDS[kind]
        prefix = '/api/v1' if api == 'v1' else '/apis/' + api
        path = prefix + '/namespaces/' + urllib.parse.quote(self.namespace, safe='') + '/' + plural
        return path + ('/' + urllib.parse.quote(resource_name, safe='') if resource_name else '')

    def call(self, method, path, body=None, content_type='application/json'):
        headers = {'Accept': 'application/json', 'Content-Type': content_type}
        if self.context:
            # Re-read the projected token on every call to support token rotation.
            with open(self.token_file) as f:
                headers['Authorization'] = 'Bearer ' + f.read().strip()
        req = urllib.request.Request(self.url + path, method=method, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, context=self.context, timeout=20) as response:
                return json.load(response)
        except urllib.error.HTTPError as e:
            # Never log request bodies (they may include trust material).
            raise APIError(e.code, e.read().decode(errors='replace')) from e

    def get(self, kind, resource_name):
        return self.call('GET', self.path(kind, resource_name))

    def list(self, kind):
        items, token = [], ''
        while True:
            query = urllib.parse.urlencode({'limit': 500, 'continue': token})
            result = self.call('GET', self.path(kind) + '?' + query)
            # Typed Kubernetes LIST responses may omit TypeMeta on each item.
            for item in result['items']:
                item.setdefault('kind', kind)
                item.setdefault('apiVersion', KINDS[kind][0])
            items.extend(result['items'])
            token = result.get('metadata', {}).get('continue', '')
            if not token:
                return items

    def patch(self, kind, resource_name, body, status=False, json_patch=False):
        return self.call('PATCH', self.path(kind, resource_name) + ('/status' if status else ''), body,
                         'application/json-patch+json' if json_patch else 'application/merge-patch+json')

    def create(self, obj):
        return self.call('POST', self.path(obj['kind']), obj)

    def delete(self, obj):
        try:
            self.call('DELETE', self.path(obj['kind'], obj['metadata']['name']),
                      {'apiVersion': 'v1', 'kind': 'DeleteOptions',
                       'preconditions': {'uid': obj['metadata']['uid'],
                                         'resourceVersion': obj['metadata']['resourceVersion']}})
        except APIError as e:
            if e.code != 404:
                raise


def owned(obj, owner):
    return obj['metadata'].get('labels', {}).get(MANAGED) == MANAGER and any(
        r.get('uid') == owner['uid'] and r.get('kind') == 'ConfigMap'
        for r in obj['metadata'].get('ownerReferences', []))


def comparable_spec(obj):
    spec = copy.deepcopy(obj['spec'])
    if obj['kind'] == 'Service':
        for key in ['clusterIP', 'clusterIPs', 'ipFamilies', 'ipFamilyPolicy',
                    'internalTrafficPolicy', 'sessionAffinity', 'type']:
            spec.pop(key, None)
    return spec


def apply_owned(api, desired, current, owner):
    if current is None:
        api.create(desired)
        return
    require(owned(current, owner), 'Refusing to overwrite unowned ' + desired['kind'] + '/' + desired['metadata']['name'])
    if comparable_spec(current) == comparable_spec(desired) and all(
        current['metadata'].get(k, {}).get(a) == b
        for k in ['labels', 'annotations'] for a, b in desired['metadata'].get(k, {}).items()
    ):
        return
    spec = copy.deepcopy(desired['spec'])
    if desired['kind'] == 'Service':
        for key in ['clusterIP', 'clusterIPs', 'ipFamilies', 'ipFamilyPolicy',
                    'internalTrafficPolicy', 'sessionAffinity', 'type']:
            if key in current['spec']:
                spec[key] = current['spec'][key]
    # Replace spec, not recursive merge: removed targets/roots/allow entries must
    # actually disappear. resourceVersion precondition prevents lost updates.
    metadata = copy.deepcopy(current['metadata'])
    for field in ['labels', 'annotations']:
        metadata.setdefault(field, {}).update(desired['metadata'].get(field, {}))
    api.patch(desired['kind'], desired['metadata']['name'], [
        {'op': 'test', 'path': '/metadata/resourceVersion', 'value': current['metadata']['resourceVersion']},
        {'op': 'replace', 'path': '/spec', 'value': spec},
        {'op': 'replace', 'path': '/metadata', 'value': metadata}], json_patch=True)


def status(api, access, ok, message, urls=()):
    old = access.get('status', {})
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    condition = {'type': 'Configured', 'status': 'True' if ok else 'False',
                 'reason': 'Reconciled' if ok else 'ReconcileFailed', 'message': message[:1000],
                 'observedGeneration': access['metadata'].get('generation', 1)}
    prior = next((c for c in old.get('conditions', []) if c['type'] == 'Configured'), {})
    condition['lastTransitionTime'] = prior.get('lastTransitionTime', now) if prior.get('status') == condition['status'] else now
    value = {'observedGeneration': access['metadata'].get('generation', 1),
             'applicationURLs': sorted(urls), 'conditions': [condition]}
    if value != old:
        api.patch(access['kind'], access['metadata']['name'], {'metadata': {
            'resourceVersion': access['metadata']['resourceVersion']}, 'status': value}, status=True)


def reconcile(api, config_name):
    import egress
    accesses = [a for kind in ['AgentMeshEgress', 'AgentMeshExpose', 'AgentMeshTrustedBundle']
                for a in api.list(kind) if not a['metadata'].get('deletionTimestamp')]
    try:
        cm = api.get('ConfigMap', config_name)
        config = json.loads(cm['data']['config.json'])
        owner = cm['metadata']
        # A staged, unselected certificate must not freeze active permissions,
        # especially revocations. Validate it independently and report its error
        # on that bundle; only required trust can block the active plan.
        bundles, bundle_errors = {}, {}
        for a in accesses:
            if a['kind'] != 'AgentMeshTrustedBundle':
                continue
            key = a['metadata']['name']
            try:
                bundles[key] = public_bundle(a['spec']['caBundle'])
            except Invalid as error:
                bundle_errors[key] = str(error)
        declarations = [a for a in accesses if a['kind'] != 'AgentMeshTrustedBundle']
        bundle_name = config.get('trustBundle', {}).get('name', 'mesh-access-trust')
        needs_trust = any(a['kind'] == 'AgentMeshExpose' or any(
            d.get('protocol', '').upper() == 'MTLS' for field in ('inCluster', 'outCluster')
            for d in a['spec'].get(field, [])) for a in declarations)
        require(not needs_trust or bundle_name not in bundle_errors,
                'Invalid required AgentMeshTrustedBundle ' + bundle_name + ': ' + bundle_errors.get(bundle_name, ''))
        require(not needs_trust or bundle_name in bundles,
                'AgentMeshTrustedBundle does not exist: ' + bundle_name)
        bundle = bundles.get(bundle_name, '')
        services = {s['metadata']['name']: s for s in api.list('Service')}
        pods = [p for p in api.list('Pod') if not p['metadata'].get('deletionTimestamp') and
                p.get('status', {}).get('phase') not in ['Succeeded', 'Failed']]
        accounts = {s['metadata']['name'] for s in api.list('ServiceAccount')}
        normalized, plans, extra_urls = egress.normalize(api.namespace, config, declarations, services, pods, accounts)
        desired, labels, urls = render(api.namespace, config, owner, bundle, normalized, services, pods, accounts)
        egress.render(api.namespace, owner, plans, desired)
        urls.update(extra_urls)
        for d in desired.values():
            require(len(json.dumps(d).encode()) < 900 * 1024, 'Generated object exceeds 900 KiB')
        current = {(k, d['metadata']['name']): d for k in ORDER
                   for d in (services.values() if k == 'Service' else api.list(k))}
        for d in current.values():
            if d['kind'] == 'Sidecar' and not owned(d, owner):
                selector = d['spec'].get('workloadSelector', {}).get('labels')
                require(not selector or not any(selected(p, selector) and
                    p['spec'].get('serviceAccountName', 'default') in plans for p in pods),
                    'Unmanaged Sidecar overlaps enrolled ServiceAccount')
        # Check name ownership before any mutation, so a collision cannot cause a
        # half-applied plan. Also reject overlapping unmanaged exact/wildcard hosts.
        for key in desired:
            require(key not in current or owned(current[key], owner), 'Unowned resource name collision: ' + str(key))
        our_hosts = {h for d in desired.values() if d['kind'] in ['ServiceEntry', 'VirtualService']
                     for h in d['spec']['hosts']}
        for d in current.values():
            if owned(d, owner):
                continue
            spec = d['spec']
            hosts = spec.get('hosts', []) if d['kind'] in ['VirtualService', 'ServiceEntry'] else []
            if d['kind'] == 'DestinationRule':
                hosts = [spec['host']]
            if d['kind'] == 'Gateway':
                hosts = [h.split('/')[-1] for s in spec.get('servers', []) for h in s.get('hosts', [])]
            for h in hosts:
                require(not any(h == x or h == '*' or (h.startswith('*.') and x.endswith(h[1:])) for x in our_hosts),
                        'Unmanaged ' + d['kind'] + '/' + d['metadata']['name'] + ' overlaps requested/exposed hostname ' + h)
        # Istio 1.13.5 can retain proxy labels from bootstrap. Updating only
        # live pod labels is insufficient: stamp the owning workload template,
        # which triggers its normal rollout policy, before relying on selectors.
        sas = set(plans)
        gateway_selectors = [d['spec']['selector'] for d in desired.values() if d['kind'] == 'Gateway']
        gateway_selectors = [{k: v for k, v in sel.items() if k != GW_LABEL} for sel in gateway_selectors]
        for kind in ['Deployment', 'StatefulSet', 'DaemonSet']:
            for workload in api.list(kind):
                template = workload['spec']['template']
                wanted = {}
                if template['spec'].get('serviceAccountName', 'default') in sas:
                    wanted[LABEL] = sa_label(template['spec'].get('serviceAccountName', 'default'))
                if any(selected(template, selector) for selector in gateway_selectors):
                    wanted[GW_LABEL] = digest(api.namespace)
                old_labels = template['metadata'].get('labels', {})
                changes = {key: wanted.get(key) for key in [LABEL, GW_LABEL]
                           if old_labels.get(key) != wanted.get(key)}
                stamp = json.dumps(wanted, sort_keys=True, separators=(',', ':')) if wanted else None
                old_stamp = template['metadata'].get('annotations', {}).get(STAMP)
                if changes or old_stamp != stamp:
                    api.patch(kind, workload['metadata']['name'], {'metadata': {
                        'resourceVersion': workload['metadata']['resourceVersion']},
                        'spec': {'template': {'metadata': {
                            'labels': changes, 'annotations': {STAMP: stamp}}}}})
        pending = [pod['metadata']['name'] for pod in pods
                   if any(pod['metadata'].get('labels', {}).get(key) != value
                          for key, value in labels.get(pod['metadata']['name'], {}).items())
                   or (labels.get(pod['metadata']['name']) and
                       pod['metadata'].get('annotations', {}).get(STAMP) !=
                       json.dumps(labels[pod['metadata']['name']], sort_keys=True, separators=(',', ':')))]
        for kind in ORDER:
            for key, d in sorted(desired.items()):
                if key[0] == kind:
                    apply_owned(api, d, current.get(key), owner)
        for kind in reversed(ORDER):
            for key, d in current.items():
                if key[0] == kind and key not in desired and owned(d, owner):
                    api.delete(d)
        for a in accesses:
            if a['kind'] == 'AgentMeshTrustedBundle':
                if a['metadata']['name'] in bundle_errors:
                    status(api, a, False, bundle_errors[a['metadata']['name']])
                    continue
                status(api, a, True, 'Public PEM certificates validated. ' +
                       ('Selected namespace bundle; generated trust reconciled.' if a['metadata']['name'] == bundle_name
                        else 'Not selected by namespace config trustBundle.name.'))
                continue
            status(api, a, not pending,
                   ('Waiting for workload rollout with bootstrap labels: ' + ', '.join(pending) +
                    '. Jobs/bare pods require pre-stamped labels/annotation; OnDelete workloads require replacement.')
                   if pending else 'Resources reconciled. Verify Envoy active config and application traffic separately.',
                   urls.get(egress.status_key(a), []))
        return not pending
    except Exception as e:
        logging.error('Reconciliation failed: %s', e)
        for a in accesses:
            try:
                status(api, a, False, str(e), a.get('status', {}).get('applicationURLs', []))
            except Exception as se:
                logging.error('Cannot update status for %s: %s', a['metadata']['name'], se)
        return False


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--namespace', default=os.environ.get('POD_NAMESPACE'))
    p.add_argument('--config', default='mesh-access-config')
    p.add_argument('--interval', type=float, default=5)
    p.add_argument('--once', action='store_true')
    p.add_argument('--api-url', help='Only for local development through kubectl proxy')
    args = p.parse_args()
    require(args.namespace and args.interval >= 1, 'Namespace required; interval must be >= 1 second')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    api = Kube(args.namespace, api_url=args.api_url)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    while not stop.is_set():
        try:
            ok = reconcile(api, args.config)
        except Exception as e:
            logging.error('Cannot read namespace inputs: %s', e)
            ok = False
        if args.once:
            return 0 if ok else 1
        stop.wait(args.interval)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
