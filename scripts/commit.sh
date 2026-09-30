#!/usr/bin/env bash
# Kollektíva robot – mentés és feltöltés a Cloudflare build-keret figyelésével.
# Havi keret: BUILD_BUDGET (alap 470 a 500-ból), a hónap napjaira egyenletesen elosztva. Ha elfogyott a
# mai rész, a változás elmentődik [CF-Pages-Skip]-pel, és a következő szabad alkalommal kerül ki az oldalra.
# Két kirakás között legalább MIN_DEPLOY_GAP_MIN perc (alap 8), hogy a sok gombnyomás ne egyenként építsen.
set -u
LABEL="${1:-Robot}"
FLAG="data/deploy_pending"
git add -A public/ data/ 2>/dev/null || true

site_changed=0
if git diff --cached --name-only | grep -qv '^data/'; then site_changed=1; fi

deploy=0
if [ "$site_changed" = 1 ] || [ -f "$FLAG" ]; then
  month_start=$(date -u +%Y-%m-01)
  day=$(date -u +%-d)
  dim=$(date -u -d "$month_start +1 month -1 day" +%-d)
  used=$(git log HEAD --since="${month_start}T00:00:00Z" --format=%s | grep -vc 'CF-Pages-Skip' || true)
  allowed=$(( ${BUILD_BUDGET:-470} * day / dim ))
  last=$(git log HEAD -1 --format=%ct --invert-grep --grep='CF-Pages-Skip' 2>/dev/null || echo 0)
  gap=$(( $(date +%s) - ${last:-0} ))
  if [ "$gap" -lt $(( ${MIN_DEPLOY_GAP_MIN:-8} * 60 )) ]; then
    echo "Az előző kirakás óta csak ${gap} mp telt el – ez a változás a következő körrel kerül ki."; touch "$FLAG"
  elif [ "${used:-0}" -lt "$allowed" ]; then
    deploy=1; rm -f "$FLAG"
  else
    echo "Build-keret: ${used}/${allowed} – a változás később kerül ki."; touch "$FLAG"
  fi
  git add -A data/
fi

git diff --cached --quiet && exit 0
MSG="$LABEL: $(TZ=Europe/Budapest date '+%F %H:%M')"
[ "$deploy" = 1 ] || MSG="$MSG [CF-Pages-Skip]"
git commit -q -m "$MSG"
for i in 1 2 3; do
  if git pull -q --rebase origin main && git push -q; then exit 0; fi
  git rebase --abort 2>/dev/null || true
  sleep 5
done
echo "A feltöltés nem sikerült." >&2
exit 1
