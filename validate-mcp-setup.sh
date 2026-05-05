#!/bin/bash
# MCP Observability Server - Setup Validation Checklist

echo "════════════════════════════════════════════════════════════════════════════════"
echo "MCP OBSERVABILITY SERVER - SETUP VALIDATION CHECKLIST"
echo "════════════════════════════════════════════════════════════════════════════════"
echo ""

# Color codes
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Check function
check_file() {
    if [ -f "$1" ]; then
        echo -e "${GREEN}✓${NC} $1"
        return 0
    else
        echo -e "${RED}✗${NC} $1"
        return 1
    fi
}

check_dir() {
    if [ -d "$1" ]; then
        echo -e "${GREEN}✓${NC} $1"
        return 0
    else
        echo -e "${RED}✗${NC} $1"
        return 1
    fi
}

check_executable() {
    if [ -x "$1" ]; then
        echo -e "${GREEN}✓${NC} $1 (executable)"
        return 0
    else
        echo -e "${RED}✗${NC} $1 (not executable)"
        return 1
    fi
}

# Counters
TOTAL=0
PASSED=0

# 1. Check mcp-observability-server directory
echo "📁 [1/4] MCP Application Files"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

FILES_MCP=(
    "mcp-observability-server/main.py"
    "mcp-observability-server/config.py"
    "mcp-observability-server/prometheus_client.py"
    "mcp-observability-server/loki_client.py"
    "mcp-observability-server/metrics.py"
    "mcp-observability-server/Dockerfile"
    "mcp-observability-server/docker-compose.yml"
    "mcp-observability-server/requirements.txt"
    "mcp-observability-server/.env.example"
    "mcp-observability-server/.dockerignore"
    "mcp-observability-server/README.md"
)

for file in "${FILES_MCP[@]}"; do
    if check_file "$file"; then
        ((PASSED++))
    fi
    ((TOTAL++))
done

echo ""

# 2. Check Kubernetes manifests
echo "☸️  [2/4] Kubernetes Manifests"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

FILES_K8S=(
    "deploy/kubernetes/manifests-mcp/00-mcp-ns.yaml"
    "deploy/kubernetes/manifests-mcp/01-mcp-configmap.yaml"
    "deploy/kubernetes/manifests-mcp/02-mcp-dep.yaml"
    "deploy/kubernetes/manifests-mcp/03-mcp-svc.yaml"
    "deploy/kubernetes/manifests-mcp/04-mcp-ingress.yaml"
)

for file in "${FILES_K8S[@]}"; do
    if check_file "$file"; then
        ((PASSED++))
    fi
    ((TOTAL++))
done

echo ""

# 3. Check scripts
echo "🔧 [3/4] Helper Scripts"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

SCRIPTS=(
    "deploy/kubernetes/manifests-mcp/deploy.sh"
    "deploy/kubernetes/manifests-mcp/test.sh"
    "SETUP_MCP.sh"
)

for script in "${SCRIPTS[@]}"; do
    if check_executable "$script"; then
        ((PASSED++))
    fi
    ((TOTAL++))
done

echo ""

# 4. Check documentation
echo "📚 [4/4] Documentation"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

DOCS=(
    "deploy/kubernetes/manifests-mcp/README.md"
    "deploy/kubernetes/manifests-mcp/ARCHITECTURE.md"
    "deploy/kubernetes/manifests-mcp/EXAMPLES.md"
    "MCP_QUICK_REFERENCE.md"
)

for doc in "${DOCS[@]}"; do
    if check_file "$doc"; then
        ((PASSED++))
    fi
    ((TOTAL++))
done

echo ""
echo "════════════════════════════════════════════════════════════════════════════════"
echo "📊 VALIDATION SUMMARY"
echo "════════════════════════════════════════════════════════════════════════════════"
echo ""
echo "Total files/scripts: $TOTAL"
echo "Validated: $PASSED"

if [ $PASSED -eq $TOTAL ]; then
    echo -e "${GREEN}Status: ALL FILES PRESENT ✓${NC}"
    echo ""
    echo "════════════════════════════════════════════════════════════════════════════════"
    echo "✅ NEXT STEPS:"
    echo "════════════════════════════════════════════════════════════════════════════════"
    echo ""
    echo "1. Build & Deploy:"
    echo "   cd deploy/kubernetes/manifests-mcp"
    echo "   ./deploy.sh deploy"
    echo ""
    echo "2. Verify Status:"
    echo "   ./deploy.sh status"
    echo ""
    echo "3. Port-Forward for Testing:"
    echo "   ./deploy.sh port-forward"
    echo ""
    echo "4. Test Health:"
    echo "   curl http://localhost:8000/health"
    echo ""
    echo "5. Run Tests:"
    echo "   ./test.sh"
    echo ""
    echo "For more info:"
    echo "   - MCP_QUICK_REFERENCE.md"
    echo "   - deploy/kubernetes/manifests-mcp/README.md"
    echo "   - deploy/kubernetes/manifests-mcp/ARCHITECTURE.md"
    echo "   - deploy/kubernetes/manifests-mcp/EXAMPLES.md"
    echo ""
else
    MISSING=$((TOTAL - PASSED))
    echo -e "${RED}Status: $MISSING FILES MISSING ✗${NC}"
    echo ""
    echo "Please ensure all files are present before deploying."
fi

echo ""
echo "════════════════════════════════════════════════════════════════════════════════"
