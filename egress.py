"""AgentMesh APIs: normalize declarations and render captured-egress policy.

This module is imported lazily by controller.py; it is not an entry point.
"""
import copy
import ipaddress
import re
import socket

import controller as c

EGRESS = 'AgentMeshEgress'
EXPOSE = 'AgentMeshExpose'


def number(value):
    c.require(type(value) is int and 1 <= value <= 65535, 'port must be 1..65535')
    return value


def ips(values):
    c.require(isinstance(values, list), 'addresses must be a list of individual IP addresses')
    try:
        return sorted({str(ipaddress.ip_address(v)) for v in values})
    except ValueError as e:
        raise c.Invalid('addresses requires individual IPs, not CIDRs') from e


def service_host(host, namespace):
    c.dns(host)
    if '.' not in host:
        return host + '.' + namespace + '.svc.cluster.local'
    c.require(host.endswith('.svc.cluster.local') and len(host.split('.')) == 5,
              'inCluster host must be a local short Service name or service.namespace.svc.cluster.local')
    return host


def resolve(host):
    try:
        return sorted({x[4][0] for x in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)})
    except socket.gaierror as e:
        raise c.Invalid('Cannot resolve internal Service ' + host) from e


def entry(item, internal, namespace, services, resolver):
    host = service_host(item['host'], namespace) if internal else c.origin('https://' + item['host'])[0]
    port = number(item['port'])
    proto = item['protocol'].upper()
    c.require(proto in ('HTTP', 'HTTPS', 'TCP', 'MTLS'), 'protocol must be HTTP, HTTPS, TCP or MTLS')
    c.require(internal or not host.endswith('.svc.cluster.local'), 'Use inCluster for Kubernetes Services')
    c.require(not internal or not item.get('endpoint'), 'Internal Services cannot override endpoint')
    c.require(proto == 'MTLS' or not item.get('serverName'), 'serverName is only for MTLS')
    server = c.origin('https://' + item.get('serverName', host))[0]
    aliases = [host]
    addresses = ips(item.get('addresses', []))
    if internal:
        parts = host.split('.')
        if parts[1] == namespace:
            c.require(parts[0] in services, 'Internal Service does not exist: ' + host)
            svc = services[parts[0]]['spec']
            c.require(svc.get('clusterIP') not in (None, 'None') and svc.get('type') != 'ExternalName',
                      'Internal whitelist requires a ClusterIP Service: ' + host)
            c.require(any(p['port'] == port and p.get('protocol', 'TCP') == 'TCP'
                          for p in svc.get('ports', [])), 'Internal TCP Service port not found: ' + host)
            if proto == 'MTLS':
                sp = next(p for p in svc['ports'] if p['port'] == port)
                c.require(sp.get('name', '').startswith(('http-', 'http2-')) or sp.get('name') in ('http', 'http2') or
                          sp.get('appProtocol') in ('http', 'http2', 'kubernetes.io/h2c'),
                          'Local gateway MTLS needs an HTTP-named Service port; use a dedicated HTTP Service alias')
            addresses = ips(svc.get('clusterIPs', [svc['clusterIP']]))
            aliases += [parts[0], parts[0] + '.' + namespace, parts[0] + '.' + namespace + '.svc']
        else:
            # DNS reads preserve namespace-only Kubernetes RBAC. No cross-namespace API access.
            addresses = ips(resolver(host))
            c.require(addresses, 'Internal Service has no resolved addresses: ' + host)
            aliases += [parts[0] + '.' + parts[1], parts[0] + '.' + parts[1] + '.svc']
    elif proto == 'TCP':
        c.require(addresses, 'External TCP requires addresses: no port-wide wildcard routing')
    c.require(proto == 'TCP' or not item.get('addresses'), 'Explicit addresses are only for TCP')
    ep = c.endpoint(item.get('endpoint'), host, port)
    if proto == 'TCP':
        c.require(not item.get('endpoint'), 'TCP endpoints are taken from addresses')
    return {'host': host, 'port': port, 'protocol': proto, 'internal': internal,
            'server': server, 'addresses': addresses, 'aliases': sorted(set(aliases)), 'endpoint': ep}


