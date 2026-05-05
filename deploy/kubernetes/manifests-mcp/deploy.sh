#!/bin/bash
# Deploy script for MCP Observability Server

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
MCP_DIR="$PROJECT_ROOT/mcp-observability-server"
K8S_DIR="$PROJECT_ROOT/deploy/kubernetes"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Functions
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

# Help
show_help() {
    cat << EOF
MCP Observability Server Deploy Script

Usage: $0 [command] [options]

Commands:
    build           Build Docker image
    deploy          Deploy to Kubernetes
    undeploy        Remove from Kubernetes
    logs            Show pod logs
    port-forward    Setup port-forward
    status          Show deployment status
    help            Show this help message

Options:
    --registry=<registry>   Docker registry (default: microservices-demo)
    --tag=<tag>            Image tag (default: latest)
    --namespace=<ns>       Kubernetes namespace (default: mcp-server)

Examples:
    $0 build
    $0 build --registry=myregistry --tag=v1.0.0
    $0 deploy --namespace=monitoring
    $0 logs
    $0 port-forward

EOF
}

# Parse arguments
REGISTRY="microservices-demo"
TAG="latest"
NAMESPACE="mcp-server"

while [[ $# -gt 0 ]]; do
    case $1 in
        --registry=*)
            REGISTRY="${1#*=}"
            shift
            ;;
        --tag=*)
            TAG="${1#*=}"
            shift
            ;;
        --namespace=*)
            NAMESPACE="${1#*=}"
            shift
            ;;
        help|-h|--help)
            show_help
            exit 0
            ;;
        *)
            COMMAND="$1"
            shift
            ;;
    esac
done

IMAGE="${REGISTRY}/mcp-observability-server:${TAG}"

# Commands
build_image() {
    log_info "Building Docker image: $IMAGE"
    cd "$MCP_DIR"
    docker build -t "$IMAGE" -f Dockerfile .
    log_success "Image built successfully"
}

deploy_k8s() {
    log_info "Deploying to Kubernetes namespace: $NAMESPACE"
    
    if ! kubectl get namespace "$NAMESPACE" &> /dev/null; then
        log_info "Creating namespace $NAMESPACE..."
        kubectl create namespace "$NAMESPACE"
    fi
    
    log_info "Applying manifests..."
    kubectl apply -f "$K8S_DIR/manifests-mcp/00-mcp-ns.yaml"
    kubectl apply -f "$K8S_DIR/manifests-mcp/01-mcp-configmap.yaml"
    kubectl apply -f "$K8S_DIR/manifests-mcp/02-mcp-dep.yaml"
    kubectl apply -f "$K8S_DIR/manifests-mcp/03-mcp-svc.yaml"
    
    log_success "Deployment completed"
    log_info "Waiting for pod to be ready..."
    kubectl rollout status deployment/mcp-observability-deployment -n "$NAMESPACE" --timeout=5m
    log_success "Pod is ready!"
}

undeploy_k8s() {
    log_warn "Removing deployment from namespace: $NAMESPACE"
    read -p "Are you sure? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        kubectl delete namespace "$NAMESPACE" || log_error "Failed to delete namespace"
        log_success "Namespace deleted"
    else
        log_info "Cancelled"
    fi
}

show_logs() {
    log_info "Showing logs from $NAMESPACE namespace..."
    kubectl logs -f deployment/mcp-observability-deployment -n "$NAMESPACE" --tail=50
}

setup_port_forward() {
    log_info "Setting up port-forward from localhost:8000 to mcp-observability:8000"
    kubectl port-forward -n "$NAMESPACE" svc/mcp-observability 8000:8000
}

show_status() {
    log_info "Deployment status in namespace: $NAMESPACE"
    echo
    echo "Deployments:"
    kubectl get deployments -n "$NAMESPACE" -o wide
    echo
    echo "Pods:"
    kubectl get pods -n "$NAMESPACE" -o wide
    echo
    echo "Services:"
    kubectl get svc -n "$NAMESPACE" -o wide
    echo
    echo "ConfigMaps:"
    kubectl get configmap -n "$NAMESPACE" -o wide
}

# Main
case "$COMMAND" in
    build)
        build_image
        ;;
    deploy)
        build_image
        deploy_k8s
        ;;
    undeploy)
        undeploy_k8s
        ;;
    logs)
        show_logs
        ;;
    port-forward)
        setup_port_forward
        ;;
    status)
        show_status
        ;;
    *)
        show_help
        if [ -n "$COMMAND" ]; then
            log_error "Unknown command: $COMMAND"
            exit 1
        fi
        ;;
esac
