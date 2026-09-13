package main

import (
	"fmt"
	"net"
	"strings"
)

type Plan struct {
	Objects    map[string]object
	Requesters map[string][]destination
	Labels     map[string]object
	URLs       map[string][]string
}

func objectKey(o object) string { return textAt(o, "kind") + "/" + textAt(o, "metadata", "name") }
func (p *Plan) add(ns string, owner object, kind, n string, spec object) error {
	k := kind + "/" + n
	if p.Objects[k] != nil {
		return fmt.Errorf("generated resource collision: %s", k)
	}
	p.Objects[k] = object{"apiVersion": kinds[kind].api, "kind": kind, "metadata": object{"name": n, "namespace": ns, "labels": object{managed: manager}, "ownerReferences": []any{object{"apiVersion": "v1", "kind": "ConfigMap", "name": owner["name"], "uid": owner["uid"], "controller": true, "blockOwnerDeletion": false}}}, "spec": spec}
	return nil
}
func conjunction(rules ...any) object { return object{"and_rules": object{"rules": rules}} }
func policy(permissions []any) object {
	policies := object{}
	if len(permissions) > 0 {
		policies["agent-mesh-allow"] = object{"principals": []any{object{"any": true}}, "permissions": permissions}
	}
	return object{"action": "ALLOW", "policies": policies}
}
func ipRule(ip string) object {
	bits := 128
	if net.ParseIP(ip).To4() != nil {
		bits = 32
	}
	return object{"destination_ip": object{"address_prefix": ip, "prefix_len": bits}}
}
func tlsSocket(direction string, validation object) object {
	return object{"transport_socket": object{"name": "envoy.transport_sockets.tls", "typed_config": object{"@type": "type.googleapis.com/envoy.extensions.transport_sockets.tls.v3." + direction + "TlsContext", "common_tls_context": object{"validation_context": validation}}}}
}

