# Installing sock-shop on Kubernetes

See the [documentation](https://microservices-demo.github.io/deployment/kubernetes-minikube.html) on how to deploy Sock Shop using Minikube.

## Kubernetes manifests

There are 2 sets of manifests for deploying Sock Shop on Kubernetes: one in the [manifests directory](manifests/), and complete-demo.yaml. The complete-demo.yaml is a single file manifest
made by concatenating all the manifests from the manifests directory, so please regenerate it when changing files in the manifests directory.

## Monitoring

All monitoring is performed by prometheus. All services expose a `/metrics` endpoint. All services have a Prometheus Histogram called `request_duration_seconds`, which is automatically appended to create the metrics `_count`, `_sum` and `_bucket`.

The manifests for the monitoring are spread across the [manifests-monitoring](./manifests-monitoring) and [manifests-alerting](./manifests-alerting/) directories.

To use them, please run `kubectl create -f <path to directory>`.

### What's Included?

* Sock-shop grafana dashboards
* Alertmanager with 500 alert connected to slack
* Prometheus with config to scrape all k8s pods, connected to local alertmanager.

### Ports

Grafana will be exposed on the NodePort `31300` and Prometheus is exposed on `31090`. If running on a real cluster, the easiest way to connect to these ports is by port forwarding in a ssh command:
```
ssh -i $KEY -L 3000:$NODE_IN_CLUSTER:31300 -L 9090:$NODE_IN_CLUSTER:31090 ubuntu@$BASTION_IP
```
Where all the pertinent information should be entered. Grafana and Prometheus will be available on `http://localhost:3000` or `:9090`.

If on Minikube, you can connect via the VM IP address and the NodePort.

## Chaos Testing with Litmus

This repository includes a starter Litmus setup for Kubernetes chaos experiments against Sock Shop.

The provided experiment installs Litmus and prepares a `pod-delete` fault against the `carts` deployment in the `sock-shop` namespace.

Use the targets below from the repository root:

```bash
make chaos-install
make chaos-run-pod-delete
make chaos-status
```

Or run everything in one step:

```bash
make chaos-up
```

To remove the experiment and Litmus resources:

```bash
make chaos-down
```

The namespace-scoped experiment manifests live in `deploy/kubernetes/manifests-chaos/`.

## ChaosCenter UI

If you want to browse and run chaos experiments from a web interface, use ChaosCenter.

From the repository root:

```bash
make chaos-center-install
make chaos-center-port-forward
make chaos-center-status
```

Or in one step:

```bash
make chaos-center-up
```

The UI will be available at `http://localhost:9091`.

Default credentials from the Litmus chart are:

```text
admin / litmus
```

Important: the standalone experiment triggered with `make chaos-run-pod-delete` is executed directly through the Litmus operator, so it will not automatically appear as a managed run inside ChaosCenter. To visualize runs in the UI, install ChaosCenter, create an environment, connect the cluster infrastructure from the portal, and launch experiments from the interface.

Recommended next flow in the UI:

1. Login with `admin/litmus`.
2. Create (or open) a project/environment.
3. Go to the environment setup and choose a Kubernetes chaos infrastructure in the `litmus` namespace.
4. Apply the generated `*-litmus-chaos-enable.yml` manifest in your terminal.
5. Wait for the infrastructure status to become `CONNECTED`.
6. Open ChaosHub/Experiments and run one of the installed Kubernetes faults (for example `pod-delete`), then inspect execution history and verdicts in the portal.

To install the full Kubernetes experiment catalog in `sock-shop` (instead of only `pod-delete`):

```bash
make chaos-experiments-install
make chaos-experiments-status
```

To stop the UI port-forward:

```bash
make chaos-center-stop-port-forward
```

To uninstall ChaosCenter:

```bash
make chaos-center-down
```
