package main

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"
)

type resource struct {
	api, plural string
	cluster     bool
}

var kinds = map[string]resource{
	"ConfigMap": {"v1", "configmaps", false}, "Service": {"v1", "services", false}, "Pod": {"v1", "pods", false}, "ServiceAccount": {"v1", "serviceaccounts", false}, "Namespace": {"v1", "namespaces", true},
	"AgentMeshEgress": {version, "agentmeshegresses", false}, "AgentMeshExpose": {version, "agentmeshexposes", false}, "AgentMeshTrustedBundle": {version, "agentmeshtrustedbundles", true},
	"Deployment": {"apps/v1", "deployments", false}, "StatefulSet": {"apps/v1", "statefulsets", false}, "DaemonSet": {"apps/v1", "daemonsets", false},
	"Sidecar": {netAPI, "sidecars", false}, "ServiceEntry": {netAPI, "serviceentries", false}, "DestinationRule": {netAPI, "destinationrules", false}, "VirtualService": {netAPI, "virtualservices", false}, "EnvoyFilter": {netAPI, "envoyfilters", false}, "Gateway": {netAPI, "gateways", false},
}
var order = []string{"Sidecar", "ServiceEntry", "DestinationRule", "EnvoyFilter", "VirtualService"}

type APIError struct {
	Code         int
	Method, Path string
}

func (e *APIError) Error() string {
	return fmt.Sprintf("Kubernetes HTTP %d: %s %s", e.Code, e.Method, e.Path)
}
func codeIs(err error, code int) bool { e, ok := err.(*APIError); return ok && e.Code == code }

type API interface {
	Namespace() string
	Scoped(string) API
	Get(string, string) (object, error)
	List(string, string) ([]object, error)
	Create(object) error
	Patch(string, string, any, bool, bool) error
	Delete(object) error
}
type Kube struct {
	ns, base, token string
	client          *http.Client
	ctx             context.Context
}

func newKube(ctx context.Context, ns, base string) (*Kube, error) {
	k := &Kube{ns: ns, base: base, ctx: ctx}
	if k.base == "" {
		k.base = "https://kubernetes.default.svc"
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	if strings.HasPrefix(k.base, "https://") {
		ca, e := os.ReadFile("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
		if e != nil {
			return nil, e
		}
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(ca) {
			return nil, fmt.Errorf("invalid Kubernetes API CA")
		}
		transport.TLSClientConfig = &tls.Config{RootCAs: pool, MinVersion: tls.VersionTLS12}
		k.token = "/var/run/secrets/kubernetes.io/serviceaccount/token"
	} else {
		u, e := url.Parse(k.base)
		if e != nil || u.Scheme != "http" || u.Hostname() != "127.0.0.1" || u.Port() == "" || u.User != nil || u.Path != "" || u.RawQuery != "" || u.Fragment != "" {
			return nil, fmt.Errorf("HTTP API allowed only for local kubectl proxy")
		}
	}
	k.client = &http.Client{Transport: transport, Timeout: 20 * time.Second, CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }}
	return k, nil
}
func (k *Kube) Namespace() string    { return k.ns }
func (k *Kube) Scoped(ns string) API { c := *k; c.ns = ns; return &c }
func (k *Kube) path(kind, name string) string {
	r := kinds[kind]
	prefix := "/apis/" + r.api
	if r.api == "v1" {
		prefix = "/api/v1"
	}
	if k.ns != "" && !r.cluster {
		prefix += "/namespaces/" + url.PathEscape(k.ns)
	}
	prefix += "/" + r.plural
	if name != "" {
		prefix += "/" + url.PathEscape(name)
	}
	return prefix
}
func (k *Kube) call(method, path string, body any, ct string) (object, error) {
	var b io.Reader
	if body != nil {
		b = bytes.NewReader(encode(body))
	}
	req, e := http.NewRequestWithContext(k.ctx, method, k.base+path, b)
	if e != nil {
		return nil, e
	}
	req.Header.Set("Accept", "application/json")
	req.Header.Set("Content-Type", ct)
	if k.token != "" {
		token, e := os.ReadFile(k.token)
		if e != nil {
			return nil, e
		}
		req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(string(token)))
	}
	res, e := k.client.Do(req)
	if e != nil {
		return nil, e
	}
	defer res.Body.Close()
	if res.StatusCode < 200 || res.StatusCode >= 300 {
		return nil, &APIError{res.StatusCode, method, path}
	}
	var out object
	if e = json.NewDecoder(res.Body).Decode(&out); e != nil {
		return nil, e
	}
	return out, nil
}
func (k *Kube) Get(kind, name string) (object, error) {
	return k.call("GET", k.path(kind, name), nil, "application/json")
}
func (k *Kube) List(kind, selector string) ([]object, error) {
	items := []object{}
	token := ""
	for {
		q := url.Values{"limit": {"500"}, "continue": {token}}
		if selector != "" {
			q.Set("labelSelector", selector)
		}
		res, e := k.call("GET", k.path(kind, "")+"?"+q.Encode(), nil, "application/json")
		if e != nil {
			return nil, e
		}
		for _, v := range arr(res["items"]) {
			o := obj(v)
			o["kind"], o["apiVersion"] = kind, kinds[kind].api
			items = append(items, o)
		}
		token = textAt(res, "metadata", "continue")
		if token == "" {
			return items, nil
		}
	}
}
func (k *Kube) Create(o object) error {
	_, e := k.call("POST", k.path(textAt(o, "kind"), ""), o, "application/json")
	return e
}
func (k *Kube) Patch(kind, name string, body any, status, jsonPatch bool) error {
	path := k.path(kind, name)
	if status {
		path += "/status"
	}
	ct := "application/merge-patch+json"
	if jsonPatch {
		ct = "application/json-patch+json"
	}
	_, e := k.call("PATCH", path, body, ct)
	return e
}
func (k *Kube) Delete(o object) error {
	_, e := k.call("DELETE", k.path(textAt(o, "kind"), textAt(o, "metadata", "name")), object{"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": object{"uid": at(o, "metadata", "uid"), "resourceVersion": at(o, "metadata", "resourceVersion")}}, "application/json")
	if codeIs(e, 404) {
		return nil
	}
	return e
}