// buildPlan is read-only. Reconciliation validates the entire namespace before writes.
func buildPlan(ns string, cfg Config, owner object, bundle string, declarations []object, services map[string]object, pods []object, accounts map[string]bool, gateways, routes []object, resolve resolver) (*Plan, error) {
	p := &Plan{map[string]object{}, map[string][]destination{}, map[string]object{}, map[string][]string{}}
	if has(cfg.ForbiddenNamespaces, ns) {
		return nil, fmt.Errorf("refusing reconciliation in forbidden namespace %s", ns)
	}
	exposures := map[string]ExposeSpec{}
	remote := map[string]destination{}
	remoteSAs := map[string][]string{}
	definitions := map[string]destination{}
	needTrust := false
	for _, a := range declarations {
		key := objectKey(a)
		switch textAt(a, "kind") {
		case "AgentMeshEgress":
			s := EgressSpec{}
			if e := strictJSON(string(encode(a["spec"])), &s); e != nil {
				return nil, e
			}
			if e := dns(s.ServiceAccount); e != nil {
				return nil, e
			}
			if !accounts[s.ServiceAccount] {
				return nil, fmt.Errorf("ServiceAccount does not exist: %s", s.ServiceAccount)
			}
			if _, ok := p.Requesters[s.ServiceAccount]; ok {
				return nil, fmt.Errorf("use one AgentMeshEgress per serviceAccount: %s", s.ServiceAccount)
			}
			if len(s.InCluster) > 64 || len(s.OutCluster) > 64 {
				return nil, fmt.Errorf("maximum 64 destinations in each list")
			}
			destinations := []destination{}
			seen := map[string]bool{}
			p.URLs[key] = []string{}
			for i, list := range [][]Destination{s.InCluster, s.OutCluster} {
				for _, d := range list {
					r, e := normalizeDestination(d, i == 0, ns, services, resolve)
					if e != nil {
						return nil, e
					}
					id := fmt.Sprintf("%s:%d", r.Host, r.Port)
					if seen[id] {
						return nil, fmt.Errorf("duplicate destination host/port: %s", id)
					}
					seen[id] = true
					destinations = append(destinations, r)
					scheme := strings.ToLower(r.Protocol)
					if r.Protocol == "MTLS" {
						scheme = "http"
						needTrust = true
						if old, ok := remote[r.Host]; ok && (old.Port != r.Port || old.Address != r.Address || old.EndpointPort != r.EndpointPort || old.Server != r.Server || old.Internal != r.Internal) {
							return nil, fmt.Errorf("conflicting requested host %s", r.Host)
						}
						remote[r.Host] = r
						remoteSAs[r.Host] = append(remoteSAs[r.Host], s.ServiceAccount)
					}
					p.URLs[key] = append(p.URLs[key], fmt.Sprintf("%s://%s:%d", scheme, r.Host, r.Port))
					if !r.Internal {
						if old, ok := definitions[r.Host]; ok && !same(old, r) {
							return nil, fmt.Errorf("conflicting external host definition: %s", r.Host)
						}
						definitions[r.Host] = r
					}
				}
			}
			p.Requesters[s.ServiceAccount] = destinations
			for _, pod := range pods {
				if serviceAccount(pod) == s.ServiceAccount {
					if !hasProxy(pod) {
						return nil, fmt.Errorf("pod lacks istio-proxy: %s", textAt(pod, "metadata", "name"))
					}
					p.Labels[textAt(pod, "metadata", "name")] = object{label: saLabel(s.ServiceAccount)}
				}
			}
		case "AgentMeshExpose":
			s := ExposeSpec{}
			if e := strictJSON(string(encode(a["spec"])), &s); e != nil {
				return nil, fmt.Errorf("AgentMeshExpose is trust-only; authorization and gateway settings belong to the owner: %w", e)
			}
			if e := fqdn(s.Host); e != nil {
				return nil, e
			}
			if e := dns(s.Service); e != nil {
				return nil, e
			}
			if s.Port < 1 || s.Port > 65535 || s.GatewayPort < 0 || s.GatewayPort > 65535 {
				return nil, fmt.Errorf("invalid exposure port")
			}
			if _, ok := exposures[s.Host]; ok {
				return nil, fmt.Errorf("duplicate exposed hostname: %s", s.Host)
			}
			if len(s.GatewaySelector) == 0 {
				return nil, fmt.Errorf("gatewaySelector requires nonempty labels")
			}
			for k, v := range s.GatewaySelector {
				if k == "" || v == "" {
					return nil, fmt.Errorf("gatewaySelector requires nonempty labels")
				}
			}
			if suffix := cfg.ExposurePolicy.DNSSuffix; suffix != "" {
				if e := dns(suffix); e != nil {
					return nil, e
				}
				if !strings.HasSuffix(s.Host, "."+suffix) {
					return nil, fmt.Errorf("exposed host outside configured dnsSuffix")
				}
			}
			if suffix := cfg.ExposurePolicy.LabelSuffix; suffix != "" && !strings.HasSuffix(strings.Split(s.Host, ".")[0], suffix) {
				return nil, fmt.Errorf("exposed first DNS label must end with %s", suffix)
			}
			if s.GatewayService == "" {
				s.GatewayService = cfg.Gateway.Service
			}
			if s.GatewayPort == 0 {
				s.GatewayPort = cfg.Gateway.Port
			}
			exposures[s.Host] = s
			needTrust = true
			p.URLs[key] = []string{}
		default:
			return nil, fmt.Errorf("unknown declaration kind %s", textAt(a, "kind"))
		}
	}
	if needTrust {
		if e := publicBundle(bundle); e != nil {
			return nil, e
		}
	}
	for host := range exposures {
		if _, ok := remote[host]; ok {
			return nil, fmt.Errorf("same-namespace gateway calls require native Service routing")
		}
	}
	patches := map[string][]any{}
	for _, host := range keys(remote) {
		d := remote[host]
		n := resourceName("remote", host)
		if !d.Internal {
			resolution := "DNS"
			if net.ParseIP(d.Address) != nil {
				resolution = "STATIC"
			}
			_ = p.add(ns, owner, "ServiceEntry", n, object{"hosts": []string{host}, "exportTo": []string{"."}, "location": "MESH_EXTERNAL", "resolution": resolution, "ports": []any{object{"number": d.Port, "name": "http-mtls", "protocol": "HTTP"}}, "endpoints": []any{object{"address": d.Address, "ports": object{"http-mtls": d.EndpointPort}}}})
		}
		_ = p.add(ns, owner, "DestinationRule", n, object{"host": host, "exportTo": []string{"."}, "subsets": []any{object{"name": subset, "trafficPolicy": object{"tls": object{"mode": "ISTIO_MUTUAL", "sni": d.Server, "subjectAltNames": []string{d.Server}}}}}})
		matches := []any{}
		for _, sa := range unique(remoteSAs[host]) {
			matches = append(matches, object{"sourceLabels": object{label: saLabel(sa)}, "port": d.Port})
			patches[sa] = append(patches[sa], object{"applyTo": "CLUSTER", "match": object{"context": "SIDECAR_OUTBOUND", "cluster": object{"service": host, "subset": subset}}, "patch": object{"operation": "MERGE", "value": tlsSocket("Upstream", object{"trusted_ca": object{"inline_string": bundle}, "match_subject_alt_names": []any{object{"exact": d.Server}}})}})
		}
		route := object{"match": matches, "route": []any{object{"destination": object{"host": host, "port": object{"number": d.Port}, "subset": subset}}}}
		if d.Server != host {
			route["rewrite"] = object{"authority": d.Server}
		}
		_ = p.add(ns, owner, "VirtualService", n, object{"hosts": []string{host}, "exportTo": []string{"."}, "gateways": []string{"mesh"}, "http": []any{route, object{"route": []any{object{"destination": object{"host": host, "port": object{"number": d.Port}}}}}}})
	}
	for _, sa := range keys(patches) {
		_ = p.add(ns, owner, "EnvoyFilter", resourceName("requester", sa), object{"workloadSelector": object{"labels": object{label: saLabel(sa)}}, "configPatches": patches[sa]})
	}
	for _, host := range keys(definitions) {
		d := definitions[host]
		if d.Protocol == "MTLS" {
			continue
		}
		proto := d.Protocol
		if proto == "HTTPS" {
			proto = "TLS"
		}
		portName := strings.ToLower(d.Protocol)
		spec := object{"hosts": []string{host}, "exportTo": []string{"."}, "location": "MESH_EXTERNAL", "ports": []any{object{"number": d.Port, "name": portName, "protocol": proto}}}
		if d.Protocol == "TCP" {
			eps := []any{}
			for _, ip := range d.IPs {
				eps = append(eps, object{"address": ip})
			}
			spec["addresses"], spec["resolution"], spec["endpoints"] = d.IPs, "STATIC", eps
		} else {
			resolution := "DNS"
			if net.ParseIP(d.Address) != nil {
				resolution = "STATIC"
			}
			spec["resolution"], spec["endpoints"] = resolution, []any{object{"address": d.Address, "ports": object{portName: d.EndpointPort}}}
		}
		_ = p.add(ns, owner, "ServiceEntry", resourceName("egress-service", host), spec)
	}
	for _, sa := range keys(p.Requesters) {
		http, tcp, hosts := []any{}, []any{}, []string{}
		for _, d := range p.Requesters[sa] {
			scope := "."
			if d.Internal {
				scope = strings.Split(d.Host, ".")[1]
			}
			hosts = append(hosts, scope+"/"+d.Host)
			switch d.Protocol {
			case "HTTP", "MTLS":
				for _, alias := range d.Aliases {
					for _, authority := range []string{alias, fmt.Sprintf("%s:%d", alias, d.Port)} {
						http = append(http, conjunction(object{"destination_port": d.Port}, object{"header": object{"name": ":authority", "string_match": object{"exact": authority, "ignore_case": true}}}))
					}
				}
			case "HTTPS":
				for _, alias := range d.Aliases {
					rules := []any{object{"destination_port": d.Port}, object{"requested_server_name": object{"exact": alias}}}
					if d.Internal {
						ips := []any{}
						for _, ip := range d.IPs {
							ips = append(ips, ipRule(ip))
						}
						rules = append(rules, object{"or_rules": object{"rules": ips}})
					}
					tcp = append(tcp, conjunction(rules...))
				}
			case "TCP":
				for _, ip := range d.IPs {
					tcp = append(tcp, conjunction(object{"destination_port": d.Port}, ipRule(ip)))
				}
			}
		}
		hosts = unique(hosts)
		if len(hosts) == 0 {
			hosts = []string{"~/*"}
		}
		_ = p.add(ns, owner, "Sidecar", resourceName("egress-scope", sa), object{"workloadSelector": object{"labels": object{label: saLabel(sa)}}, "outboundTrafficPolicy": object{"mode": "REGISTRY_ONLY"}, "egress": []any{object{"hosts": hosts}}})
		_ = p.add(ns, owner, "EnvoyFilter", resourceName("egress-guard", sa), object{"workloadSelector": object{"labels": object{label: saLabel(sa)}}, "configPatches": []any{
			object{"applyTo": "HTTP_FILTER", "match": object{"context": "SIDECAR_OUTBOUND", "listener": object{"filterChain": object{"filter": object{"name": "envoy.filters.network.http_connection_manager", "subFilter": object{"name": "envoy.filters.http.router"}}}}}, "patch": object{"operation": "INSERT_BEFORE", "value": object{"name": "agent_mesh.egress.http", "typed_config": object{"@type": "type.googleapis.com/envoy.extensions.filters.http.rbac.v3.RBAC", "rules": policy(http)}}}},
			object{"applyTo": "NETWORK_FILTER", "match": object{"context": "SIDECAR_OUTBOUND", "listener": object{"filterChain": object{"filter": object{"name": "envoy.filters.network.tcp_proxy"}}}}, "patch": object{"operation": "INSERT_BEFORE", "value": object{"name": "agent_mesh.egress.tcp", "typed_config": object{"@type": "type.googleapis.com/envoy.extensions.filters.network.rbac.v3.RBAC", "stat_prefix": "agent_mesh_egress", "rules": policy(tcp)}}}}}})
	}
	for _, host := range keys(exposures) {
		if e := renderExposure(p, ns, owner, bundle, exposures[host], services, pods, gateways, routes); e != nil {
			return nil, e
		}
	}
	// Check after preserving gateway validation constraints, not before.
	for _, o := range p.Objects {
		if len(encode(o)) >= 900*1024 {
			return nil, fmt.Errorf("generated object exceeds 900 KiB; reduce destinations or bundle size")
		}
	}
	return p, nil
}

