import copy
import unittest

import controller as c
import egress as e
import test_controller as fixtures
from test_controller import cr


class EgressTests(unittest.TestCase):
    plan = fixtures.ControllerTests.plan
    fake = fixtures.ControllerTests.fake
    def setUp(self):
        fixtures.ControllerTests.setUp(self)
        self.services['backend']['spec']['clusterIP'] = '10.1.2.3'
        self.services['ingress']['spec']['clusterIP'] = '10.1.2.4'
        self.services['ingress']['spec']['ports'][0]['name'] = 'http-origination'

    def api_cr(self, kind, name, spec):
        a = cr(name, spec.get('serviceAccount', 'unused'))
        a.update(kind=kind, spec=spec)
        return a

    def egress(self, **spec):
        return self.api_cr(e.EGRESS, 'caller', dict(serviceAccount='caller', **spec))

    def new_plan(self, accesses):
        normalized, plans, urls = e.normalize('team', self.config, accesses,
            self.services, self.pods, self.accounts, resolver=lambda h: ['10.2.3.4'])
        objects, labels, _ = c.render('team', self.config, self.owner, self.pem,
                                     normalized, self.services, self.pods, self.accounts)
        e.render('team', self.owner, plans, objects)
        return objects, labels, urls

    def test_empty_whitelist_denies_in_both_filter_types(self):
        objects, labels, _ = self.new_plan([self.egress()])
        ef = objects['EnvoyFilter', c.name('egress-guard', 'caller')]
        for p in ef['spec']['configPatches']:
            self.assertEqual(p['patch']['value']['typed_config']['rules'], {'action': 'ALLOW', 'policies': {}})
        self.assertIn('caller', labels)

    def test_mixed_protocols_and_explicit_ports(self):
        a = self.egress(inCluster=[{'host': 'backend', 'port': 80, 'protocol': 'HTTP'}], outCluster=[
            {'host': 'secure.test', 'port': 443, 'protocol': 'HTTPS'},
            {'host': 'remote.test', 'port': 443, 'protocol': 'MTLS'},
            {'host': 'db.test', 'port': 3306, 'protocol': 'TCP', 'addresses': ['192.0.2.1']}])
        objects, _, _ = self.new_plan([a])
        self.assertEqual(sum(k[0] == 'ServiceEntry' for k in objects), 3)
        guard = objects['EnvoyFilter', c.name('egress-guard', 'caller')]['spec']['configPatches']
        http = guard[0]['patch']['value']['typed_config']['rules']['policies']['agent-mesh-allow']['permissions']
        self.assertEqual({p['and_rules']['rules'][0]['destination_port'] for p in http}, {80, 443})
        tcp = guard[1]['patch']['value']['typed_config']['rules']['policies']['agent-mesh-allow']['permissions']
        self.assertEqual(len(tcp), 2)
        self.assertNotIn('any', str(tcp))

    def test_local_gateway_mtls_uses_native_service_and_rewrites_authority(self):
        a = self.egress(inCluster=[{'host': 'ingress', 'port': 443, 'protocol': 'MTLS', 'serverName': 'public.test'}])
        objects, _, _ = self.new_plan([a])
        self.assertFalse(any(k[0] == 'ServiceEntry' for k in objects))
        vs = objects['VirtualService', c.name('remote', 'ingress.team.svc.cluster.local')]['spec']
        self.assertEqual(vs['http'][0]['rewrite'], {'authority': 'public.test'})
        ef = objects['EnvoyFilter', c.name('requester', 'caller')]['spec']['configPatches'][0]
        self.assertEqual(ef['patch']['value']['transport_socket']['typed_config']['common_tls_context']
                         ['validation_context']['match_subject_alt_names'], [{'exact': 'public.test'}])

    def test_tcp_requires_individual_addresses(self):
        for addresses in [[], ['0.0.0.0/0'], ['not-an-ip']]:
            with self.assertRaises(c.Invalid):
                self.new_plan([self.egress(outCluster=[{'host': 'db.test', 'port': 3306,
                                                       'protocol': 'TCP', 'addresses': addresses}])])

    def test_duplicate_sa_and_conflicting_host_rejected(self):
        a = self.egress(outCluster=[{'host': 'remote.test', 'port': 443, 'protocol': 'HTTPS'}])
        b = copy.deepcopy(a)
        b['metadata']['name'] = 'second'
        with self.assertRaises(c.Invalid):
            self.new_plan([a, b])
        b['spec']['serviceAccount'] = 'other'
        b['spec']['outCluster'][0]['protocol'] = 'MTLS'
        with self.assertRaises(c.Invalid):
            self.new_plan([a, b])

    def test_developer_gateway_selector_and_naming_policy(self):
        spec = {'service': 'backend', 'port': 80,
                'host': 'orders-agent-mesh.test', 'gatewaySelector': {'app': 'ingress'},
                'allow': ['remote-mesh/ns/team/sa/caller']}
        a = self.api_cr(e.EXPOSE, 'orders', spec)
        self.config['exposurePolicy'] = {'dnsSuffix': 'test', 'labelSuffix': '-agent-mesh'}
        objects, _, _ = self.new_plan([a])
        gw = objects['Gateway', c.name('expose', spec['host'])]
        self.assertEqual(gw['spec']['selector'], {'app': 'ingress', c.GW_LABEL: c.digest('team')})
        a['spec']['gatewaySelector'] = {'app': 'backend'}
        with self.assertRaisesRegex(c.Invalid, 'exactly'):
            self.new_plan([a])

    def test_new_api_status_and_revocation_reconcile(self):
        api = self.fake([])
        a = self.egress(inCluster=[{'host': 'backend', 'port': 80, 'protocol': 'HTTP'}])
        api.save(a)
        c.reconcile(api, 'mesh-access-config')
        self.assertTrue(c.reconcile(api, 'mesh-access-config'))
        condition = api.data[e.EGRESS, 'caller']['status']['conditions'][0]
        self.assertEqual(condition['status'], 'True')
        api.data[e.EGRESS, 'caller']['spec']['inCluster'] = []
        self.assertTrue(c.reconcile(api, 'mesh-access-config'))
        ef = api.data['EnvoyFilter', c.name('egress-guard', 'caller')]
        self.assertEqual(ef['spec']['configPatches'][0]['patch']['value']['typed_config']['rules']['policies'], {})

    def test_plain_egress_does_not_require_trusted_bundle(self):
        api = self.fake([])
        del api.data['AgentMeshTrustedBundle', 'mesh-access-trust']
        api.save(self.egress())
        c.reconcile(api, 'mesh-access-config')
        self.assertTrue(c.reconcile(api, 'mesh-access-config'))

    def test_expose_selects_mixed_service_accounts_without_enrolling_backends(self):
        self.pods.append(fixtures.pod('second-backend', 'other', {'app': 'backend'}))
        unrelated = fixtures.pod('unrelated', 'backend', {'app': 'unrelated'})
        unrelated['spec']['containers'] = [{'name': 'app'}]
        self.pods.append(unrelated)
        objects, labels, _ = self.new_plan([self.expose])
        alias = next(d for d in objects.values() if d['kind'] == 'Service')
        self.assertEqual(alias['spec']['selector'], {'app': 'backend'})
        self.assertEqual(set(labels), {'gateway'})
        api = self.fake([self.expose])
        self.assertTrue(c.reconcile(api, 'mesh-access-config'))
        self.assertFalse(any(w[1] == 'Deployment' for w in api.writes))

    def test_expose_serviceaccount_rejected(self):
        self.expose['spec']['serviceAccount'] = 'backend'
        with self.assertRaisesRegex(c.Invalid, 'not supported'):
            self.new_plan([self.expose])

    def test_missing_bundle_preserves_last_good_and_reports_failure(self):
        api = self.fake([self.request])
        self.assertTrue(c.reconcile(api, 'mesh-access-config'))
        key = ('EnvoyFilter', c.name('requester', 'caller'))
        before = copy.deepcopy(api.data[key])
        del api.data['AgentMeshTrustedBundle', 'mesh-access-trust']
        self.assertFalse(c.reconcile(api, 'mesh-access-config'))
        self.assertEqual(api.data[key], before)
        self.assertIn('does not exist', api.data[e.EGRESS, 'caller']['status']['conditions'][0]['message'])

    def test_invalid_bundle_and_configured_reference(self):
        self.config['trustBundle'] = {'name': 'partners'}
        api = self.fake([self.request])
        bundle = api.data.pop(('AgentMeshTrustedBundle', 'mesh-access-trust'))
        bundle['metadata']['name'] = 'partners'
        api.save(bundle)
        self.assertTrue(c.reconcile(api, 'mesh-access-config'))
        api.data['AgentMeshTrustedBundle', 'partners']['spec']['caBundle'] = 'invalid'
        self.assertFalse(c.reconcile(api, 'mesh-access-config'))
        self.assertEqual(api.data['AgentMeshTrustedBundle', 'partners']['status']['conditions'][0]['status'], 'False')

    def test_only_three_custom_apis_and_expose_schema_has_no_sa(self):
        import yaml
        docs = list(yaml.safe_load_all((fixtures.HERE / 'crd.yaml').read_text()))
        self.assertEqual({d['spec']['names']['kind'] for d in docs},
                         {e.EGRESS, e.EXPOSE, 'AgentMeshTrustedBundle'})
        self.assertEqual({k for k, v in c.KINDS.items() if v[0] == c.VERSION},
                         {e.EGRESS, e.EXPOSE, 'AgentMeshTrustedBundle'})
        expose = next(d for d in docs if d['spec']['names']['kind'] == e.EXPOSE)
        spec = expose['spec']['versions'][0]['schema']['openAPIV3Schema']['properties']['spec']
        self.assertNotIn('serviceAccount', spec['properties'])
        self.assertNotIn('serviceAccount', spec['required'])


if __name__ == '__main__':
    unittest.main()
