#!/usr/bin/env bash
# What is ACTUALLY baked into the built extension, and is it ACTUALLY running
# on the GPU? Run against an image tag:  ./check-arch.sh anuga-gpu:latest
set -uo pipefail
IMG="${1:-anuga-gpu:latest}"

echo "=============================================="
echo " 1. Which architectures are compiled in?"
echo "=============================================="
# --list-elf  = native cubins (exact arch match required)
# --list-ptx  = PTX, JIT-able onto NEWER archs only, never older
docker run --rm "$IMG" bash -c '
  SO=$(python -c "from anuga.shallow_water import sw_domain_gpu_ext as m; print(m.__file__)" 2>/dev/null)
  [ -z "$SO" ] && { echo "could not locate extension"; exit 1; }
  echo "extension: $SO"
  echo "--- native cubins (sm_XX) ---"
  cuobjdump --list-elf "$SO" 2>/dev/null || echo "  none"
  echo "--- embedded PTX (compute_XX) ---"
  cuobjdump --list-ptx "$SO" 2>/dev/null || echo "  none  <-- no JIT fallback possible"
'

echo
echo "=============================================="
echo " 2. Does it actually launch kernels?"
echo "=============================================="
echo "NVCOMPILER_ACC_NOTIFY=3 prints every kernel launch and data transfer."
echo "Silence here means you are running on the CPU."
docker run --rm --gpus all -e NVCOMPILER_ACC_NOTIFY=3 "$IMG" \
  bash -c 'nvidia-smi --query-gpu=name,compute_cap --format=csv,noheader; \
           python /app/src/worker.py 2>&1 | head -40'

cat <<'NOTE'

READING THE OUTPUT
------------------
For the A40 you need one of:
  - a native cubin  sm_86
  - PTX at compute_86 or LOWER (JIT goes forward only)

If you see only sm_89 / compute_89, the A40 will fail or silently drop to CPU,
regardless of how well it behaves on the 5060.
NOTE
