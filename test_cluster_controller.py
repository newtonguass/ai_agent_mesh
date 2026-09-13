"""Behavior of central trust, namespace isolation and last-declaration cleanup."""
import copy
import json
import ssl
import unittest

import controller as c
import test_controller as fixtures
from test_controller import FakeAPI


class ClusterAPI:
    def __init__(self, stores, namespace='operator'):
        self.stores, self.namespace = stores, namespace

    def in_namespace(self, namespace):
        return ClusterAPI(self.stores, namespace)

    def target(self, kind):
        return self.stores[None if kind in c.CLUSTER_KINDS else self.namespace]

    def get(self, kind, name):
        return self.target(kind).get(kind, name)

    def list(self, kind, selector=None):
        stores = [self.target(kind)] if self.namespace or kind in c.CLUSTER_KINDS else [
            store for ns, store in self.stores.items() if ns is not None]
        result = [item for store in stores for item in store.list(kind)]
        if selector:
            key, value = selector.split('=', 1)
            result = [r for r in result if r['metadata'].get('labels', {}).get(key) == value]
        return result

    def create(self, obj):
        self.target(obj['kind']).create(obj)

    def patch(self, kind, name, body, **kwargs):
        self.target(kind).patch(kind, name, body, **kwargs)

    def delete(self, obj):
        self.target(obj['kind']).delete(obj)


class ClusterControllerTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.ControllerTests()
        fixture.setUp()
        self.pem = fixture.pem
        self.stores = {None: FakeAPI([]), 'operator': FakeAPI([])}
        for ns in ['team-a', 'team-b']:
            store = fixture.fake([fixture.request])
            del store.data['ConfigMap', 'mesh-access-config']
            trust = store.data.pop(('AgentMeshTrustedBundle', 'mesh-access-trust'))
            self.stores[None].save(trust)
            for item in store.data.values():
                item['metadata']['namespace'] = ns
            self.stores[ns] = store
        self.stores['operator'].save({'kind': 'ConfigMap', 'metadata': {
            'name': 'mesh-access-config', 'namespace': 'operator'},
            'data': {'config.json': json.dumps(fixture.config)}})
        for ns in self.stores:
            if ns:
                self.stores[None].save({'kind': 'Namespace', 'metadata': {'name': ns}})
        self.api = ClusterAPI(self.stores)

    def reconcile(self):
        return c.reconcile_cluster(self.api, 'mesh-access-config')

    def trusted(self, ns):
        ef = self.stores[ns].get('EnvoyFilter', c.name('requester', 'caller'))
        return ef['spec']['configPatches'][0]['patch']['value']['transport_socket']['typed_config'][
            'common_tls_context']['validation_context']['trusted_ca']['inline_string']

    def test_shared_bundle_updates_both_namespaces_without_local_config(self):
        self.assertTrue(self.reconcile())
        for ns in ['team-a', 'team-b']:
            self.assertEqual(self.trusted(ns), self.pem)
            self.assertNotIn('config.json', self.stores[ns].get('ConfigMap', 'mesh-access-config')['data'])
        second = ssl.DER_cert_to_PEM_cert(ssl.create_default_context().get_ca_certs(binary_form=True)[1])
        self.stores[None].data['AgentMeshTrustedBundle', 'mesh-access-trust']['spec']['caBundle'] += second
        self.assertTrue(self.reconcile())
        for ns in ['team-a', 'team-b']:
            self.assertEqual(self.trusted(ns), self.pem + second)

    def test_namespace_error_does_not_block_other_namespace_revocation_or_bundle_status(self):
        self.assertTrue(self.reconcile())
        self.stores['team-a'].data['AgentMeshEgress', 'caller']['spec']['serviceAccount'] = 'missing'
        self.stores['team-b'].data['AgentMeshEgress', 'caller']['spec']['outCluster'] = []
        self.assertFalse(self.reconcile())
        self.assertNotIn(('EnvoyFilter', c.name('requester', 'caller')), self.stores['team-b'].data)
        trust = self.stores[None].get('AgentMeshTrustedBundle', 'mesh-access-trust')
        self.assertEqual(trust['status']['conditions'][0]['status'], 'True')
        self.assertEqual(self.trusted('team-a'), self.pem)

    def test_final_declaration_deletion_prunes_only_its_namespace(self):
        self.assertTrue(self.reconcile())
        del self.stores['team-a'].data['AgentMeshEgress', 'caller']
        self.assertTrue(self.reconcile())
        self.assertFalse(any(r['metadata'].get('labels', {}).get(c.MANAGED) == c.MANAGER
                             for r in self.stores['team-a'].data.values()))
        self.assertEqual(self.trusted('team-b'), self.pem)

    def test_namespace_config_cannot_override_admin_selected_trust(self):
        self.stores['team-a'].save({'kind': 'ConfigMap', 'metadata': {
            'name': 'mesh-access-config', 'namespace': 'team-a', 'uid': 'preserve-owner'},
            'data': {'config.json': json.dumps({'trustBundle': {'name': 'unapproved'}})}})
        self.assertTrue(self.reconcile())
        self.assertEqual(self.trusted('team-a'), self.pem)
        self.assertEqual(self.stores['team-a'].get('ConfigMap', 'mesh-access-config')['metadata']['uid'], 'preserve-owner')

    def test_operator_namespace_forbidden_and_terminating_namespace_skipped(self):
        declaration = copy.deepcopy(self.stores['team-a'].data['AgentMeshEgress', 'caller'])
        declaration['metadata']['namespace'] = 'operator'
        self.stores['operator'].save(declaration)
        self.stores[None].data['Namespace', 'team-a']['status'] = {'phase': 'Terminating'}
        self.assertFalse(self.reconcile())
        self.assertEqual(self.stores['team-a'].writes, [])
        self.assertTrue(all(w[1] == 'AgentMeshEgress' for w in self.stores['operator'].writes))
        self.assertEqual(self.trusted('team-b'), self.pem)

    def test_install_separates_developer_trust_admin_and_controller_permissions(self):
        import yaml
        docs = list(yaml.safe_load_all((fixtures.HERE / 'install.yaml').read_text()))
        roles = {d['metadata']['name']: d for d in docs if d['kind'] == 'ClusterRole'}
        def allowed(role, resource, verb):
            return any(resource in rule['resources'] and verb in rule['verbs'] for rule in roles[role]['rules'])
        for resource in ['agentmeshegresses', 'agentmeshexposes']:
            self.assertTrue(allowed('agentmesh-developer', resource, 'create'))
            self.assertFalse(allowed('agentmesh-trust-admin', resource, 'create'))
        for verb in ['create', 'update', 'patch', 'delete']:
            self.assertTrue(allowed('agentmesh-trust-admin', 'agentmeshtrustedbundles', verb))
            self.assertFalse(allowed('agentmesh-developer', 'agentmeshtrustedbundles', verb))
            self.assertFalse(allowed(c.MANAGER, 'agentmeshtrustedbundles', verb))
        self.assertTrue(allowed(c.MANAGER, 'agentmeshtrustedbundles/status', 'patch'))
        self.assertFalse(allowed(c.MANAGER, 'secrets', 'get'))
        bindings = [d for d in docs if d['kind'] == 'ClusterRoleBinding']
        self.assertEqual([d['roleRef']['name'] for d in bindings], [c.MANAGER])


if __name__ == '__main__':
    unittest.main()
