package main

import (
	"fmt"
	"log"
	"net"
	"regexp"
	"sort"
	"strings"
	"time"
)

func owned(o, owner object) bool {
	if textAt(o, "metadata", "labels", managed) != manager {
		return false
	}
	for _, r := range arr(at(o, "metadata", "ownerReferences")) {
		if at(r, "uid") == owner["uid"] && textAt(r, "kind") == "ConfigMap" {
			return true
		}
	}
	return false
}
func applyOwned(api API, want, current, owner object) error {
	if current == nil {
		return api.Create(want)
	}
	if !owned(current, owner) {
		return fmt.Errorf("refusing to overwrite unowned %s", objectKey(want))
	}
	if same(current["spec"], want["spec"]) {
		return nil
	}
	meta := clone(obj(current["metadata"]))
	return api.Patch(textAt(want, "kind"), textAt(want, "metadata", "name"), []any{object{"op": "test", "path": "/metadata/resourceVersion", "value": at(current, "metadata", "resourceVersion")}, object{"op": "replace", "path": "/spec", "value": want["spec"]}, object{"op": "replace", "path": "/metadata", "value": meta}}, false, true)
}
func statusValue(a object, ok bool, message string, urls []string) object {
	if len(message) > 1000 {
		message = message[:1000]
	}
	s, reason := "False", "ReconcileFailed"
	if ok {
		s, reason = "True", "Reconciled"
	}
	now := time.Now().UTC().Format(time.RFC3339)
	for _, c := range arr(at(a, "status", "conditions")) {
		if textAt(c, "type") == "Configured" && textAt(c, "status") == s {
			now = textAt(c, "lastTransitionTime")
		}
	}
	generation := at(a, "metadata", "generation")
	if generation == nil {
		generation = 1
	}
	urls = unique(urls)
	return object{"observedGeneration": generation, "applicationURLs": urls, "conditions": []any{object{"type": "Configured", "status": s, "reason": reason, "message": message, "observedGeneration": generation, "lastTransitionTime": now}}}
}
func writeStatus(api API, a object, ok bool, message string, urls []string) error {
	v := statusValue(a, ok, message, urls)
	if same(v, a["status"]) {
		return nil
	}
	return api.Patch(textAt(a, "kind"), textAt(a, "metadata", "name"), object{"metadata": object{"resourceVersion": at(a, "metadata", "resourceVersion")}, "status": v}, true, false)
}

type Trust struct {
	Bundles map[string]string
	Errors  map[string]string
}

func validateBundles(api API) (Trust, error) {
	t := Trust{map[string]string{}, map[string]string{}}
	items, e := api.List("AgentMeshTrustedBundle", "")
	if e != nil {
		return t, e
	}
	for _, a := range items {
		if at(a, "metadata", "deletionTimestamp") != nil {
			continue
		}
		name := textAt(a, "metadata", "name")
		pem := textAt(a, "spec", "caBundle")
		e := publicBundle(pem)
		message := "Public PEM certificates validated; verify namespace configuration and active TLS separately."
		if e != nil {
			t.Errors[name] = e.Error()
			message = e.Error()
		} else {
			t.Bundles[name] = pem
		}
		if e = writeStatus(api, a, e == nil, message, nil); e != nil {
			return t, e
		}
	}
	return t, nil
}
func needsTrust(as []object) bool {
	for _, a := range as {
		if textAt(a, "kind") == "AgentMeshExpose" {
			return true
		}
		for _, field := range []string{"inCluster", "outCluster"} {
			for _, d := range arr(at(a, "spec", field)) {
				if strings.EqualFold(textAt(d, "protocol"), "MTLS") {
					return true
				}
			}
		}
	}
	return false
}

var retiredRoute = regexp.MustCompile(`^ma-expose-[0-9a-f]{16}$`)

func preserveRetired(o object) bool {
	return has([]string{"VirtualService", "DestinationRule"}, textAt(o, "kind")) && retiredRoute.MatchString(textAt(o, "metadata", "name"))
}

