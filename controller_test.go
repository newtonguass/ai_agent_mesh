package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
)

func TestClusterNamespaceIsolationAndSharedTrust(t *testing.T) {
	f := newFake(nil)
	f.ns = "operator"
	reference := inputsFor(t, "shared")
	for _, ns := range []string{"operator", "alpha", "beta"} {
		f.save(object{"kind": "Namespace", "metadata": object{"name": ns}})
	}
	config, _ := reference.Get("ConfigMap", "mesh-access-config")
	obj(config["metadata"])["namespace"] = "operator"
	f.save(config)
	for _, ns := range []string{"alpha", "beta"} {
		for _, o := range reference.store.objects {
			if textAt(o, "kind") == "ConfigMap" {
				continue
			}
			o = clone(o)
			if !kinds[textAt(o, "kind")].cluster {
				obj(o["metadata"])["namespace"] = ns
			}
			f.save(o)
		}
	}
	if !reconcileCluster(f, "mesh-access-config") {
		t.Fatal("initial cluster reconcile")
	}
	alpha := f.Scoped("alpha").(*fakeAPI)
	beta := f.Scoped("beta").(*fakeAPI)
	old, _ := alpha.Get("EnvoyFilter", resourceName("requester", "caller"))
	obj(f.store.objects[alpha.key("AgentMeshEgress", "caller")]["spec"])["serviceAccount"] = "missing"
	delete(f.store.objects, beta.key("AgentMeshEgress", "caller"))
	delete(f.store.objects, beta.key("AgentMeshEgress", "other"))
	if reconcileCluster(f, "mesh-access-config") {
		t.Fatal("invalid alpha must be reported")
	}
	current, _ := alpha.Get("EnvoyFilter", resourceName("requester", "caller"))
	if !same(old, current) {
		t.Fatal("invalid namespace lost last good plan")
	}
	if _, e := beta.Get("EnvoyFilter", resourceName("requester", "caller")); !codeIs(e, 404) {
		t.Fatal("other namespace deletion was blocked")
	}
	bundle, _ := f.Get("AgentMeshTrustedBundle", "mesh-access-trust")
	if textAt(arr(at(bundle, "status", "conditions"))[0], "status") != "True" {
		t.Fatal("namespace error corrupted global bundle status")
	}
}

func TestRESTPaginationAndTokenRotation(t *testing.T) {
	token := t.TempDir() + "/token"
	_ = os.WriteFile(token, []byte("first"), 0600)
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.URL.Path != "/api/v1/namespaces/team/pods" {
			t.Error(r.URL.Path)
		}
		expected := "first"
		if calls == 2 {
			expected = "second"
			if r.URL.Query().Get("continue") != "next" {
				t.Error("missing pagination token")
			}
		}
		if r.Header.Get("Authorization") != "Bearer "+expected {
			t.Error("projected token was cached")
		}
		next := ""
		if calls == 1 {
			next = "next"
			_ = os.WriteFile(token, []byte("second"), 0600)
		}
		_ = json.NewEncoder(w).Encode(object{"metadata": object{"continue": next}, "items": []any{object{"metadata": object{"name": fmt.Sprint(calls)}}}})
	}))
	defer server.Close()
	k := &Kube{ns: "team", base: server.URL, token: token, client: server.Client(), ctx: context.Background()}
	pods, e := k.List("Pod", "")
	if e != nil || len(pods) != 2 || textAt(pods[0], "kind") != "Pod" {
		t.Fatal(pods, e)
	}
	if k.path("AgentMeshTrustedBundle", "shared") != "/apis/agentmesh.io/v1alpha1/agentmeshtrustedbundles/shared" {
		t.Fatal("cluster resource incorrectly namespaced")
	}
}

type fakeStore struct {
	objects map[string]object
	writes  []string
	reads   []string
}
type fakeAPI struct {
	ns            string
	store         *fakeStore
	namespaceOnly bool
}

