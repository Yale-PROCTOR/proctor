#!/usr/bin/env bash
# Per-stage vector breakdown of a bench run (reads bench.json).
#
#   ./bench_report.sh                    # latest bench under out/
#   ./bench_report.sh B02_organic        # latest bench for a suite
#   ./bench_report.sh out/bench-B02_organic-20260728T161647   # a specific run
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
arg="${1:-}"

if [ -z "$arg" ]; then
  bj=$(ls -dt "$ROOT"/out/bench-*/bench.json 2>/dev/null | head -1 || true)
elif [ -f "$arg" ]; then
  bj="$arg"
elif [ -f "$arg/bench.json" ]; then
  bj="$arg/bench.json"
else
  bj=$(ls -dt "$ROOT"/out/bench-"$arg"-*/bench.json 2>/dev/null | head -1 || true)
fi

if [ -z "${bj:-}" ] || [ ! -f "$bj" ]; then
  echo "no bench.json found (arg: '${arg:-<latest>}'); run ./bench.sh first" >&2
  exit 1
fi

echo "bench: $bj"
python3 - "$bj" <<'PY'
import json, sys

d = json.load(open(sys.argv[1]))
print("=" * 66)
print(f"{d.get('bench', '?')}   {d['ok']}/{d['total']} translated"
      f"   wall {d.get('wall_s', '?')}s")
print("=" * 66)

P = F = S = clean = 0
for c in d["cases"]:
    vs = c.get("vectors") or []
    print(c["name"])
    if not vs:
        print("   (no vector verification)")
        continue
    for v in vs:
        if v.get("error"):
            print(f"   {v['stage']:<10} ERROR: {(v['error'] or '')[:55]}")
        else:
            bad = "" if v.get("build_ok") else "  build-fail"
            print(f"   {v['stage']:<10} {v['passed']}/{v['total']} pass, "
                  f"{v['failed']} fail, {v['skipped']} skip{bad}")
    last = vs[-1]  # summarize on the final verified stage
    if not last.get("error"):
        P += last.get("passed", 0)
        F += last.get("failed", 0)
        S += last.get("skipped", 0)
        if last.get("build_ok") and last.get("failed", 0) == 0:
            clean += 1

print("-" * 66)
rate = f"{100 * P / (P + F):.1f}%" if (P + F) else "n/a"
print(f"final-stage: {clean}/{d['total']} cases clean   "
      f"{P} pass, {F} fail, {S} skip ({rate})")
PY
