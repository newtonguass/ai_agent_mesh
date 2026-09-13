package main

import (
	"bytes"
	"crypto/sha256"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"net"
	"net/url"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

const group = "agentmesh.io"
const version = group + "/v1alpha1"
const netAPI = "networking.istio.io/v1alpha3"
const metadataGroup = "mesh-access.example.com" // Preserve ownership and bootstrap labels across upgrades.
const label = metadataGroup + "/service-account"
const gatewayLabel = metadataGroup + "/gateway"
const stamp = metadataGroup + "/bootstrap-labels"
const managed = metadataGroup + "/managed-by"
const stateLabel = metadataGroup + "/namespace-state"
const manager = "mesh-access-controller"
const subset = "mesh-access-mtls"

type object = map[string]any

func obj(v any) object {
	if x, ok := v.(map[string]any); ok {
		return x
	}
	return object{}
}
func at(v any, keys ...string) any {
	for _, k := range keys {
		v = obj(v)[k]
	}
	return v
}
func str(v any) string                    { s, _ := v.(string); return s }
func arr(v any) []any                     { a, _ := v.([]any); return a }
func textAt(v any, keys ...string) string { return str(at(v, keys...)) }
func integer(v any) int {
	switch x := v.(type) {
	case int:
		return x
	case float64:
		if x == float64(int(x)) {
			return int(x)
		}
	}
	return 0
}
func stringsOf(v any) []string {
	r := []string{}
	for _, x := range arr(v) {
		r = append(r, str(x))
	}
	return r
}
func has(values []string, s string) bool {
	for _, v := range values {
		if s == v {
			return true
		}
	}
	return false
}
func unique(values []string) []string {
	m := map[string]bool{}
	for _, s := range values {
		m[s] = true
	}
	return keys(m)
}
func keys[T any](m map[string]T) []string {
	r := make([]string, 0, len(m))
	for k := range m {
		r = append(r, k)
	}
	sort.Strings(r)
	return r
}
func encode(v any) []byte                  { b, _ := json.Marshal(v); return b }
func clone(v object) object                { r := object{}; _ = json.Unmarshal(encode(v), &r); return r }
func same(a, b any) bool                   { return bytes.Equal(encode(a), encode(b)) }
func digest(s string) string               { return fmt.Sprintf("%x", sha256.Sum256([]byte(s)))[:16] }
func resourceName(prefix, s string) string { return "ma-" + prefix + "-" + digest(s) }
func saLabel(sa string) string             { return "sa-" + digest(sa) }
func selected(p object, s object) bool {
	for k, v := range s {
		if at(p, "metadata", "labels", k) != v {
			return false
		}
	}
	return true
}
func containers(p object) []any {
	r := append([]any{}, arr(at(p, "spec", "containers"))...)
	for _, c := range arr(at(p, "spec", "initContainers")) {
		if textAt(c, "restartPolicy") == "Always" {
			r = append(r, c)
		}
	}
	return r
}
func hasProxy(p object) bool {
	for _, c := range containers(p) {
		if textAt(c, "name") == "istio-proxy" {
			return true
		}
	}
	return false
}
func serviceAccount(p object) string {
	if s := textAt(p, "spec", "serviceAccountName"); s != "" {
		return s
	}
	return "default"
}

var dnsLabel = regexp.MustCompile(`^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$`)

func dns(s string) error {
	if len(s) == 0 || len(s) > 253 {
		return fmt.Errorf("invalid DNS name %q", s)
	}
	for _, p := range strings.Split(s, ".") {
		if !dnsLabel.MatchString(p) {
			return fmt.Errorf("invalid DNS name %q", s)
		}
	}
	return nil
}
func fqdn(s string) error {
	if e := dns(s); e != nil {
		return e
	}
	if !strings.Contains(s, ".") || net.ParseIP(s) != nil {
		return fmt.Errorf("requires fully qualified DNS name: %s", s)
	}
	return nil
}
func endpoint(s, host string, port int) (string, int, error) {
	if s == "" {
		return host, port, nil
	}
	u, e := url.Parse("//" + s)
	if e != nil || u.User != nil || u.Path != "" || u.RawQuery != "" || u.Fragment != "" {
		return "", 0, fmt.Errorf("endpoint must be DNS-name:port or IPv4:port")
	}
	p, e := strconv.Atoi(u.Port())
	if e != nil || p < 1 || p > 65535 {
		return "", 0, fmt.Errorf("invalid endpoint port")
	}
	if e = dns(u.Hostname()); e != nil {
		return "", 0, e
	}
	return u.Hostname(), p, nil
}
func publicBundle(s string) error {
	if len(s) == 0 || len(s) > 256*1024 {
		return fmt.Errorf("trust bundle must contain 1..262144 bytes")
	}
	rest := []byte(strings.TrimSpace(s))
	count := 0
	for len(rest) > 0 {
		if !bytes.HasPrefix(rest, []byte("-----BEGIN CERTIFICATE-----")) {
			return fmt.Errorf("trust bundle must contain only PEM certificates")
		}
		b, tail := pem.Decode(rest)
		if b == nil || b.Type != "CERTIFICATE" || len(b.Headers) != 0 {
			return fmt.Errorf("invalid PEM certificate")
		}
		if _, e := x509.ParseCertificate(b.Bytes); e != nil {
			return fmt.Errorf("invalid X.509 certificate: %w", e)
		}
		count++
		rest = bytes.TrimSpace(tail)
	}
	if count == 0 {
		return fmt.Errorf("empty trust bundle")
	}
	return nil
}
func strictJSON(data string, out any) error {
	d := json.NewDecoder(strings.NewReader(data))
	d.DisallowUnknownFields()
	if e := d.Decode(out); e != nil {
		return e
	}
	var trailing any
	if d.Decode(&trailing) != nil { // Require EOF, not merely a malformed second value.
		rest := strings.TrimSpace(data)
		if !json.Valid([]byte(rest)) {
			return fmt.Errorf("invalid JSON")
		}
		return nil
	}
	return fmt.Errorf("multiple JSON values")
}

type Config struct {
	SchemaVersion       int      `json:"schemaVersion"`
	ForbiddenNamespaces []string `json:"forbiddenNamespaces,omitempty"`
	TrustBundle         struct {
		Name string `json:"name"`
	} `json:"trustBundle"`
	Gateway struct {
		Service string `json:"service"`
		Port    int    `json:"port"`
	} `json:"gateway"`
	ExposurePolicy struct {
		DNSSuffix   string `json:"dnsSuffix"`
		LabelSuffix string `json:"labelSuffix"`
	} `json:"exposurePolicy,omitempty"`
}

func readConfig(s string) (Config, error) {
	c := Config{}
	e := strictJSON(s, &c)
	if e != nil {
		return c, e
	}
	if c.SchemaVersion != 1 {
		return c, fmt.Errorf("config.json schemaVersion must be 1")
	}
	if c.TrustBundle.Name == "" {
		c.TrustBundle.Name = "mesh-access-trust"
	}
	if c.Gateway.Port == 0 {
		c.Gateway.Port = 443
	}
	if c.ForbiddenNamespaces == nil {
		c.ForbiddenNamespaces = []string{"istio-system", "kube-system", "kube-public", "kube-node-lease"}
	}
	return c, dns(c.TrustBundle.Name)
}

type Destination struct {
	Host       string   `json:"host"`
	Port       int      `json:"port"`
	Protocol   string   `json:"protocol"`
	ServerName string   `json:"serverName,omitempty"`
	Endpoint   string   `json:"endpoint,omitempty"`
	Addresses  []string `json:"addresses,omitempty"`
}
type EgressSpec struct {
	ServiceAccount string        `json:"serviceAccount"`
	InCluster      []Destination `json:"inCluster,omitempty"`
	OutCluster     []Destination `json:"outCluster,omitempty"`
}
type ExposeSpec struct {
	Service         string            `json:"service"`
	Port            int               `json:"port"`
	Host            string            `json:"host"`
	GatewaySelector map[string]string `json:"gatewaySelector"`
	GatewayService  string            `json:"gatewayService,omitempty"`
	GatewayPort     int               `json:"gatewayPort,omitempty"`
}
type destination struct {
	Destination
	Internal     bool
	Server       string
	Aliases      []string
	IPs          []string
	Address      string
	EndpointPort int
}
type resolver func(string) ([]string, error)

func parseIPs(values []string) ([]string, error) {
	r := []string{}
	for _, s := range values {
		p := net.ParseIP(s)
		if p == nil {
			return nil, fmt.Errorf("addresses requires individual IPs, not CIDRs: %s", s)
		}
		r = append(r, p.String())
	}
	return unique(r), nil
}
func normalizeDestination(d Destination, internal bool, ns string, services map[string]object, resolve resolver) (destination, error) {
	r := destination{Destination: d, Internal: internal}
	r.Protocol = strings.ToUpper(d.Protocol)
	if !has([]string{"HTTP", "HTTPS", "TCP", "MTLS"}, r.Protocol) || d.Port < 1 || d.Port > 65535 {
		return r, fmt.Errorf("protocol must be HTTP, HTTPS, TCP or MTLS and port 1..65535")
	}
	if len(d.Addresses) > 64 {
		return r, fmt.Errorf("maximum 64 addresses")
	}
	if internal {
		if !strings.Contains(r.Host, ".") {
			r.Host += "." + ns + ".svc.cluster.local"
		}
		if !strings.HasSuffix(r.Host, ".svc.cluster.local") || len(strings.Split(r.Host, ".")) != 5 {
			return r, fmt.Errorf("inCluster host must be a short Service name or service.namespace.svc.cluster.local")
		}
		if d.Endpoint != "" {
			return r, fmt.Errorf("internal Services cannot override endpoint")
		}
	} else if strings.HasSuffix(r.Host, ".svc.cluster.local") {
		return r, fmt.Errorf("use inCluster for Kubernetes Services")
	}
	if e := fqdn(r.Host); e != nil {
		return r, e
	}
	if r.Protocol != "MTLS" && d.ServerName != "" {
		return r, fmt.Errorf("serverName is only for MTLS")
	}
	r.Server = d.ServerName
	if r.Server == "" {
		r.Server = r.Host
	}
	if e := fqdn(r.Server); e != nil {
		return r, e
	}
	if r.Protocol != "TCP" && len(d.Addresses) > 0 {
		return r, fmt.Errorf("explicit addresses are only for TCP")
	}
	var e error
	r.IPs, e = parseIPs(d.Addresses)
	if e != nil {
		return r, e
	}
	r.Aliases = []string{r.Host}
	if internal {
		parts := strings.Split(r.Host, ".")
		if parts[1] == ns {
			svc, ok := services[parts[0]]
			if !ok {
				return r, fmt.Errorf("internal Service does not exist: %s", r.Host)
			}
			ip := textAt(svc, "spec", "clusterIP")
			if ip == "" || ip == "None" || textAt(svc, "spec", "type") == "ExternalName" {
				return r, fmt.Errorf("internal whitelist requires a ClusterIP Service")
			}
			found := false
			for _, p := range arr(at(svc, "spec", "ports")) {
				if integer(at(p, "port")) == d.Port && (textAt(p, "protocol") == "" || textAt(p, "protocol") == "TCP") {
					found = true
					if r.Protocol == "MTLS" {
						n, ap := textAt(p, "name"), textAt(p, "appProtocol")
						if !(n == "http" || n == "http2" || strings.HasPrefix(n, "http-") || strings.HasPrefix(n, "http2-") || has([]string{"http", "http2", "kubernetes.io/h2c"}, ap)) {
							return r, fmt.Errorf("local gateway MTLS needs an HTTP-named Service port; use an owner-managed HTTP Service alias")
						}
					}
				}
			}
			if !found {
				return r, fmt.Errorf("internal TCP Service port not found")
			}
			values := stringsOf(at(svc, "spec", "clusterIPs"))
			if len(values) == 0 {
				values = []string{ip}
			}
			r.IPs, e = parseIPs(values)
			r.Aliases = append(r.Aliases, parts[0], parts[0]+"."+ns, parts[0]+"."+ns+".svc")
		} else {
			var values []string
			values, e = resolve(r.Host)
			if e == nil {
				r.IPs, e = parseIPs(values)
			}
			r.Aliases = append(r.Aliases, parts[0]+"."+parts[1], parts[0]+"."+parts[1]+".svc")
		}
		if e != nil || len(r.IPs) == 0 {
			return r, fmt.Errorf("cannot resolve internal Service %s: %v", r.Host, e)
		}
	}
	if r.Protocol == "TCP" {
		if len(r.IPs) == 0 || d.Endpoint != "" {
			return r, fmt.Errorf("TCP requires addresses and cannot override endpoint")
		}
	}
	r.Address, r.EndpointPort, e = endpoint(d.Endpoint, r.Host, r.Port)
	r.Aliases = unique(r.Aliases)
	return r, e
}
