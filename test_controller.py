"""Behavior tests: scoping, revocation, ownership, drift and namespace RBAC paths."""
import copy
import json
import pathlib
import unittest

import controller as c
import egress as e


HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent / 'independent-ca-envoyfilter/gateway-envoyfilter/evidence/gateway-before.json'


def pem_fixture():
    # Public certificate only. Standard system trust store makes tests standalone.
    import ssl
    ctx = ssl.create_default_context()
    return ssl.DER_cert_to_PEM_cert(ctx.get_ca_certs(binary_form=True)[0])


def pod(name, sa, labels=None):
    return {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': name, 'labels': labels or {},
            'resourceVersion': '1'}, 'spec': {'serviceAccountName': sa,
            'containers': [{'name': 'istio-proxy', 'ports': [{'name': 'https', 'containerPort': 8443}]}]}}


def cr(name, sa, requests=(), exposes=()):
    metadata = {'name': name, 'namespace': 'team', 'uid': name, 'generation': 1, 'resourceVersion': '1'}
    if exposes:
        ex = exposes[0]
        spec = {'service': ex['service'], 'port': ex.get('port', 80),
                'host': c.origin(ex['url'])[0], 'gatewaySelector': {'app': 'ingress'}, 'allow': ex['allow']}
        kind = e.EXPOSE
    else:
        spec = {'serviceAccount': sa, 'outCluster': [dict(
            host=c.origin(r['url'])[0], port=c.origin(r['url'])[1], protocol='MTLS',
            **({'endpoint': r['endpoint']} if 'endpoint' in r else {})) for r in requests]}
        kind = e.EGRESS
    return {'apiVersion': c.VERSION, 'kind': kind, 'metadata': metadata, 'spec': spec}


