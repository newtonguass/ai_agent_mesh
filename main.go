// AgentMesh reconciles captured egress and host-scoped gateway trust.
// Kubernetes and Istio configuration convergence is not proof of active traffic.
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"
)

var buildID = "development"

func main() {
	ns := flag.String("namespace", os.Getenv("POD_NAMESPACE"), "installation namespace; application namespace in ConfigMap mode")
	mode := flag.String("mode", "cluster", "cluster (CRDs) or namespace (ConfigMaps)")
	config := flag.String("config", "mesh-access-config", "administrator settings ConfigMap")
	declarations := flag.String("declarations", "mesh-access-declarations", "namespace mode declarations ConfigMap")
	state := flag.String("state", "mesh-access-state", "namespace mode status/ownership ConfigMap")
	interval := flag.Duration("interval", 5*time.Second, "reconciliation interval")
	once := flag.Bool("once", false, "reconcile once and exit")
	base := flag.String("api-url", "", "local development kubectl proxy URL")
	showVersion := flag.Bool("version", false, "print source build identity")
	flag.Parse()
	if *showVersion {
		fmt.Println(buildID)
		return
	}
	if *ns == "" || *interval < time.Second || !has([]string{"cluster", "namespace"}, *mode) {
		log.Fatal("namespace required; mode cluster|namespace; interval >=1s")
	}
	if *mode == "namespace" && (*config == *declarations || *state == *config || *state == *declarations) {
		log.Fatal("config, declarations and state must be separate ConfigMaps")
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	api, e := newKube(ctx, *ns, *base)
	if e != nil {
		log.Fatal(e)
	}
	log.Printf("AgentMesh Go build=%s mode=%s namespace=%s", buildID, *mode, *ns)
	for {
		ok := false
		if *mode == "cluster" {
			ok = reconcileCluster(api, *config)
		} else {
			ok = reconcileConfigMaps(api, *config, *declarations, *state)
		}
		if *once {
			if !ok {
				os.Exit(1)
			}
			return
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(*interval):
		}
	}
}