func renderExposure(p *Plan, ns string, owner object, bundle string, s ExposeSpec, services map[string]object, pods, gateways, routes []object) error {
	svc, ok := services[s.Service]
	if !ok {
		return fmt.Errorf("backend Service does not exist: %s", s.Service)
	}
	selector := obj(at(svc, "spec", "selector"))
	if len(selector) == 0 || textAt(svc, "spec", "type") == "ExternalName" {
		return fmt.Errorf("exposed Service must select local pods")
	}
	found := false
	for _, port := range arr(at(svc, "spec", "ports")) {
		if integer(at(port, "port")) == s.Port && (textAt(port, "protocol") == "" || textAt(port, "protocol") == "TCP") {
			found = true
		}
	}
	if !found {
		return fmt.Errorf("backend TCP Service port not found")
	}
	for _, pod := range pods {
		if selected(pod, selector) && !hasProxy(pod) {
			return fmt.Errorf("backend requires istio-proxy")
		}
	}
	gsvc, ok := services[s.GatewayService]
	if !ok {
		return fmt.Errorf("configure existing gateway Service")
	}
	gsel := obj(at(gsvc, "spec", "selector"))
	sel := object{}
	for k, v := range s.GatewaySelector {
		sel[k] = v
	}
	chosen := []object{}
	for _, pod := range pods {
		a, b := selected(pod, sel), len(gsel) > 0 && selected(pod, gsel)
		if a != b {
			return fmt.Errorf("gatewaySelector must select exactly gateway Service pods")
		}
		if a {
			if !hasProxy(pod) {
				return fmt.Errorf("gateway pod missing istio-proxy")
			}
			chosen = append(chosen, pod)
		}
	}
	if len(chosen) == 0 {
		return fmt.Errorf("no gateway pods selected")
	}
	var gp object
	for _, port := range arr(at(gsvc, "spec", "ports")) {
		if integer(at(port, "port")) == s.GatewayPort {
			if gp != nil {
				return fmt.Errorf("ambiguous gateway port")
			}
			gp = obj(port)
		}
	}
	if gp == nil || !(textAt(gp, "protocol") == "" || textAt(gp, "protocol") == "TCP") {
		return fmt.Errorf("gateway TCP Service port not found")
	}
	listener := s.GatewayPort
	if gp["targetPort"] != nil {
		listener = integer(gp["targetPort"])
		if portName := str(gp["targetPort"]); portName != "" {
			numbers := map[int]bool{}
			for _, pod := range chosen {
				local := map[int]bool{}
				for _, c := range containers(pod) {
					for _, port := range arr(at(c, "ports")) {
						if textAt(port, "name") == portName {
							local[integer(at(port, "containerPort"))] = true
						}
					}
				}
				if len(local) != 1 {
					return fmt.Errorf("cannot resolve gateway named targetPort on each selected pod")
				}
				for n := range local {
					numbers[n] = true
				}
			}
			if len(numbers) != 1 {
				return fmt.Errorf("gateway named targetPort is inconsistent")
			}
			for n := range numbers {
				listener = n
			}
		}
	}
	if listener < 1 || listener > 65535 {
		return fmt.Errorf("invalid gateway listener port")
	}
	gatewayName := ""
	var tls object
	count := 0
	for _, g := range gateways {
		gs := obj(at(g, "spec", "selector"))
		if len(gs) == 0 {
			continue
		}
		matches := true
		for _, pod := range chosen {
			matches = matches && selected(pod, gs)
		}
		if !matches {
			continue
		}
		for _, server := range arr(at(g, "spec", "servers")) {
			hosts := stringsOf(at(server, "hosts"))
			matches = false
			for _, h := range []string{s.Host, ns + "/" + s.Host, "*/" + s.Host, "./" + s.Host} {
				matches = matches || has(hosts, h)
			}
			if !matches || integer(at(server, "port", "number")) != s.GatewayPort {
				continue
			}
			if textAt(server, "port", "protocol") != "HTTPS" || textAt(server, "tls", "mode") != "MUTUAL" {
				return fmt.Errorf("gateway owner must configure HTTPS MUTUAL listener")
			}
			count++
			gatewayName = textAt(g, "metadata", "name")
			tls = obj(at(server, "tls"))
		}
	}
	if count != 1 {
		return fmt.Errorf("require exactly one existing owner-managed Gateway server with exact SNI and port")
	}
	found = false
	backendNames := []string{s.Service, s.Service + "." + ns, s.Service + "." + ns + ".svc", s.Service + "." + ns + ".svc.cluster.local"}
	for _, r := range routes {
		if !has(stringsOf(at(r, "spec", "hosts")), s.Host) {
			continue
		}
		bound := stringsOf(at(r, "spec", "gateways"))
		if !has(bound, gatewayName) && !has(bound, ns+"/"+gatewayName) {
			continue
		}
		for _, http := range arr(at(r, "spec", "http")) {
			for _, route := range arr(at(http, "route")) {
				if has(backendNames, textAt(route, "destination", "host")) && integer(at(route, "destination", "port", "number")) == s.Port {
					found = true
				}
			}
		}
	}
	if !found {
		return fmt.Errorf("gateway owner must configure VirtualService route to declared Service and port")
	}
	validation := object{"trusted_ca": object{"inline_string": bundle}}
	sans := []any{}
	for _, s := range stringsOf(tls["subjectAltNames"]) {
		sans = append(sans, object{"exact": s})
	}
	if len(sans) > 0 {
		validation["match_subject_alt_names"] = sans
	}
	for k, v := range map[string]string{"verifyCertificateSpki": "verify_certificate_spki", "verifyCertificateHash": "verify_certificate_hash"} {
		if len(arr(tls[k])) > 0 {
			validation[v] = tls[k]
		}
	}
	return p.add(ns, owner, "EnvoyFilter", resourceName("expose", s.Host), object{"workloadSelector": object{"labels": sel}, "configPatches": []any{object{"applyTo": "FILTER_CHAIN", "match": object{"context": "GATEWAY", "listener": object{"portNumber": listener, "filterChain": object{"sni": s.Host}}}, "patch": object{"operation": "MERGE", "value": tlsSocket("Downstream", validation)}}}})
}
