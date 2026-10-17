#!/usr/bin/env bash
# Acceptance smoke test against a running stack (after make up).
# Usage: scripts/smoke.sh [base_url]. COMPOSE selects the compose command (default "docker compose").
# SMOKE_LOCAL=1 runs app commands on this machine instead of in the web container (cloud sessions);
# then SMOKE_WORKER_LOG names the worker's log file and SQL runs through psql "$DATABASE_URL".
# Each phase appends the checks for its acceptance criteria.
set -euo pipefail

BASE="${1:-http://localhost:8000}"
read -ra compose <<<"${COMPOSE:-docker compose}"
LOCAL="${SMOKE_LOCAL:-0}"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# Run a command where the app runs (web container, or locally with SMOKE_LOCAL=1).
in_app() {
    if [[ "$LOCAL" == 1 ]]; then
        "$@"
    else
        "${compose[@]}" exec -T web "$@"
    fi
}

worker_log() {
    if [[ "$LOCAL" == 1 ]]; then
        cat "${SMOKE_WORKER_LOG:?set SMOKE_WORKER_LOG with SMOKE_LOCAL=1}"
    else
        "${compose[@]}" logs --no-color worker 2>/dev/null
    fi
}

# Run SQL on the app database (db container, or DATABASE_URL with SMOKE_LOCAL=1).
sql() {
    if [[ "$LOCAL" == 1 ]]; then
        psql "${DATABASE_URL:?set DATABASE_URL with SMOKE_LOCAL=1}" -v ON_ERROR_STOP=1 -qAt -c "$1"
    else
        "${compose[@]}" exec -T db sh -c \
            'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -qAt -c "$1"' sh "$1"
    fi
}

# json_get <json> <python expression on d>: tiny JSON accessor (python3 is on the CI runner).
json_get() {
    python3 -c 'import json, sys; d = json.loads(sys.argv[1]); print(eval(sys.argv[2]))' "$1" "$2"
}

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

for _ in $(seq 1 60); do
    curl -fsS "$BASE/healthz" >/dev/null 2>&1 && break
    sleep 1
done

# Phase 0: health, dashboard navigation, ping job completed by the worker.
health=$(curl -fsS "$BASE/healthz") || fail "GET /healthz"
[[ "$health" == '{"status":"ok","database":"ok"}' ]] || fail "healthz returned $health"
echo "PASS healthz: $health"

page=$(curl -fsS "$BASE/") || fail "GET /"
grep -q "<h1>Dashboard</h1>" <<<"$page" || fail "dashboard heading missing"
for path in / /upload /review /library /search /tags /sources /jobs /settings; do
    grep -q "href=\"$path\"" <<<"$page" || fail "nav link $path missing"
done
echo "PASS dashboard with navigation"

reply=$(curl -fsS -X POST "$BASE/api/debug/ping") || fail "POST /api/debug/ping"
job_id=$(sed -nE 's/.*"job_id":([0-9]+).*/\1/p' <<<"$reply")
[[ -n "$job_id" ]] || fail "ping returned $reply"
done_line=""
for _ in $(seq 1 30); do
    done_line=$(worker_log | grep -m1 "Job ping\[$job_id\].*ended with status: Success" || true)
    [[ -n "$done_line" ]] && break
    sleep 1
done
[[ -n "$done_line" ]] || fail "worker did not complete ping job $job_id"
echo "PASS ping job $job_id completed: ${done_line#*| }"

# Phase 1: upload, duplicates, review grid, dashboard counts, video streaming, CLI import.
# Media are generated where the app runs (it has Pillow and ffmpeg) and copied here.
if [[ "$LOCAL" == 1 ]]; then
    media="$TMP/media"
    python scripts/smoke_media.py "$media"
else
    media_in_app="/tmp/smoke-media-$$"
    in_app python scripts/smoke_media.py "$media_in_app"
    "${compose[@]}" cp "web:$media_in_app" "$TMP/media" >/dev/null
    media="$TMP/media"
fi
upload_args=()
for f in "$media"/upload/*; do upload_args+=(-F "files=@$f"); done

count_of() { # count_of <status>: number on the dashboard
    curl -fsS "$BASE/" | sed -nE "s/.*id=\"count-$1\">([0-9]+)<.*/\1/p"
}

files_total() { # sum of all status counts on the dashboard
    curl -fsS "$BASE/" | grep -oE 'id="count-[a-z_]+">[0-9]+<' | grep -oE '[0-9]+' |
        awk '{ s += $1 } END { print s + 0 }'
}