func newFake(inputs []object) *fakeAPI {
	f := &fakeAPI{"team", &fakeStore{objects: map[string]object{}}, false}
	for _, o := range inputs {
		f.save(o)
	}
	return f
}
func (f *fakeAPI) Namespace() string    { return f.ns }
func (f *fakeAPI) Scoped(ns string) API { r := *f; r.ns = ns; return &r }
func (f *fakeAPI) key(kind, name string) string {
	ns := f.ns
	if kinds[kind].cluster {
		ns = ""
	}
	return ns + "/" + kind + "/" + name
}
func (f *fakeAPI) save(o object) {
	o = clone(o)
	meta := obj(o["metadata"])
	if meta["uid"] == nil {
		meta["uid"] = "uid-" + str(meta["name"])
	}
	if meta["resourceVersion"] == nil {
		meta["resourceVersion"] = "1"
	}
	ns := str(meta["namespace"])
	if kinds[textAt(o, "kind")].cluster {
		ns = ""
	}
	f.store.objects[ns+"/"+objectKey(o)] = o
}
func (f *fakeAPI) Get(kind, name string) (object, error) {
	f.store.reads = append(f.store.reads, f.key(kind, name))
	o := f.store.objects[f.key(kind, name)]
	if o == nil {
		return nil, &APIError{404, "GET", f.key(kind, name)}
	}
	return clone(o), nil
}
func (f *fakeAPI) List(kind, selector string) ([]object, error) {
	if f.namespaceOnly && (f.ns != "team" || strings.HasPrefix(kind, "AgentMesh") || kinds[kind].cluster || kind == "ConfigMap") {
		return nil, fmt.Errorf("out-of-scope LIST")
	}
	out := []object{}
	for k, o := range f.store.objects {
		if textAt(o, "kind") != kind {
			continue
		}
		if !kinds[kind].cluster && f.ns != "" && !strings.HasPrefix(k, f.ns+"/") {
			continue
		}
		if selector != "" {
			kv := strings.SplitN(selector, "=", 2)
			if textAt(o, "metadata", "labels", kv[0]) != kv[1] {
				continue
			}
		}
		out = append(out, clone(o))
	}
	return out, nil
}
func (f *fakeAPI) Create(o object) error {
	f.store.writes = append(f.store.writes, "create "+objectKey(o))
	f.save(o)
	return nil
}
func merge(a, b object) {
	for k, v := range b {
		if v == nil {
			delete(a, k)
		} else if sub, ok := v.(map[string]any); ok {
			if a[k] == nil {
				a[k] = object{}
			}
			merge(obj(a[k]), sub)
		} else {
			a[k] = v
		}
	}
}
func (f *fakeAPI) Patch(kind, name string, body any, status, jp bool) error {
	if f.namespaceOnly && kind == "ConfigMap" && name != "mesh-access-state" {
		return fmt.Errorf("cannot patch input ConfigMap")
	}
	f.store.writes = append(f.store.writes, "patch "+kind+"/"+name)
	o := f.store.objects[f.key(kind, name)]
	if o == nil {
		return &APIError{404, "PATCH", name}
	}
	if jp {
		for _, v := range body.([]any) {
			op := obj(v)
			path := str(op["path"])
			if str(op["op"]) == "test" {
				if !same(at(o, "metadata", "resourceVersion"), op["value"]) {
					return fmt.Errorf("conflict")
				}
			} else {
				o[strings.TrimPrefix(path, "/")] = op["value"]
			}
		}
	} else {
		merge(o, clone(obj(body)))
	}
	f.save(o)
	return nil
}
func (f *fakeAPI) Delete(o object) error {
	f.store.writes = append(f.store.writes, "delete "+objectKey(o))
	delete(f.store.objects, f.key(textAt(o, "kind"), textAt(o, "metadata", "name")))
	return nil
}

type parityCase struct {
	Name     string
	Inputs   []object
	Expected []object
}