class FakeAPI:
    namespace = 'team'

    def __init__(self, inputs):
        self.data, self.writes = {}, []
        for obj in inputs:
            self.save(obj)

    def save(self, obj):
        obj = copy.deepcopy(obj)
        obj['metadata'].setdefault('uid', 'uid-' + obj['metadata']['name'])
        obj['metadata'].setdefault('resourceVersion', '1')
        self.data[(obj['kind'], obj['metadata']['name'])] = obj

    def get(self, kind, name):
        if (kind, name) not in self.data:
            raise c.APIError(404, 'not found')
        return copy.deepcopy(self.data[(kind, name)])

    def list(self, kind):
        return [copy.deepcopy(v) for (k, n), v in self.data.items() if k == kind]

    def create(self, obj):
        self.writes.append(('create', obj['kind'], obj['metadata']['name']))
        self.save(obj)

    def delete(self, obj):
        self.writes.append(('delete', obj['kind'], obj['metadata']['name']))
        del self.data[(obj['kind'], obj['metadata']['name'])]

    def patch(self, kind, name, body, status=False, json_patch=False):
        self.writes.append(('patch', kind, name))
        current = self.data[(kind, name)]
        if json_patch:
            for op in body:
                if op['op'] == 'test':
                    assert current['metadata']['resourceVersion'] == op['value']
                else:
                    current[op['path'].lstrip('/')] = copy.deepcopy(op['value'])
        elif status:
            current['status'] = copy.deepcopy(body['status'])
        elif 'spec' in body:
            changes = body['spec']['template']['metadata']
            template = current['spec']['template']
            for field, values in changes.items():
                for key, value in values.items():
                    if value is None:
                        template['metadata'].setdefault(field, {}).pop(key, None)
                    else:
                        template['metadata'].setdefault(field, {})[key] = value
            # Simulate the workload's normal rollout creating a new pod.
            if ('Pod', name) in self.data:
                self.data[('Pod', name)]['metadata']['labels'] = copy.deepcopy(template['metadata'].get('labels', {}))
                self.data[('Pod', name)]['metadata']['annotations'] = copy.deepcopy(template['metadata'].get('annotations', {}))
        else:
            for k, v in body['metadata'].get('labels', {}).items():
                if v is None:
                    current['metadata'].setdefault('labels', {}).pop(k, None)
                else:
                    current['metadata'].setdefault('labels', {})[k] = v


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.pem = pem_fixture()
        self.config = {'schemaVersion': 1, 'gateway': {'service': 'ingress', 'port': 443,
                                                      'credentialName': 'gateway-tls'}}
        self.owner = {'name': 'mesh-access-config', 'uid': 'config-uid'}
        self.pods = [pod('caller', 'caller'), pod('other', 'other'),
                     pod('backend', 'backend', {'app': 'backend'}),
                     pod('gateway', 'gateway', {'app': 'ingress'})]
        self.services = {
            'backend': {'apiVersion': 'v1', 'kind': 'Service', 'metadata': {'name': 'backend'},
                        'spec': {'selector': {'app': 'backend'}, 'ports': [{'port': 80, 'targetPort': 8080}]}},
            'ingress': {'apiVersion': 'v1', 'kind': 'Service', 'metadata': {'name': 'ingress'},
                        'spec': {'selector': {'app': 'ingress'}, 'ports': [{'port': 443, 'targetPort': 'https'}]}}}
        self.accounts = {'caller', 'other', 'backend', 'gateway'}
        self.request = cr('caller', 'caller', [{'url': 'https://remote.test'}])
        self.expose = cr('backend', 'backend', exposes=[{'service': 'backend', 'url': 'https://exposed.test',
                                                       'allow': ['remote-mesh/ns/agents/sa/caller']}])

    def plan(self, accesses):
        normalized, _, _ = e.normalize('team', self.config, accesses, self.services, self.pods, self.accounts)
        return c.render('team', self.config, self.owner, self.pem, normalized,
                        self.services, self.pods, self.accounts)

    def fake(self, accesses):
        cm = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': self.owner,
              'data': {'config.json': json.dumps(self.config)}}
        trust = {'apiVersion': c.VERSION, 'kind': 'AgentMeshTrustedBundle', 'metadata': {'name': 'mesh-access-trust'},
                 'spec': {'caBundle': self.pem}}
        accounts = [{'apiVersion': 'v1', 'kind': 'ServiceAccount', 'metadata': {'name': sa}} for sa in self.accounts]
        _, labels, _ = self.plan(accesses)
        pods = copy.deepcopy(self.pods)
        workloads = []
        for p in pods:
            wanted = labels.get(p['metadata']['name'], {})
            p['metadata']['labels'].update(wanted)
            if wanted:
                p['metadata']['annotations'] = {c.STAMP: json.dumps(wanted, sort_keys=True, separators=(',', ':'))}
            workloads.append({'apiVersion': 'apps/v1', 'kind': 'Deployment',
                              'metadata': {'name': p['metadata']['name']},
                              'spec': {'template': {'metadata': {'labels': copy.deepcopy(p['metadata']['labels']),
                                        'annotations': copy.deepcopy(p['metadata'].get('annotations', {}))},
                                        'spec': copy.deepcopy(p['spec'])}}})
        return FakeAPI([cm, trust, *accounts, *pods, *workloads, *self.services.values(), *accesses])

    def test_requester_preserves_identity_and_exact_server_san(self):
        objects, labels, urls = self.plan([self.request])
        ef = next(d for d in objects.values() if d['kind'] == 'EnvoyFilter')['spec']
        self.assertEqual(ef['workloadSelector']['labels'], {c.LABEL: c.sa_label('caller')})
        patch = ef['configPatches'][0]
        self.assertEqual(patch['match']['cluster'], {'service': 'remote.test', 'subset': c.SUBSET})
        common = patch['patch']['value']['transport_socket']['typed_config']['common_tls_context']
        self.assertEqual(common['validation_context']['match_subject_alt_names'], [{'exact': 'remote.test'}])
        self.assertNotIn('tls_certificate_sds_secret_configs', common)
        self.assertNotIn('other', labels)
        self.assertEqual(urls['AgentMeshEgress/caller'], ['http://remote.test:443'])
        vs = next(d for d in objects.values() if d['kind'] == 'VirtualService')['spec']
        self.assertEqual(vs['http'][0]['match'][0]['sourceLabels'], {c.LABEL: c.sa_label('caller')})
        self.assertNotIn('subset', vs['http'][1]['route'][0]['destination'])
        dr = next(d for d in objects.values() if d['kind'] == 'DestinationRule')['spec']
        self.assertNotIn('trafficPolicy', dr)
        self.assertNotIn('workloadSelector', dr)
        for d in objects.values():
            if d['kind'] != 'EnvoyFilter':
                self.assertEqual(d['spec']['exportTo'], ['.'])

    def test_shared_host_aggregates_two_service_accounts(self):
        objects, _, _ = self.plan([self.request, cr('other', 'other', [{'url': 'https://remote.test'}])])
        self.assertEqual(sum(k[0] == 'VirtualService' for k in objects), 1)
        self.assertEqual(sum(k[0] == 'EnvoyFilter' for k in objects), 2)
        vs = next(d for d in objects.values() if d['kind'] == 'VirtualService')
        self.assertEqual(len(vs['spec']['http'][0]['match']), 2)

    def test_gateway_port_sni_authorization_and_alias_service(self):
        before = copy.deepcopy(self.services)
        objects, labels, _ = self.plan([self.expose])
        ef = next(d for d in objects.values() if d['kind'] == 'EnvoyFilter')['spec']
        self.assertEqual(ef['configPatches'][0]['match']['listener'],
                         {'portNumber': 8443, 'filterChain': {'sni': 'exposed.test'}})
        self.assertEqual(ef['workloadSelector']['labels'][c.GW_LABEL], c.digest('team'))
        policy = next(d for d in objects.values() if d['kind'] == 'AuthorizationPolicy')['spec']
        self.assertEqual(policy['action'], 'DENY')
        self.assertEqual(policy['rules'][0]['to'][0]['operation']['hosts'], ['exposed.test', 'exposed.test:*'])
        self.assertEqual(self.services, before)
        alias = next(d for d in objects.values() if d['kind'] == 'Service')
        self.assertEqual(alias['spec']['selector'], {'app': 'backend'})
        self.assertNotIn('backend', labels)
        self.assertEqual(alias['spec']['ports'][0]['targetPort'], 8080)

    def test_conflicting_endpoints_and_duplicate_sa_rejected(self):
        for other in [cr('other', 'other', [{'url': 'https://remote.test', 'endpoint': 'elsewhere.test:443'}]),
                      cr('duplicate', 'caller', [{'url': 'https://another.test'}])]:
            with self.assertRaises(c.Invalid):
                self.plan([self.request, other])

    def test_backend_different_sa_accepted_but_sidecar_required(self):
        self.pods[2]['spec']['serviceAccountName'] = 'other'
        self.plan([self.expose])
        self.pods[2]['spec']['serviceAccountName'] = 'backend'
        self.pods[2]['spec']['containers'] = [{'name': 'app'}]
        with self.assertRaisesRegex(c.Invalid, 'istio-proxy'):
            self.plan([self.expose])

    def test_url_and_bundle_validation(self):
        for value in ['http://remote.test', 'https://remote.test/path', 'https://user@remote.test',
                      'https://1.2.3.4', 'https://*.test', 'https://remote.test:0', 'https://remote.test?x=y']:
            with self.assertRaises(c.Invalid, msg=value):
                c.origin(value)
        for value in ['', 'not a certificate', self.pem + '\n-----BEGIN PRIVATE KEY-----\n']:
            with self.assertRaises(c.Invalid):
                c.public_bundle(value)

    def test_reconcile_idempotence_drift_and_deletion(self):
        api = self.fake([self.request])
        self.assertTrue(c.reconcile(api, 'mesh-access-config'))
        api.writes.clear()
        self.assertTrue(c.reconcile(api, 'mesh-access-config'))
        self.assertEqual(api.writes, [])
        key = ('DestinationRule', c.name('remote', 'remote.test'))
        api.data[key]['spec']['subsets'][0]['trafficPolicy']['tls']['mode'] = 'DISABLE'
        self.assertTrue(c.reconcile(api, 'mesh-access-config'))
        self.assertEqual(api.data[key]['spec']['subsets'][0]['trafficPolicy']['tls']['mode'], 'ISTIO_MUTUAL')
        del api.data[(e.EGRESS, 'caller')]
        self.assertTrue(c.reconcile(api, 'mesh-access-config'))
        self.assertNotIn(key, api.data)
        self.assertNotIn(c.LABEL, api.data[('Pod', 'caller')]['metadata']['labels'])
        self.assertIn(('Service', 'backend'), api.data)

    def test_revocation_removes_old_route_and_filter_without_touching_other_sa(self):
        api = self.fake([self.request, cr('other', 'other', [{'url': 'https://remote.test'}])])
        self.assertTrue(c.reconcile(api, 'mesh-access-config'))
        del api.data[(e.EGRESS, 'caller')]
        self.assertTrue(c.reconcile(api, 'mesh-access-config'))
        vs = api.data[('VirtualService', c.name('remote', 'remote.test'))]
        self.assertEqual(len(vs['spec']['http'][0]['match']), 1)
        self.assertNotIn(('EnvoyFilter', c.name('requester', 'caller')), api.data)
        self.assertIn(('EnvoyFilter', c.name('requester', 'other')), api.data)

    def test_ca_rotation_and_removed_allow_entry_replace_old_values(self):
        api = self.fake([self.expose])
        self.assertTrue(c.reconcile(api, 'mesh-access-config'))
        api.data[('AgentMeshTrustedBundle', 'mesh-access-trust')]['spec']['caBundle'] = self.pem + self.pem
        api.data[(e.EXPOSE, 'backend')]['spec']['allow'] = ['new-mesh/ns/new/sa/new']
        self.assertTrue(c.reconcile(api, 'mesh-access-config'))
        policy = api.data[('AuthorizationPolicy', c.name('expose', 'exposed.test'))]
        self.assertEqual(policy['spec']['rules'][0]['from'][0]['source']['notPrincipals'], ['new-mesh/ns/new/sa/new'])
        ef = api.data[('EnvoyFilter', c.name('expose', 'exposed.test'))]
        self.assertEqual(ef['spec']['configPatches'][0]['patch']['value']['transport_socket']['typed_config']
                         ['common_tls_context']['validation_context']['trusted_ca']['inline_string'], self.pem * 2)

    def test_unowned_collision_preserved_and_status_false(self):
        api = self.fake([self.request])
        api.save({'apiVersion': c.NET, 'kind': 'VirtualService',
                  'metadata': {'name': 'manual'}, 'spec': {'hosts': ['*.test'], 'http': []}})
        self.assertFalse(c.reconcile(api, 'mesh-access-config'))
        self.assertEqual({w[1] for w in api.writes}, {e.EGRESS, 'AgentMeshTrustedBundle'})
        self.assertEqual(api.data[(e.EGRESS, 'caller')]['status']['conditions'][0]['status'], 'False')

    def test_initial_enrollment_stamps_template_and_waits_for_rollout(self):
        api = self.fake([self.request])
        api.data[('Pod', 'caller')]['metadata']['labels'].pop(c.LABEL)
        api.data[('Pod', 'caller')]['metadata'].pop('annotations')
        template = api.data[('Deployment', 'caller')]['spec']['template']
        template['metadata']['labels'].pop(c.LABEL)
        template['metadata'].pop('annotations')
        self.assertFalse(c.reconcile(api, 'mesh-access-config'))
        self.assertIn(('patch', 'Deployment', 'caller'), api.writes)
        self.assertNotIn(('patch', 'Pod', 'caller'), api.writes)
        self.assertEqual(template['metadata']['labels'][c.LABEL], c.sa_label('caller'))
        self.assertTrue(c.reconcile(api, 'mesh-access-config'))

    def test_list_normalizes_missing_kubernetes_type_metadata(self):
        api = c.Kube('team', api_url='http://127.0.0.1:8001')
        pages = [{'items': [{'metadata': {'name': 'one'}}], 'metadata': {'continue': 'next'}},
                 {'items': [{'metadata': {'name': 'two'}}]}]
        api.call = lambda *args: pages.pop(0)
        items = api.list('Service')
        self.assertEqual([x['kind'] for x in items], ['Service', 'Service'])
        self.assertEqual([x['apiVersion'] for x in items], ['v1', 'v1'])

    def test_native_sidecar_is_detected_for_requester_and_gateway_port(self):
        for p in self.pods:
            native = p['spec']['containers'].pop()
            native['restartPolicy'] = 'Always'
            p['spec']['containers'] = [{'name': 'application'}]
            p['spec']['initContainers'] = [{'name': 'istio-init'}, native]
        objects, _, _ = self.plan([self.request, self.expose])
        ef = objects[('EnvoyFilter', c.name('expose', 'exposed.test'))]
        self.assertEqual(ef['spec']['configPatches'][0]['match']['listener']['portNumber'], 8443)
        self.assertTrue(c.has_proxy(self.pods[0]))

    def test_finite_init_container_is_not_a_proxy(self):
        self.pods[0]['spec']['initContainers'] = self.pods[0]['spec'].pop('containers')
        self.pods[0]['spec']['containers'] = [{'name': 'application'}]
        self.assertFalse(c.has_proxy(self.pods[0]))
        with self.assertRaisesRegex(c.Invalid, 'lacks istio-proxy'):
            self.plan([self.request])

    def test_all_api_paths_are_namespaced(self):
        api = c.Kube('team', api_url='http://127.0.0.1:8001')
        for kind in c.KINDS:
            self.assertIn('/namespaces/team/', api.path(kind, 'test'))


if __name__ == '__main__':
    unittest.main()