wait_batch() { # wait_batch <batch_id>: print the final batch JSON
    local body=""
    for _ in $(seq 1 90); do
        body=$(curl -fsS "$BASE/api/uploads/$1") || fail "GET /api/uploads/$1"
        [[ $(json_get "$body" 'd["done"]') == True ]] && { echo "$body"; return; }
        sleep 1
    done
    fail "batch $1 did not finish: $body"
}

pending_before=$(count_of pending_review)
total_before=$(files_total)
reply=$(curl -fsS "${upload_args[@]}" -F "tags=smoke, 스모크" "$BASE/api/uploads") ||
    fail "POST /api/uploads"
batch=$(json_get "$reply" 'd["batch_id"]')
status=$(wait_batch "$batch")
[[ $(json_get "$status" 'd["counts"]["new"]') == 4 ]] || fail "first upload: $status"
echo "PASS upload of 3 images + 1 video: $(json_get "$status" 'd["counts"]')"

review=$(curl -fsS "$BASE/review") || fail "GET /review"
for id in $(json_get "$status" '" ".join(str(f["file_id"]) for f in d["files"])'); do
    grep -q "src=\"/media/$id/thumb\"" <<<"$review" || fail "file $id missing from /review"
    code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/media/$id/thumb")
    [[ "$code" == 200 ]] || fail "thumbnail of file $id returned $code"
    file_status=$(curl -fsS "$BASE/files/$id" | sed -nE 's/.*id="file-status">([^<]+)<.*/\1/p')
    [[ "$file_status" == "pending review" ]] || fail "file $id status: $file_status"
done
pending_after=$(count_of pending_review)
[[ $((pending_after - pending_before)) == 4 ]] ||
    fail "dashboard pending_review went $pending_before -> $pending_after"
echo "PASS 4 files pending_review with thumbnails on /review; dashboard pending_review $pending_before -> $pending_after"

reply=$(curl -fsS "${upload_args[@]}" "$BASE/api/uploads") || fail "POST /api/uploads (again)"
status2=$(wait_batch "$(json_get "$reply" 'd["batch_id"]')")
[[ $(json_get "$status2" 'd["counts"]["duplicate_exact"]') == 4 ]] || fail "re-upload: $status2"
[[ $(json_get "$status2" 'd["counts"]["new"]') == 0 ]] || fail "re-upload: $status2"
total_after=$(files_total)
[[ $((total_after - total_before)) == 4 ]] || fail "files total went $total_before -> $total_after"
echo "PASS re-upload: 4 duplicate_exact, 0 new; files total $total_before -> $total_after"

video_id=$(json_get "$status" 'd["files"][-1]["file_id"]')
grep -q "<video src=\"/media/$video_id/original\" controls" <<<"$(curl -fsS "$BASE/files/$video_id")" ||
    fail "/files/$video_id has no video element"
size=$(stat -c %s "$media/upload/video.mp4")
headers=$(curl -fsS -D - -o "$TMP/part" -H "Range: bytes=1000-1999" "$BASE/media/$video_id/original")
grep -qi "^HTTP/[0-9.]* 206" <<<"$headers" || fail "range request: $headers"
grep -qi "^content-range: bytes 1000-1999/$size" <<<"$headers" || fail "content-range: $headers"
cmp -s "$TMP/part" <(tail -c +1001 "$media/upload/video.mp4" | head -c 1000) ||
    fail "range body differs"
grep -qi "^content-type: video/mp4" <<<"$headers" || fail "content-type: $headers"
echo "PASS /files/$video_id has a <video> element; Range bytes=1000-1999 -> 206, content-range bytes 1000-1999/$size"

if [[ "$LOCAL" == 1 ]]; then
    import_dir="$media/import"
else
    import_dir="$media_in_app/import"
fi
import_out=$(in_app python -m app.cli import "$import_dir" --recursive --tags imported) ||
    fail "app.cli import: $import_out"
import_batch=$(sed -nE 's/^batch ([0-9a-f]{32}): queued ([0-9]+) file.*/\1/p' <<<"$import_out")
queued=$(sed -nE 's/^batch [0-9a-f]{32}: queued ([0-9]+) file.*/\1/p' <<<"$import_out")
expected=$(find "$media/import" -type f | wc -l)
[[ -n "$import_batch" && "$queued" == "$expected" ]] || fail "import queued: $import_out"
status3=$(wait_batch "$import_batch")
[[ $(json_get "$status3" 'd["counts"]["new"]') == "$expected" ]] || fail "import: $status3"
kinds=$(for id in $(json_get "$status3" '" ".join(str(f["file_id"]) for f in d["files"])'); do
    curl -fsS "$BASE/files/$id" | sed -nE 's/.*<dt>Type<\/dt><dd>([a-z]+) .*/\1/p'
done | sort | uniq -c | tr -s ' ' | paste -sd, -)
echo "PASS app.cli import: $expected files queued, $expected new (kinds:$kinds)"