func fixtures(t *testing.T) []parityCase {
	t.Helper()
	b, e := os.ReadFile("testdata/python-parity.json")
	if e != nil {
		t.Fatal(e)
	}
	var out []parityCase
	if e = json.Unmarshal(b, &out); e != nil {
		t.Fatal(e)
	}
	return out
}
func inputsFor(t *testing.T, name string) *fakeAPI {
	t.Helper()
	for _, c := range fixtures(t) {
		if c.Name == name {
			return newFake(c.Inputs)
		}
	}
	t.Fatal(name)
	return nil
}
func runPlan(t *testing.T, f *fakeAPI) (*Plan, error) {
	t.Helper()
	cm, _ := f.Get("ConfigMap", "mesh-access-config")
	cfg, e := readConfig(textAt(cm, "data", "config.json"))
	if e != nil {
		return nil, e
	}
	bundle, _ := f.Get("AgentMeshTrustedBundle", "mesh-access-trust")
	as := []object{}
	for _, kind := range []string{"AgentMeshEgress", "AgentMeshExpose"} {
		list, _ := f.List(kind, "")
		as = append(as, list...)
	}
	svcs := map[string]object{}
	list, _ := f.List("Service", "")
	for _, s := range list {
		svcs[textAt(s, "metadata", "name")] = s
	}
	pods, _ := f.List("Pod", "")
	accounts := map[string]bool{}
	list, _ = f.List("ServiceAccount", "")
	for _, s := range list {
		accounts[textAt(s, "metadata", "name")] = true
	}
	g, _ := f.List("Gateway", "")
	r, _ := f.List("VirtualService", "")
	return buildPlan(f.ns, cfg, obj(cm["metadata"]), textAt(bundle, "spec", "caBundle"), as, svcs, pods, accounts, g, r, func(string) ([]string, error) { return []string{"10.2.3.4"}, nil })
}
func TestPythonTrafficPlanParity(t *testing.T) {
	for _, c := range fixtures(t) {
		t.Run(c.Name, func(t *testing.T) {
			f := newFake(c.Inputs)
			p, e := runPlan(t, f)
			if e != nil {
				t.Fatal(e)
			}
			expected := map[string]object{}
			for _, o := range c.Expected {
				expected[objectKey(o)] = o
			}
			if !same(p.Objects, expected) {
				for k, o := range expected {
					if !same(o, p.Objects[k]) {
						t.Errorf("%s differs\nwant %s\ngot %s", k, encode(o), encode(p.Objects[k]))
					}
				}
				t.Fatalf("object count: want %d got %d", len(expected), len(p.Objects))
			}
		})
	}
}
func TestInvalidDeclarations(t *testing.T) {
	for _, tc := range []struct {
		name   string
		change func(*fakeAPI)
	}{
		{"missing SA", func(f *fakeAPI) {
			f.store.objects[f.key("AgentMeshEgress", "caller")]["spec"].(map[string]any)["serviceAccount"] = "missing"
		}},
		{"unknown field", func(f *fakeAPI) { obj(f.store.objects[f.key("AgentMeshEgress", "caller")]["spec"])["typo"] = true }},
		{"duplicate SA", func(f *fakeAPI) {
			a, _ := f.Get("AgentMeshEgress", "caller")
			obj(a["metadata"])["name"] = "duplicate"
			f.save(a)
		}},
		{"non-injected caller", func(f *fakeAPI) { obj(f.store.objects[f.key("Pod", "caller")]["spec"])["containers"] = []any{} }},
		{"invalid protocol", func(f *fakeAPI) {
			obj(arr(at(f.store.objects[f.key("AgentMeshEgress", "caller")], "spec", "outCluster"))[0])["protocol"] = "UDP"
		}},
		{"unbounded TCP", func(f *fakeAPI) {
			d := obj(arr(at(f.store.objects[f.key("AgentMeshEgress", "caller")], "spec", "outCluster"))[0])
			d["protocol"] = "TCP"
			delete(d, "addresses")
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := inputsFor(t, "mixed")
			tc.change(f)
			if _, e := runPlan(t, f); e == nil {
				t.Fatal("accepted invalid declaration")
			}
			if len(f.store.writes) != 0 {
				t.Fatal("render mutated API")
			}
		})
	}
}
func TestGatewayOwnerBoundary(t *testing.T) {
	for _, mode := range []string{"MUTUAL", "SIMPLE", "PASSTHROUGH", "missing-route", "auth-field", "named-port-missing", "native"} {
		t.Run(mode, func(t *testing.T) {
			f := inputsFor(t, "expose")
			g := f.store.objects[f.key("Gateway", "existing-ingress")]
			tls := obj(at(arr(at(g, "spec", "servers"))[0], "tls"))
			tls["subjectAltNames"] = []any{"spiffe://remote/ns/team/sa/caller"}
			tls["verifyCertificateSpki"] = []any{"pin"}
			if has([]string{"SIMPLE", "PASSTHROUGH"}, mode) {
				tls["mode"] = mode
			}
			if mode == "missing-route" {
				delete(f.store.objects, f.key("VirtualService", "existing-ingress"))
			}
			if mode == "auth-field" {
				obj(f.store.objects[f.key("AgentMeshExpose", "backend")]["spec"])["allow"] = []any{"anything"}
			}
			if mode == "named-port-missing" {
				obj(arr(at(f.store.objects[f.key("Pod", "gateway")], "spec", "containers"))[0])["ports"] = []any{}
			}
			if mode == "native" {
				spec := obj(f.store.objects[f.key("Pod", "gateway")]["spec"])
				proxy := obj(arr(spec["containers"])[0])
				proxy["restartPolicy"] = "Always"
				spec["initContainers"], spec["containers"] = []any{proxy}, []any{}
			}
			before := string(encode(f.store.objects))
			p, e := runPlan(t, f)
			if mode != "MUTUAL" && mode != "native" {
				if e == nil {
					t.Fatal("invalid owner setting accepted")
				}
				return
			}
			if e != nil {
				t.Fatal(e)
			}
			if len(p.Objects) != 1 {
				t.Fatal("Expose must generate one EnvoyFilter only")
			}
			if before != string(encode(f.store.objects)) {
				t.Fatal("owner objects changed")
			}
			for _, o := range p.Objects {
				patch := arr(at(o, "spec", "configPatches"))[0]
				if integer(at(patch, "match", "listener", "portNumber")) != 8443 || textAt(patch, "match", "listener", "filterChain", "sni") != "exposed.test" {
					t.Fatal("wrong listener match")
				}
				v := obj(at(patch, "patch", "value", "transport_socket", "typed_config", "common_tls_context", "validation_context"))
				if len(arr(v["match_subject_alt_names"])) != 1 || len(arr(v["verify_certificate_spki"])) != 1 {
					t.Fatal("owner validation restrictions dropped")
				}
				if at(patch, "patch", "value", "transport_socket", "typed_config", "require_client_certificate") != nil {
					t.Fatal("controller owns TLS enforcement")
				}
			}
		})
	}
}
func TestPublicBundleAndStrictInput(t *testing.T) {
	for _, s := range []string{"", "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----", strings.Repeat("x", 262145), "-----BEGIN CERTIFICATE-----\neA==\n-----END CERTIFICATE-----"} {
		if publicBundle(s) == nil {
			t.Fatal("accepted invalid PEM")
		}
	}
	for _, s := range []string{`{"schemaVersion":1} {}`, `{"schemaVersion":1} garbage`, `{"schemaVersion":1,"typo":true}`} {
		if _, e := readConfig(s); e == nil {
			t.Fatal("accepted invalid config")
		}
	}
}
func setupCM(t *testing.T) *fakeAPI {
	f := inputsFor(t, "mixed")
	input, _ := f.Get("AgentMeshEgress", "caller")
	spec := clone(obj(input["spec"]))
	spec["name"] = "caller"
	bundle, _ := f.Get("AgentMeshTrustedBundle", "mesh-access-trust")
	for _, name := range []string{"mesh-access-declarations", "mesh-access-state", "mesh-access-trust"} {
		data := object{}
		switch name {
		case "mesh-access-declarations":
			data["declarations.json"] = string(encode(object{"schemaVersion": 1, "egress": []any{spec}, "expose": []any{}}))
		case "mesh-access-trust":
			data["ca.crt"] = at(bundle, "spec", "caBundle")
		}
		f.save(object{"kind": "ConfigMap", "apiVersion": "v1", "metadata": object{"name": name, "namespace": "team"}, "data": data})
	}
	f.namespaceOnly = true
	return f
}
func TestConfigMapScopeDriftRevocationAndInvalidRetention(t *testing.T) {
	f := setupCM(t)
	reconcile := func() bool {
		return reconcileConfigMaps(f, "mesh-access-config", "mesh-access-declarations", "mesh-access-state")
	}
	if !reconcile() {
		t.Fatal("initial reconcile")
	}
	for _, w := range f.store.writes {
		if strings.Contains(w, "Authorization") || strings.Contains(w, "Gateway") || strings.Contains(w, " Service/") {
			t.Fatal(w)
		}
	}
	n := resourceName("remote", "remote.test")
	dr := f.store.objects[f.key("DestinationRule", n)]
	obj(at(arr(at(dr, "spec", "subsets"))[0], "trafficPolicy", "tls"))["mode"] = "DISABLE"
	if !reconcile() {
		t.Fatal("drift reconcile")
	}
	if !strings.Contains(string(encode(f.store.objects[f.key("DestinationRule", n)])), "ISTIO_MUTUAL") {
		t.Fatal("drift remains")
	}
	input := f.store.objects[f.key("ConfigMap", "mesh-access-declarations")]
	original := textAt(input, "data", "declarations.json")
	obj(input["data"])["declarations.json"] = `{"schemaVersion":1,"egress":[{"name":"caller","serviceAccount":"missing"}]}`
	if reconcile() {
		t.Fatal("invalid config accepted")
	}
	if f.store.objects[f.key("DestinationRule", n)] == nil {
		t.Fatal("invalid input deleted last good plan")
	}
	obj(input["data"])["declarations.json"] = original
	if !reconcile() {
		t.Fatal("restore")
	}
	obj(input["data"])["declarations.json"] = `{"schemaVersion":1,"egress":[],"expose":[]}`
	if !reconcile() {
		t.Fatal("prune")
	}
	if f.store.objects[f.key("DestinationRule", n)] != nil {
		t.Fatal("revoked destination remains")
	}
	for _, w := range f.store.writes {
		if strings.HasPrefix(w, "patch ConfigMap/") && w != "patch ConfigMap/mesh-access-state" {
			t.Fatal("modified administrator/developer input:", w)
		}
	}
}
func TestPlanPreflightOwnership(t *testing.T) {
	f := inputsFor(t, "mixed")
	cm, _ := f.Get("ConfigMap", "mesh-access-config")
	cfg, _ := readConfig(textAt(cm, "data", "config.json"))
	b, _ := f.Get("AgentMeshTrustedBundle", "mesh-access-trust")
	a, _ := f.Get("AgentMeshEgress", "caller")
	f.save(object{"kind": "DestinationRule", "metadata": object{"name": resourceName("remote", "remote.test"), "namespace": "team"}, "spec": object{"host": "other.test"}})
	_, _, e := executePlan(f, cfg, cm, Trust{map[string]string{"mesh-access-trust": textAt(b, "spec", "caBundle")}, nil}, []object{a})
	if e == nil || len(f.store.writes) != 0 {
		t.Fatal("ownership collision must reject before any write", e, f.store.writes)
	}
}