// executePlan never writes gateway resources, authorization policies, Secrets, or trust inputs.
func executePlan(api API, cfg Config, anchor object, trust Trust, declarations []object) (map[string][]string, bool, error) {
	bundle := trust.Bundles[cfg.TrustBundle.Name]
	if needsTrust(declarations) {
		if msg, ok := trust.Errors[cfg.TrustBundle.Name]; ok {
			return nil, false, fmt.Errorf("invalid required trust bundle %s: %s", cfg.TrustBundle.Name, msg)
		}
		if bundle == "" {
			return nil, false, fmt.Errorf("required trust bundle does not exist: %s", cfg.TrustBundle.Name)
		}
	}
	servicesList, e := api.List("Service", "")
	if e != nil {
		return nil, false, e
	}
	services := map[string]object{}
	for _, s := range servicesList {
		services[textAt(s, "metadata", "name")] = s
	}
	allPods, e := api.List("Pod", "")
	if e != nil {
		return nil, false, e
	}
	pods := []object{}
	for _, pod := range allPods {
		if at(pod, "metadata", "deletionTimestamp") == nil && !has([]string{"Succeeded", "Failed"}, textAt(pod, "status", "phase")) {
			pods = append(pods, pod)
		}
	}
	sas, e := api.List("ServiceAccount", "")
	if e != nil {
		return nil, false, e
	}
	accounts := map[string]bool{}
	for _, sa := range sas {
		accounts[textAt(sa, "metadata", "name")] = true
	}
	current := map[string]object{}
	routes := []object{}
	for _, kind := range order {
		list, e := api.List(kind, "")
		if e != nil {
			return nil, false, e
		}
		for _, o := range list {
			current[objectKey(o)] = o
		}
		if kind == "VirtualService" {
			routes = list
		}
	}
	gateways, e := api.List("Gateway", "")
	if e != nil {
		return nil, false, e
	}
	owner := obj(anchor["metadata"])
	plan, e := buildPlan(api.Namespace(), cfg, owner, bundle, declarations, services, pods, accounts, gateways, routes, net.LookupHost)
	if e != nil {
		return nil, false, e
	}
	ourHosts := []string{}
	for k, d := range plan.Objects {
		if old := current[k]; old != nil && !owned(old, owner) {
			return nil, false, fmt.Errorf("unowned resource name collision: %s", k)
		}
		if has([]string{"ServiceEntry", "VirtualService"}, textAt(d, "kind")) { // Plan objects use typed slices until serialized.
			if hs, ok := at(d, "spec", "hosts").([]string); ok {
				ourHosts = append(ourHosts, hs...)
			}
		}
	}
	for _, d := range current {
		if owned(d, owner) {
			continue
		}
		kind := textAt(d, "kind")
		if kind == "Sidecar" {
			sel := obj(at(d, "spec", "workloadSelector", "labels"))
			for _, pod := range pods {
				if _, ok := plan.Requesters[serviceAccount(pod)]; ok && len(sel) > 0 && selected(pod, sel) {
					return nil, false, fmt.Errorf("unmanaged Sidecar overlaps enrolled ServiceAccount")
				}
			}
		}
		hosts := []string{}
		if kind == "DestinationRule" {
			hosts = []string{textAt(d, "spec", "host")}
		}
		if has([]string{"ServiceEntry", "VirtualService"}, kind) {
			hosts = stringsOf(at(d, "spec", "hosts"))
		}
		for _, h := range hosts {
			for _, ours := range ourHosts {
				if h == ours || h == "*" || (strings.HasPrefix(h, "*.") && strings.HasSuffix(ours, h[1:])) {
					return nil, false, fmt.Errorf("unmanaged %s overlaps requested hostname %s", objectKey(d), h)
				}
			}
		}
	}
	// Preflight every workload before mutating any template or generated resource.
	changes := []object{}
	for _, kind := range []string{"Deployment", "StatefulSet", "DaemonSet"} {
		workloads, e := api.List(kind, "")
		if e != nil {
			return nil, false, e
		}
		for _, w := range workloads {
			template := obj(at(w, "spec", "template"))
			_, requested := plan.Requesters[serviceAccount(template)]
			isGateway := textAt(template, "metadata", "labels", gatewayLabel) != ""
			for _, g := range gateways {
				sel := obj(at(g, "spec", "selector"))
				isGateway = isGateway || (len(sel) > 0 && selected(template, sel))
			}
			if isGateway {
				if requested {
					return nil, false, fmt.Errorf("gateway workloads are owner-managed; use a separate requester ServiceAccount")
				}
				continue
			}
			wanted := object{}
			if requested {
				wanted[label] = saLabel(serviceAccount(template))
			}
			old := obj(at(template, "metadata", "labels"))
			if old[label] == nil && !requested {
				continue
			}
			var desiredStamp any
			if requested {
				desiredStamp = string(encode(wanted))
			}
			if !same(old[label], wanted[label]) || !same(at(template, "metadata", "annotations", stamp), desiredStamp) {
				changes = append(changes, object{"kind": kind, "name": textAt(w, "metadata", "name"), "body": object{"metadata": object{"resourceVersion": at(w, "metadata", "resourceVersion")}, "spec": object{"template": object{"metadata": object{"labels": object{label: wanted[label]}, "annotations": object{stamp: desiredStamp}}}}}})
			}
		}
	}
	pending := false
	for _, pod := range pods {
		wanted := plan.Labels[textAt(pod, "metadata", "name")]
		if len(wanted) > 0 && (!selected(pod, wanted) || textAt(pod, "metadata", "annotations", stamp) != string(encode(wanted))) {
			pending = true
		}
	}
	for _, change := range changes {
		if e := api.Patch(str(change["kind"]), str(change["name"]), change["body"], false, false); e != nil {
			return nil, false, e
		}
	}
	for _, kind := range order {
		for _, k := range keys(plan.Objects) {
			d := plan.Objects[k]
			if textAt(d, "kind") == kind {
				if e := applyOwned(api, d, current[k], owner); e != nil {
					return nil, false, e
				}
			}
		}
	}
	for i := len(order) - 1; i >= 0; i-- {
		for _, k := range keys(current) {
			d := current[k]
			if textAt(d, "kind") == order[i] && plan.Objects[k] == nil && owned(d, owner) && !preserveRetired(d) {
				if e := api.Delete(d); e != nil {
					return nil, false, e
				}
			}
		}
	}
	return plan.URLs, !pending, nil
}
func resultMessage(ok bool, e error) string {
	if e != nil {
		return e.Error()
	}
	if !ok {
		return "Waiting for workload rollout with bootstrap labels. Jobs/bare pods need pre-stamped labels; OnDelete workloads need replacement."
	}
	return "Resources reconciled. Verify Envoy active config and application traffic separately."
}
func reconcileNamespace(api API, cfg Config, anchor object, trust Trust, declarations []object) bool {
	urls, ok, e := executePlan(api, cfg, anchor, trust, declarations)
	message := resultMessage(ok, e)
	if e != nil {
		log.Printf("namespace %s: %s", api.Namespace(), message)
	}
	result := ok && e == nil
	for _, a := range declarations {
		u := urls[objectKey(a)]
		if e != nil {
			u = stringsOf(at(a, "status", "applicationURLs"))
		}
		if err := writeStatus(api, a, result, message, u); err != nil {
			log.Printf("status %s: %v", objectKey(a), err)
			result = false
		}
	}
	return result
}
func ensureAnchor(api API, name string) (object, error) {
	cm, e := api.Get("ConfigMap", name)
	if codeIs(e, 404) {
		e = api.Create(object{"apiVersion": "v1", "kind": "ConfigMap", "metadata": object{"name": name, "namespace": api.Namespace(), "labels": object{stateLabel: manager}}, "data": object{"purpose": "AgentMesh resource ownership; administrator settings live centrally."}})
		if e != nil {
			return nil, e
		}
		return api.Get("ConfigMap", name)
	}
	if e != nil {
		return nil, e
	}
	if textAt(cm, "metadata", "labels", stateLabel) != manager {
		if e = api.Patch("ConfigMap", name, object{"metadata": object{"resourceVersion": at(cm, "metadata", "resourceVersion"), "labels": object{stateLabel: manager}}}, false, false); e != nil {
			return nil, e
		}
		return api.Get("ConfigMap", name)
	}
	return cm, nil
}
func reconcileCluster(api API, configName string) bool {
	cluster := api.Scoped("")
	byNS := map[string][]object{}
	for _, kind := range []string{"AgentMeshEgress", "AgentMeshExpose"} {
		items, e := cluster.List(kind, "")
		if e != nil {
			log.Print(e)
			return false
		}
		for _, a := range items {
			if at(a, "metadata", "deletionTimestamp") == nil {
				ns := textAt(a, "metadata", "namespace")
				byNS[ns] = append(byNS[ns], a)
			}
		}
	}
	fail := func(e error) bool {
		log.Print(e)
		for ns, as := range byNS {
			for _, a := range as {
				_ = writeStatus(api.Scoped(ns), a, false, e.Error(), nil)
			}
		}
		return false
	}
	trust, e := validateBundles(cluster)
	if e != nil {
		return fail(e)
	}
	cm, e := api.Get("ConfigMap", configName)
	if e != nil {
		return fail(e)
	}
	cfg, e := readConfig(textAt(cm, "data", "config.json"))
	if e != nil {
		return fail(e)
	}
	cfg.ForbiddenNamespaces = append(cfg.ForbiddenNamespaces, api.Namespace())
	anchors, e := cluster.List("ConfigMap", stateLabel+"="+manager)
	if e != nil {
		return fail(e)
	}
	for _, a := range anchors {
		if textAt(a, "metadata", "name") == configName {
			ns := textAt(a, "metadata", "namespace")
			if byNS[ns] == nil {
				byNS[ns] = []object{}
			}
		}
	}
	namespaces, e := cluster.List("Namespace", "")
	if e != nil {
		return fail(e)
	}
	active := map[string]bool{}
	for _, ns := range namespaces {
		if at(ns, "metadata", "deletionTimestamp") == nil && textAt(ns, "status", "phase") != "Terminating" {
			active[textAt(ns, "metadata", "name")] = true
		}
	}
	ok := true
	for _, ns := range keys(byNS) {
		if !active[ns] {
			continue
		}
		as := byNS[ns]
		scoped := api.Scoped(ns)
		if has(cfg.ForbiddenNamespaces, ns) {
			for _, a := range as {
				_ = writeStatus(scoped, a, false, "Refusing reconciliation in a forbidden namespace", nil)
			}
			ok = false
			continue
		}
		anchor, e := ensureAnchor(scoped, configName)
		if e != nil {
			log.Print(e)
			ok = false
			continue
		}
		ok = reconcileNamespace(scoped, cfg, anchor, trust, as) && ok
	}
	return ok
}