# Phase 2: perceptual duplicates, review badges, discarded matches, rehash and recompute.
upload_one() { # upload_one <file>: upload, wait, print the batch JSON
    local reply
    reply=$(curl -fsS -F "files=@$1" "$BASE/api/uploads") || fail "POST /api/uploads $1"
    wait_batch "$(json_get "$reply" 'd["batch_id"]')"
}

review_tile() { # review_tile <file_id>: the /review grid tile of one file
    curl -fsS "$BASE/review" | python3 -c '
import sys
html, marker = sys.stdin.read(), f"data-file-id=\"{sys.argv[1]}\""
start = html.find(marker)
print(html[start : html.index("</li>", start)] if start >= 0 else "")' "$1"
}

similar=$media/similar
status=$(upload_one "$similar/original.png")
[[ $(json_get "$status" 'd["counts"]["new"]') == 1 ]] || fail "original upload: $status"
first_id=$(json_get "$status" 'd["files"][0]["file_id"]')
status=$(upload_one "$similar/half.png")
[[ $(json_get "$status" 'd["counts"]["new"]') == 1 ]] || fail "resized upload: $status"
half_id=$(json_get "$status" 'd["files"][0]["file_id"]')
tile=$(review_tile "$half_id")
grep -q "similar to archived or pending (1)" <<<"$tile" || fail "no similar badge on $half_id: $tile"
grep -q "href=\"/files/$first_id\"" <<<"$tile" || fail "badge of $half_id does not link to $first_id"
similar_json=$(curl -fsS "$BASE/api/files/$half_id/similar") || fail "GET similar of $half_id"
[[ $(json_get "$similar_json" '[s["file_id"] for s in d["similar"]]') == "[$first_id]" ]] ||
    fail "similar of $half_id: $similar_json"
echo "PASS 50 percent copy $half_id shows 'similar to archived or pending (1)' linking to /files/$first_id; API: $(json_get "$similar_json" 'd["similar"][0]')"

sql "UPDATE files SET status = 'discarded', discarded_at = now() WHERE id = $first_id" ||
    fail "discard $first_id"
status=$(upload_one "$similar/reencoded.jpg")
[[ $(json_get "$status" 'd["counts"]["new"]') == 1 ]] || fail "re-encoded upload: $status"
again_id=$(json_get "$status" 'd["files"][0]["file_id"]')
tile=$(review_tile "$again_id")
grep -q 'class="badge badge-discarded"' <<<"$tile" || fail "no discarded badge on $again_id: $tile"
grep -q "similar to discarded (1)" <<<"$tile" || fail "discarded badge text on $again_id: $tile"
grep -q "href=\"/files/$first_id\"" <<<"$tile" || fail "discarded badge does not link to $first_id"
grep -q "Similar files" <<<"$(curl -fsS "$BASE/files/$again_id")" || fail "file page section"
echo "PASS after discarding $first_id (psql), re-encoded copy $again_id shows 'similar to discarded (1)'"

copy_args=(-F "files=@$similar/original.png" -F "files=@$similar/half.png" -F "files=@$similar/reencoded.jpg")
reply=$(curl -fsS "${copy_args[@]}" "$BASE/api/uploads") || fail "POST exact copies"
status=$(wait_batch "$(json_get "$reply" 'd["batch_id"]')")
[[ $(json_get "$status" 'd["counts"]["duplicate_exact"]') == 3 ]] || fail "exact copies: $status"
[[ $(json_get "$status" 'd["counts"]["new"]') == 0 ]] || fail "exact copies: $status"
echo "PASS exact copies of all three: 3 duplicate_exact, 0 new"

rehash_out=$(in_app python -m app.cli rehash --missing-only) || fail "rehash --missing-only: $rehash_out"
grep -q "errors 0" <<<"$rehash_out" || fail "rehash: $rehash_out"
recompute_out=$(in_app python -m app.cli recompute-similar) || fail "recompute-similar: $recompute_out"
pairs_now=$(sql "SELECT count(*) FROM similar_pairs WHERE $first_id IN (file_a_id, file_b_id)")
[[ "$pairs_now" == 2 ]] || fail "pairs of $first_id after recompute: $pairs_now"
echo "PASS $rehash_out; $recompute_out"

echo "All smoke checks passed."
