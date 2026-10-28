#!/bin/zsh
# Counts-only inventory of the sealed evaluator account. Run as fpeval; prints JSON on stdout.
# Reports existence, counts and commit hashes only: no file names, family names or contents.
emulate -L zsh
setopt null_glob
home=$HOME
print -r -- "{"
print -r -- "  \"user\": \"$(id -un)\","
print -r -- "  \"home_mode\": \"$(stat -f %Lp "$home")\","
pythons=($home/**/bin/python3(N:a))
print -r -- "  \"venv_pythons\": ${#pythons},"
ok=0
for p in $pythons; do
  $p -c 'import cv2, numpy, shapely, ocrmac, floor_interior_design_and_renderer' 2>/dev/null && ok=$((ok + 1))
done
print -r -- "  \"venvs_importing_parser_and_ocr\": $ok,"
repos=($home/**/.git(N/:h))
print -r -- "  \"git_checkouts\": ${#repos},"
print -r -- "  \"checkout_heads\": ["
i=0
for r in $repos; do
  i=$((i + 1))
  head=$(git -C "$r" rev-parse HEAD 2>/dev/null || print unknown)
  dirty=$(git -C "$r" status --porcelain 2>/dev/null | wc -l | tr -d ' ')
  sep=","; (( i == ${#repos} )) && sep=""
  print -r -- "    {\"head\": \"$head\", \"dirty_files\": $dirty}$sep"
done
print -r -- "  ],"
manifests=($home/**/manifest*.csv(N))
sealed_rows=0
for m in $manifests; do
  n=$(awk -F, 'NR==1{for(i=1;i<=NF;i++) if($i=="split") c=i; next} c && $c=="sealed"{n++} END{print n+0}' "$m")
  sealed_rows=$((sealed_rows + n))
done
print -r -- "  \"manifest_files\": ${#manifests},"
print -r -- "  \"manifest_sealed_rows\": $sealed_rows,"
gold=($home/**/gold/*/plan.json(N))
print -r -- "  \"gold_plan_files\": ${#gold},"
images=($home/**/*.(png|jpg|jpeg|PNG|JPG|JPEG)(N))
print -r -- "  \"image_files\": ${#images},"
criteria=($home/**/poc6-criteria.toml(N))
print -r -- "  \"criteria_files\": ${#criteria},"
print -r -- "  \"criteria_sha256\": ["
i=0
for c in $criteria; do
  i=$((i + 1)); sep=","; (( i == ${#criteria} )) && sep=""
  print -r -- "    \"$(shasum -a 256 "$c" | cut -d' ' -f1)\"$sep"
done
print -r -- "  ]"
print -r -- "}"
