#!/usr/bin/env bash
# Re-quantiza o Qwen2-14B para EXL2 com a versão atual do exllamav2
# Isso corrige a incompatibilidade entre o formato 0.2.2 e os kernels atuais.
#
# Uso:
#   bash requantize.sh                  # baixa o modelo FP16 e quantiza
#   bash requantize.sh --skip-download  # pula o download (modelo FP16 já existe)
#   bash requantize.sh --resume         # retoma quantização interrompida

set -euo pipefail

# ── Configuração ──────────────────────────────────────────────────────────────
HF_REPO="Qwen/Qwen3-14B"
FP16_DIR="$HOME/models/qwen14b-fp16"
WORK_DIR="$HOME/models/qwen14b-exl2-work"   # arquivos temporários da quantização
OUT_DIR="$HOME/models/qwen14b-exl2-v2"      # modelo final pronto para uso

BITS=4.0     # bits por peso (mesmo da quantização original)
HEAD_BITS=6  # bits para camadas head (mesmo da quantização original)
CAL_ROWS=115 # linhas de calibração (mesmo da quantização original)
CAL_LEN=2048 # tokens por amostra de calibração

VENV_ACTIVATE="$(dirname "$0")/../.venv/bin/activate"
# ─────────────────────────────────────────────────────────────────────────────

SKIP_DOWNLOAD=false
RESUME=false
for arg in "$@"; do
  case "$arg" in
    --skip-download) SKIP_DOWNLOAD=true ;;
    --resume)        RESUME=true ;;
  esac
done

# Ativa o venv
if [ -f "$VENV_ACTIVATE" ]; then
  source "$VENV_ACTIVATE"
else
  echo "AVISO: .venv não encontrado em $VENV_ACTIVATE, usando Python do sistema"
fi

# Localiza o convert_exl2.py instalado
CONVERT_SCRIPT=$(python -c "
import exllamav2, os
pkg = os.path.dirname(exllamav2.__file__)
script = os.path.join(pkg, 'conversion', 'convert_exl2.py')
print(script)
")

if [ ! -f "$CONVERT_SCRIPT" ]; then
  echo "ERRO: convert_exl2.py não encontrado em $CONVERT_SCRIPT"
  exit 1
fi
echo "Script de conversão: $CONVERT_SCRIPT"

# ── 1. Download do modelo FP16 ────────────────────────────────────────────────
if [ "$SKIP_DOWNLOAD" = false ]; then
  echo ""
  echo "=== Baixando $HF_REPO (~28 GB) ==="
  echo "    Destino: $FP16_DIR"
  echo "    (use --skip-download se o modelo já existir)"
  echo ""
  python - <<PYEOF
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id="$HF_REPO",
    local_dir="$FP16_DIR",
    ignore_patterns=["*.gguf", "*.bin"],  # baixa apenas safetensors
)
print("Download concluído.")
PYEOF
else
  echo "Download ignorado. Usando: $FP16_DIR"
fi

if [ ! -d "$FP16_DIR" ]; then
  echo "ERRO: diretório do modelo FP16 não encontrado: $FP16_DIR"
  exit 1
fi

# ── 2. Quantização ────────────────────────────────────────────────────────────
echo ""
echo "=== Iniciando quantização EXL2 ==="
echo "    Entrada : $FP16_DIR"
echo "    Trabalho: $WORK_DIR"
echo "    Saída   : $OUT_DIR"
echo "    Bits    : $BITS  |  Head bits: $HEAD_BITS  |  Cal rows: $CAL_ROWS"
echo ""
echo "Tempo estimado: 30–90 min dependendo da GPU."
echo ""

RESUME_FLAG=""
if [ "$RESUME" = true ]; then
  RESUME_FLAG="-res"
fi

python -c '
# Workaround: exllamav2/conversion/tokenize.py shadows stdlib tokenize when the
# script is run directly (Python inserts its directory at sys.path[0]).
# Running via -c keeps sys.path[0]="" so stdlib is found first; runpy then
# executes the script without re-inserting its directory.
import sys, tokenize, linecache  # cache stdlib modules before any shadow
sys.argv = sys.argv[1:]           # shift so argv[0] = script path
import runpy
runpy.run_path(sys.argv[0], run_name="__main__")
' "$CONVERT_SCRIPT" \
  -i  "$FP16_DIR" \
  -o  "$WORK_DIR" \
  -cf "$OUT_DIR" \
  -b  "$BITS" \
  -hb "$HEAD_BITS" \
  -r  "$CAL_ROWS" \
  -l  "$CAL_LEN" \
  -hsol 8 \
  $RESUME_FLAG

echo ""
echo "=== Quantização concluída! ==="
echo ""
echo "Modelo pronto em: $OUT_DIR"
echo ""
echo "Para usar, exporte a variável antes de rodar agent.py:"
echo "  export MODEL_DIR=\"$OUT_DIR\""
echo "  python agent.py"