// ConfigMap mode uses only named GETs and a separate, pre-created status/ownership anchor.
// The controller has no permission to write its declarations, settings, or public trust input.
func configMapDeclarations(cm object) ([]object, error) {
	type document struct {
		SchemaVersion int      `json:"schemaVersion"`
		Egress        []object `json:"egress"`
		Expose        []object `json:"expose"`
	}
	d := document{}
	if e := strictJSON(textAt(cm, "data", "declarations.json"), &d); e != nil {
		return nil, e
	}
	if d.SchemaVersion != 1 {
		return nil, fmt.Errorf("declarations.json schemaVersion must be 1")
	}
	out := []object{}
	seen := map[string]bool{}
	for i, list := range [][]object{d.Egress, d.Expose} {
		kind := []string{"AgentMeshEgress", "AgentMeshExpose"}[i]
		for _, entry := range list {
			s := clone(entry)
			name := str(s["name"])
			if e := dns(name); e != nil {
				return nil, e
			}
			delete(s, "name")
			key := kind + "/" + name
			if seen[key] {
				return nil, fmt.Errorf("duplicate declaration %s", key)
			}
			seen[key] = true
			out = append(out, object{"apiVersion": version, "kind": kind, "metadata": object{"name": name, "namespace": at(cm, "metadata", "namespace"), "generation": 1}, "spec": s})
		}
	}
	sort.Slice(out, func(i, j int) bool { return objectKey(out[i]) < objectKey(out[j]) })
	return out, nil
}
func reconcileConfigMaps(api API, configName, declarationName, stateName string) bool {
	anchor, e := api.Get("ConfigMap", stateName)
	if e != nil {
		log.Print(e)
		return false
	}
	summary := object{}
	inputVersion := ""
	var declarations []object
	var urls map[string][]string
	ok := false
	work := func() error {
		cm, e := api.Get("ConfigMap", configName)
		if e != nil {
			return e
		}
		cfg, e := readConfig(textAt(cm, "data", "config.json"))
		if e != nil {
			return e
		}
		if has([]string{configName, declarationName, stateName}, cfg.TrustBundle.Name) {
			return fmt.Errorf("trust ConfigMap must be separate from config, declarations and state")
		}
		input, e := api.Get("ConfigMap", declarationName)
		if e != nil {
			return e
		}
		inputVersion = textAt(input, "metadata", "resourceVersion")
		declarations, e = configMapDeclarations(input)
		if e != nil {
			return e
		}
		trust := Trust{map[string]string{}, map[string]string{}}
		if needsTrust(declarations) {
			cm, e := api.Get("ConfigMap", cfg.TrustBundle.Name)
			if e != nil {
				return e
			}
			pem := textAt(cm, "data", "ca.crt")
			if e = publicBundle(pem); e != nil {
				return e
			}
			trust.Bundles[cfg.TrustBundle.Name] = pem
		}
		urls, ok, e = executePlan(api, cfg, anchor, trust, declarations)
		return e
	}
	e = work()
	message := resultMessage(ok, e)
	if e != nil {
		log.Printf("namespace ConfigMaps %s: %v", api.Namespace(), e)
	}
	prior := object{}
	_ = strictJSON(textAt(anchor, "data", "status.json"), &prior)
	for _, a := range declarations {
		key := objectKey(a)
		a["status"] = at(prior, "declarations", key)
		summary[key] = statusValue(a, ok && e == nil, message, urls[key])
	}
	status := object{"inputResourceVersion": inputVersion, "configured": ok && e == nil, "message": message, "declarations": summary}
	if !same(prior, status) {
		if err := api.Patch("ConfigMap", stateName, object{"metadata": object{"resourceVersion": at(anchor, "metadata", "resourceVersion")}, "data": object{"status.json": string(encode(status))}}, false, false); err != nil {
			log.Print(err)
			return false
		}
	}
	return ok && e == nil
}