def normalize(namespace, config, accesses, services, pods, accounts, resolver=resolve):
    """Build separate requester and service-exposure plans; keep CR identity for status."""
    combined, plans, urls = {}, {}, {}
    for a in sorted(accesses, key=lambda x: (x['kind'], x['metadata']['name'])):
        spec, kind, key = a['spec'], a['kind'], status_key(a)
        target = {'metadata': {'name': key}, 'spec': {'requests': [], 'exposes': []}}
        combined[key] = target
        urls[key] = []
        if kind == EGRESS:
            sa = c.dns(spec['serviceAccount'], 'serviceAccount')
            c.require(sa in accounts, 'ServiceAccount does not exist: ' + sa)
            target['spec']['serviceAccount'] = sa
            c.require(sa not in plans, 'Use one AgentMeshEgress per serviceAccount: ' + sa)
            destinations = [entry(x, internal, namespace, services, resolver)
                            for field, internal in [('inCluster', True), ('outCluster', False)]
                            for x in spec.get(field, [])]
            seen = set()
            for d in destinations:
                identity = (d['host'], d['port'])
                c.require(identity not in seen, 'Duplicate destination host/port: ' + str(identity))
                seen.add(identity)
                if d['protocol'] == 'MTLS':
                    req = {'url': 'https://' + d['host'] + ':' + str(d['port']),
                           '_internal': d['internal'], '_serverName': d['server']}
                    if not d['internal']:
                        req['endpoint'] = d['endpoint'][0] + ':' + str(d['endpoint'][1])
                    target['spec']['requests'].append(req)
                scheme = {'HTTP': 'http', 'MTLS': 'http', 'HTTPS': 'https', 'TCP': 'tcp'}[d['protocol']]
                urls[key].append(scheme + '://' + d['host'] + ':' + str(d['port']))
            plans[sa] = destinations
        elif kind == EXPOSE:
            c.require('serviceAccount' not in spec, 'AgentMeshExpose exposes a Service; serviceAccount is not supported')
            host = c.origin('https://' + spec['host'])[0]
            rule = config.get('exposurePolicy', {})
            suffix = rule.get('dnsSuffix')
            c.require(not suffix or host.endswith('.' + c.dns(suffix)), 'Exposed host outside configured dnsSuffix')
            label_suffix = rule.get('labelSuffix')
            c.require(not label_suffix or host.split('.')[0].endswith(label_suffix),
                      'Exposed first DNS label must end with ' + str(label_suffix))
            selector = spec.get('gatewaySelector')
            c.require(isinstance(selector, dict) and selector and all(
                isinstance(k, str) and isinstance(v, str) and k and v for k, v in selector.items()),
                'gatewaySelector must contain nonempty labels')
            gateway = copy.deepcopy(config.get('gateway', {}))
            if spec.get('gatewayService'):
                gateway['service'] = spec['gatewayService']
            if spec.get('gatewayPort'):
                gateway['port'] = number(spec['gatewayPort'])
            if spec.get('credentialName'):
                gateway['credentialName'] = spec['credentialName']
            c.require(gateway.get('service') in services, 'Configure gateway Service for this exposure')
            gsel = services[gateway['service']]['spec'].get('selector', {})
            chosen = {p['metadata']['name'] for p in pods if c.selected(p, selector)}
            behind = {p['metadata']['name'] for p in pods if gsel and c.selected(p, gsel)}
            c.require(chosen and chosen == behind, 'gatewaySelector must select exactly the configured gateway Service pods')
            gateway['_selector'] = selector
            target['spec']['exposes'].append({'service': spec['service'], 'port': number(spec['port']),
                'url': 'https://' + host, 'allow': spec['allow'], '_gateway': gateway})
        else:
            raise c.Invalid('Unknown declaration kind ' + kind)
    return list(combined.values()), plans, urls


def status_key(a):
    return a['kind'] + '/' + a['metadata']['name']


def conjunction(*rules):
    return {'and_rules': {'rules': list(rules)}}


def policy(permissions):
    return {'action': 'ALLOW', 'policies': {
        'agent-mesh-allow': {'principals': [{'any': True}], 'permissions': permissions}}
        if permissions else {}}


def render(namespace, owner, plans, objects):
    def add(kind, n, spec):
        key = (kind, n)
        c.require(key not in objects, 'Egress resource collision: ' + str(key))
        objects[key] = {'apiVersion': c.KINDS[kind][0], 'kind': kind,
            'metadata': {'name': n, 'namespace': namespace, 'labels': {c.MANAGED: c.MANAGER},
                'ownerReferences': [{'apiVersion': 'v1', 'kind': 'ConfigMap', 'name': owner['name'],
                    'uid': owner['uid'], 'controller': True, 'blockOwnerDeletion': False}]}, 'spec': spec}

    existing_hosts = {h for d in objects.values() if d['kind'] == 'ServiceEntry' for h in d['spec']['hosts']}
    definitions = {}
    for sa, destinations in sorted(plans.items()):
        http, tcp, hosts = [], [], set()
        for d in destinations:
            host, port, proto = d['host'], d['port'], d['protocol']
            hosts.add((host.split('.')[1] if d['internal'] else '.') + '/' + host)
            if proto in ('HTTP', 'MTLS'):
                authorities = [v for h in d['aliases'] for v in (h, h + ':' + str(port))]
                http += [conjunction({'destination_port': port}, {'header': {
                    'name': ':authority', 'string_match': {'exact': authority,
                                                         'ignore_case': True}}}) for authority in authorities]
            elif proto == 'HTTPS':
                for alias in d['aliases']:
                    rules = [{'destination_port': port}, {'requested_server_name': {'exact': alias}}]
                    if d['internal']:
                        rules.append({'or_rules': {'rules': [{'destination_ip': {
                            'address_prefix': ip, 'prefix_len': ipaddress.ip_address(ip).max_prefixlen}}
                            for ip in d['addresses']]}})
                    tcp.append(conjunction(*rules))
            else:
                tcp += [conjunction({'destination_port': port}, {'destination_ip': {
                    'address_prefix': ip, 'prefix_len': ipaddress.ip_address(ip).max_prefixlen}})
                        for ip in d['addresses']]
            if not d['internal']:
                identity = host
                signature = (port, proto, d['endpoint'], tuple(d['addresses']), d['server'])
                c.require(identity not in definitions or definitions[identity] == signature,
                          'Conflicting external host definition across declarations: ' + host)
                first = identity not in definitions
                definitions[identity] = signature
                if first and proto != 'MTLS':
                    c.require(host not in existing_hosts, 'External host conflicts with existing MTLS declaration: ' + host)
                    ep, epport = d['endpoint']
                    se = {'hosts': [host], 'exportTo': ['.'], 'location': 'MESH_EXTERNAL',
                          'ports': [{'number': port, 'name': proto.lower(),
                                     'protocol': 'TLS' if proto == 'HTTPS' else proto}]}
                    if proto == 'TCP':
                        se.update(addresses=d['addresses'], resolution='STATIC',
                                  endpoints=[{'address': ip} for ip in d['addresses']])
                    else:
                        try:
                            ipaddress.ip_address(ep)
                            resolution = 'STATIC'
                        except ValueError:
                            resolution = 'DNS'
                        se.update(resolution=resolution, endpoints=[{'address': ep, 'ports': {proto.lower(): epport}}])
                    add('ServiceEntry', c.name('egress-service', host), se)
        add('Sidecar', c.name('egress-scope', sa), {
            'workloadSelector': {'labels': {c.LABEL: c.sa_label(sa)}},
            'outboundTrafficPolicy': {'mode': 'REGISTRY_ONLY'},
            'egress': [{'hosts': sorted(hosts) or ['~/*']}]})
        # HTTP checks both authority and original destination port. TCP checks
        # SNI+port for HTTPS, and exact IP+port for opaque TCP. No ANY permission.
        add('EnvoyFilter', c.name('egress-guard', sa), {
            'workloadSelector': {'labels': {c.LABEL: c.sa_label(sa)}},
            'configPatches': [
                {'applyTo': 'HTTP_FILTER', 'match': {'context': 'SIDECAR_OUTBOUND',
                    'listener': {'filterChain': {'filter': {'name': 'envoy.filters.network.http_connection_manager',
                        'subFilter': {'name': 'envoy.filters.http.router'}}}}},
                 'patch': {'operation': 'INSERT_BEFORE', 'value': {'name': 'agent_mesh.egress.http',
                    'typed_config': {'@type': 'type.googleapis.com/envoy.extensions.filters.http.rbac.v3.RBAC',
                                     'rules': policy(http)}}}},
                {'applyTo': 'NETWORK_FILTER', 'match': {'context': 'SIDECAR_OUTBOUND',
                    'listener': {'filterChain': {'filter': {'name': 'envoy.filters.network.tcp_proxy'}}}},
                 'patch': {'operation': 'INSERT_BEFORE', 'value': {'name': 'agent_mesh.egress.tcp',
                    'typed_config': {'@type': 'type.googleapis.com/envoy.extensions.filters.network.rbac.v3.RBAC',
                                     'stat_prefix': 'agent_mesh_egress', 'rules': policy(tcp)}}}}]})
