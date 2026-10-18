#!/usr/bin/env bash
# Acceptance smoke test against a running stack (after make up).
# Usage: scripts/smoke.sh [base_url]. COMPOSE selects the compose command (default "docker compose").
# SMOKE_LOCAL=1 runs app commands on this machine instead of in the web container (cloud sessions);
# then SMOKE_WORKER_LOG names the worker's log file and SQL runs through psql "$DATABASE_URL",
# the phase 6 media server runs here too, and SMOKE_RESTART_WORKER and SMOKE_RESTART_WEB are
# commands that restart the worker and the web server (phase 8). Phase 8 recreates the database volume at the end (make restore check);
# SMOKE_SKIP_RESTORE=1 skips that step.
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

# Unsafe methods need a same-origin Origin or Referer header (phase 8). Browsers send one; curl
# does not, so every request here carries the Origin of BASE.
curl() {
    command curl -H "Origin: $BASE" "$@"
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
for _ in $(seq 1 180); do  # the worker installs the crawler tools before it starts
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

batch_ids=$(json_get "$status" '",".join(str(f["file_id"]) for f in d["files"])')
review=$(curl -fsS "$BASE/review/fragments/grid?ids=$batch_ids") || fail "GET review grid"
for id in ${batch_ids//,/ }; do
    grep -q "src=\"/media/$id/thumb\"" <<<"$review" || fail "file $id missing from /review"
    code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/media/$id/thumb")
    [[ "$code" == 200 ]] || fail "thumbnail of file $id returned $code"
    file_status=$(curl -fsS "$BASE/files/$id" | sed -nE 's/.*id="file-status">([^<]+)<.*/\1/p')
    [[ "$file_status" == "pending review" ]] || fail "file $id status: $file_status"
done
pending_after=$(count_of pending_review)
[[ $((pending_after - pending_before)) == 4 ]] ||
    fail "dashboard pending_review went $pending_before -> $pending_after"
echo "PASS 4 files pending_review with thumbnails in the review grid; dashboard pending_review $pending_before -> $pending_after"

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

review_tile() { # review_tile <file_id>: the review grid tile of one file
    curl -fsS "$BASE/review/fragments/grid?ids=$1" | python3 -c '
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

reply=$(curl -fsS -H 'Content-Type: application/json' -d "{\"file_ids\": [$first_id]}" \
    "$BASE/api/review/discard") || fail "discard $first_id"
[[ $(json_get "$reply" 'd["ok"]') == True ]] || fail "discard $first_id: $reply"
status=$(upload_one "$similar/reencoded.jpg")
[[ $(json_get "$status" 'd["counts"]["new"]') == 1 ]] || fail "re-encoded upload: $status"
again_id=$(json_get "$status" 'd["files"][0]["file_id"]')
tile=$(review_tile "$again_id")
grep -q 'class="badge badge-discarded"' <<<"$tile" || fail "no discarded badge on $again_id: $tile"
grep -q "similar to discarded (1)" <<<"$tile" || fail "discarded badge text on $again_id: $tile"
grep -q "href=\"/files/$first_id\"" <<<"$tile" || fail "discarded badge does not link to $first_id"
grep -q "Similar files" <<<"$(curl -fsS "$BASE/files/$again_id")" || fail "file page section"
echo "PASS after discarding $first_id, re-encoded copy $again_id shows 'similar to discarded (1)'"

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

# Phase 3: review queue, archive and discard, undo, compare, trash purge.
post_json() { # post_json <path> <json>: POST JSON, print the response body
    curl -fsS -H 'Content-Type: application/json' -d "$2" "$BASE$1" || fail "POST $1 $2"
}

data_dir=$(in_app python -c 'from app.config import get_settings; print(get_settings().data_dir)')

where_is() { # where_is <file_id>: staging, archive, trash or missing (the original on disk)
    local row sha ext staging
    row=$(sql "SELECT encode(sha256, 'hex'), coalesce(ext, ''), coalesce(staging_path, '') FROM files WHERE id = $1")
    IFS='|' read -r sha ext staging <<<"$row"
    local suffix=${ext:+.$ext}
    if [[ -n "$staging" ]] && in_app test -f "$data_dir/$staging"; then
        echo staging
    elif in_app test -f "$data_dir/archive/${sha:0:2}/${sha:2:2}/$sha$suffix"; then
        echo archive
    elif in_app test -f "$data_dir/trash/$sha$suffix"; then
        echo trash
    else
        echo missing
    fi
}

grep -q "js/review.js" <<<"$(curl -fsS "$BASE/review")" || fail "/review does not load review.js"
review_args=()
for f in "$media"/review/*; do review_args+=(-F "files=@$f"); done
reply=$(curl -fsS "${review_args[@]}" "$BASE/api/uploads") || fail "POST 20 review files"
review_batch=$(json_get "$reply" 'd["batch_id"]')
status=$(wait_batch "$review_batch")
[[ $(json_get "$status" 'd["counts"]["new"]') == 20 ]] || fail "20 review files: $status"
queue=$(curl -fsS "$BASE/api/review/queue?batch=$review_batch&limit=7")
[[ $(json_get "$queue" 'd["total"]') == 20 ]] || fail "queue total: $queue"
queue_ids=$(json_get "$queue" '" ".join(str(i["id"]) for i in d["items"])')
cursor=$(json_get "$queue" 'd["next_cursor"]')
while [[ "$cursor" != None ]]; do
    page=$(curl -fsS "$BASE/api/review/queue?batch=$review_batch&limit=7&cursor=$cursor")
    queue_ids="$queue_ids $(json_get "$page" '" ".join(str(i["id"]) for i in d["items"])')"
    cursor=$(json_get "$page" 'd["next_cursor"]')
done
read -ra queue_ids <<<"$queue_ids"
[[ ${#queue_ids[@]} == 20 ]] || fail "queue pages returned ${#queue_ids[@]} ids"
echo "PASS review queue of batch $review_batch: 20 files over 3 cursor pages (limit 7)"

# The page's A and D keys call these endpoints, one file per key press.
inbox_id=$(sql "SELECT id FROM folders WHERE parent_id IS NULL AND name = 'Inbox'")
i=0
for id in "${queue_ids[@]}"; do
    if ((i % 2 == 0)); then action=archive; else action=discard; fi
    reply=$(post_json "/api/review/$action" "{\"file_ids\": [$id]}")
    [[ $(json_get "$reply" 'd["ok"]') == True ]] || fail "$action $id: $reply"
    last_actions=$(json_get "$reply" 'd["action_ids"]')
    i=$((i + 1))
done
i=0
for id in "${queue_ids[@]}"; do
    row=$(sql "SELECT status, coalesce(folder_id::text, '') FROM files WHERE id = $id")
    loc=$(where_is "$id")
    if ((i % 2 == 0)); then
        [[ "$row" == "archived|$inbox_id" && "$loc" == archive ]] || fail "file $id: $row, $loc"
    else
        [[ "$row" == "discarded|" && "$loc" == trash ]] || fail "file $id: $row, $loc"
    fi
    i=$((i + 1))
done
left=$(curl -fsS "$BASE/api/review/queue?batch=$review_batch")
[[ $(json_get "$left" 'd["total"]') == 0 ]] || fail "queue not empty: $left"
echo "PASS 20 decisions: 10 archived into Inbox (originals under archive/), 10 discarded (under trash/), queue empty"

last_id=${queue_ids[19]}
reply=$(post_json /api/review/undo "{\"action_ids\": $last_actions}")
[[ $(json_get "$reply" 'd["results"][0]["status"]') == pending_review ]] || fail "undo: $reply"
[[ $(where_is "$last_id") == staging ]] || fail "undo: file $last_id is in $(where_is "$last_id")"
again=$(post_json /api/review/undo "{\"action_ids\": $last_actions}")
[[ $(json_get "$again" 'd["ok"]') == False ]] || fail "second undo: $again"
reply=$(post_json /api/review/archive "{\"file_ids\": [$last_id]}")
[[ $(where_is "$last_id") == archive ]] || fail "re-archive $last_id"
echo "PASS undo of the last discard moved file $last_id back to staging (pending_review); a second undo is refused: $(json_get "$again" 'd["results"][0]["error"]')"

pair=$media/review_pair
status=$(upload_one "$pair/original.png")
orig_id=$(json_get "$status" 'd["files"][0]["file_id"]')
status=$(upload_one "$pair/resized.png")
resized_id=$(json_get "$status" 'd["files"][0]["file_id"]')
compare=$(curl -fsS "$BASE/review/fragments/compare?left=$resized_id&right=$orig_id") ||
    fail "compare fragment"
grep -q "data-left-id=\"$resized_id\" data-right-id=\"$orig_id\"" <<<"$compare" || fail "compare ids"
grep -q "class=\"compare-side larger\" data-file-id=\"$orig_id\"" <<<"$compare" ||
    fail "the original is not highlighted as larger"
grep -q '320 &times; 240' <<<"$compare" && grep -q '640 &times; 480' <<<"$compare" ||
    fail "compare dimensions"
reply=$(post_json /api/review/compare \
    "{\"file_id\": $resized_id, \"other_id\": $orig_id, \"choice\": \"keep_left\"}")
[[ $(where_is "$resized_id") == archive && $(where_is "$orig_id") == trash ]] ||
    fail "keep left, discard right: $reply"
echo "PASS compare shows resized $resized_id beside original $orig_id (larger highlighted); keep left, discard right: $(json_get "$reply" '[(r["file_id"], r["status"]) for r in d["results"]]')"

status=$(upload_one "$pair/original.png")
[[ $(json_get "$status" 'd["counts"]["duplicate_exact"]') == 1 ]] || fail "copy of discarded: $status"
status=$(upload_one "$pair/variant.jpg")
variant_id=$(json_get "$status" 'd["files"][0]["file_id"]')
single=$(curl -fsS "$BASE/review/fragments/single?file_id=$variant_id")
strip=$(python3 -c 'import sys; h = sys.stdin.read(); print(h[h.find("similar-strip"):])' <<<"$single")
grep -q "data-other-id=\"$orig_id\" data-other-status=\"discarded\"" <<<"$strip" ||
    fail "similar strip of $variant_id lacks discarded $orig_id"
echo "PASS exact copy of discarded $orig_id dropped (duplicate_exact); variant $variant_id shows it in the similar strip as discarded"

reply=$(post_json /api/review/discard "{\"file_ids\": [$variant_id]}")
discard_actions=$(json_get "$reply" 'd["action_ids"]')
sql "UPDATE files SET discarded_at = now() - interval '30 days' WHERE id = $variant_id" >/dev/null
purge_out=$(in_app python -m app.cli purge-trash) || fail "purge-trash: $purge_out"
[[ $(where_is "$variant_id") == missing ]] || fail "purge left $variant_id in $(where_is "$variant_id")"
[[ $(where_is "$orig_id") == trash ]] || fail "purge removed the recent discard $orig_id"
reply=$(post_json /api/review/undo "{\"action_ids\": $discard_actions}")
grep -q "no longer in the trash" <<<"$reply" || fail "undo after purge: $reply"
echo "PASS $purge_out; undo after purge refused: $(json_get "$reply" 'd["results"][0]["error"]')"

# Phase 4: archive panel call, library tree counts and moves, folder delete, tag merge,
# removed crawler tags surviving a duplicate ingest.
patch_json() { # patch_json <path> <json>
    curl -fsS -X PATCH -H 'Content-Type: application/json' -d "$2" "$BASE$1" || fail "PATCH $1 $2"
}

folder_counts() { # folder_counts <folder_id>: "direct subtree" from GET /api/folders
    curl -fsS "$BASE/api/folders" | python3 -c '
import json, sys
def walk(nodes):
    for n in nodes:
        if n["id"] == int(sys.argv[1]):
            print(n["direct_count"], n["subtree_count"])
        walk(n["children"])
walk(json.load(sys.stdin)["folders"])' "$1"
}

tag_state() { # tag_state <file_id>: "active=name:origin,... removed=name,..." from the API
    json_get "$(curl -fsS "$BASE/api/files/$1/tags")" \
        '"active=" + ",".join(t["name"] + ":" + t["origin"] for t in d["active"]) + " removed=" + ",".join(t["name"] for t in d["removed"])'
}

for path in /library /tags; do
    page=$(curl -fsS "$BASE$path") || fail "GET $path"
    grep -q "Coming in phase" <<<"$page" && fail "$path is still a placeholder"
done
grep -q 'id="archive-panel"' <<<"$(curl -fsS "$BASE/review")" || fail "/review has no archive panel"

if [[ "$LOCAL" == 1 ]]; then crawled="$media/library/crawled.png"; else crawled="$media_in_app/library/crawled.png"; fi
run_tag=$RANDOM$RANDOM
crawl_batch=$(in_app python scripts/smoke_crawler_ingest.py "$crawled" smoke_addon "post-$run_tag-1" \
    "Sunset,Beach,Sky") || fail "crawler ingest"
status=$(wait_batch "$crawl_batch")
[[ $(json_get "$status" 'd["counts"]["new"]') == 1 ]] || fail "crawler ingest: $status"
crawled_id=$(json_get "$status" 'd["files"][0]["file_id"]')
travel=$(json_get "$(post_json /api/folders "{\"name\": \"Travel $run_tag\"}")" 'd["id"]')
coast=$(json_get "$(post_json /api/folders "{\"name\": \"Coast\", \"parent_id\": $travel}")" 'd["id"]')
reply=$(post_json /api/review/archive "{\"file_ids\": [$crawled_id], \"folder_id\": $coast,
    \"add_tags\": [\"Favourite\"], \"remove_tags_per_file\": {\"$crawled_id\": [\"beach\"]}}")
[[ $(json_get "$reply" 'd["ok"]') == True ]] || fail "archive with panel options: $reply"
row=$(sql "SELECT status, folder_id FROM files WHERE id = $crawled_id")
state=$(tag_state "$crawled_id")
[[ "$row" == "archived|$coast" ]] || fail "archived file $crawled_id: $row"
[[ "$state" == "active=favourite:user,sky:crawler,sunset:crawler removed=beach" ]] ||
    fail "tags of $crawled_id: $state"
[[ $(where_is "$crawled_id") == archive ]] || fail "original of $crawled_id not under archive/"
echo "PASS archive panel call: file $crawled_id archived into Travel $run_tag / Coast with $state"

dup_batch=$(in_app python scripts/smoke_crawler_ingest.py "$crawled" smoke_addon "post-$run_tag-2" \
    "beach,sunset,Night") || fail "duplicate crawler ingest"
status=$(wait_batch "$dup_batch")
[[ $(json_get "$status" 'd["counts"]["duplicate_exact"]') == 1 ]] || fail "duplicate ingest: $status"
state=$(tag_state "$crawled_id")
[[ "$state" == "active=favourite:user,night:crawler,sky:crawler,sunset:crawler removed=beach" ]] ||
    fail "after duplicate ingest: $state"
echo "PASS duplicate ingest from the same add-on: 'beach' stays removed, new crawler tag added ($state)"

box_files=$(curl -fsS "$BASE/api/library?folder=$inbox_id&per_page=10")
move_ids=$(json_get "$box_files" '",".join(str(i["id"]) for i in d["items"])')
[[ $(json_get "$box_files" 'len(d["items"])') == 10 ]] || fail "need 10 archived files in the Inbox"
read -r box_before _ <<<"$(folder_counts "$inbox_id")"
mountains=$(json_get "$(post_json /api/folders "{\"name\": \"Mountains\", \"parent_id\": $travel}")" 'd["id"]')
reply=$(post_json /api/library/move "{\"file_ids\": [$move_ids], \"folder_id\": $mountains}")
[[ $(json_get "$reply" 'd["ok"]') == True ]] || fail "move 10 files: $reply"
read -r box_after _ <<<"$(folder_counts "$inbox_id")"
[[ "$(folder_counts "$mountains")" == "10 10" && "$(folder_counts "$travel")" == "0 11" ]] ||
    fail "counts after move: mountains $(folder_counts "$mountains"), travel $(folder_counts "$travel")"
[[ $((box_before - box_after)) == 10 ]] || fail "Inbox count went $box_before -> $box_after"
first_moved=${move_ids%%,*}
[[ $(where_is "$first_moved") == archive ]] || fail "moved file $first_moved left archive/"
tree=$(curl -fsS "$BASE/library?folder=$mountains")
grep -qE "href=\"/library\?folder=$travel\"[^>]*>Travel $run_tag <span class=\"folder-count\"[^>]*>0 / 11<" <<<"$tree" ||
    fail "library tree does not show Travel $run_tag 0 / 11"
[[ $(grep -c 'class="tile library-tile"' <<<"$tree") == 10 ]] || fail "library grid of Mountains"
echo "PASS moved 10 files Inbox -> Travel $run_tag / Mountains: Inbox $box_before -> $box_after, Mountains $(folder_counts "$mountains"), Travel $(folder_counts "$travel") (direct subtree); originals stay under archive/"

# Deleting Travel moves Coast and Mountains to the top level; a top-level folder with the same
# name as Mountains already exists, so Mountains is merged into it (with its own subfolder).
ridge=$(json_get "$(post_json /api/folders "{\"name\": \"Ridge\", \"parent_id\": $mountains}")" 'd["id"]')
post_json /api/library/move "{\"file_ids\": [$first_moved], \"folder_id\": $ridge}" >/dev/null
top_mountains=$(json_get "$(post_json /api/folders "{\"name\": \"Mountains $run_tag\"}")" 'd["id"]')
patch_json "/api/folders/$mountains" "{\"name\": \"mountains $run_tag\"}" >/dev/null
patch_json "/api/folders/$coast" "{\"name\": \"Coast $run_tag\"}" >/dev/null
code=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE "$BASE/api/folders/$travel")
[[ "$code" == 409 ]] || fail "deleting a non-empty folder without moving returned $code"
reply=$(curl -fsS -X DELETE "$BASE/api/folders/$travel?move_contents_to_parent=true") ||
    fail "delete Travel $run_tag"
[[ $(json_get "$reply" '(d["folders_moved"], d["folders_merged"])') == "(2, 1)" ]] ||
    fail "delete Travel $run_tag: $reply"
orphans=$(sql "SELECT count(*) FROM files WHERE status = 'archived' AND (folder_id IS NULL OR folder_id NOT IN (SELECT id FROM folders))")
[[ "$orphans" == 0 ]] || fail "$orphans archived files without a folder"
[[ "$(folder_counts "$top_mountains")" == "9 10" ]] ||
    fail "merged Mountains $run_tag: $(folder_counts "$top_mountains")"
[[ $(sql "SELECT parent_id FROM folders WHERE id = $ridge") == "$top_mountains" ]] ||
    fail "Ridge was not moved into the merged folder"
[[ $(sql "SELECT folder_id FROM files WHERE id = $crawled_id") == "$coast" ]] ||
    fail "file $crawled_id left Coast $run_tag"
echo "PASS deleting Travel $run_tag with move_contents_to_parent: $(json_get "$reply" 'd'); Mountains merged into the top-level one ($(folder_counts "$top_mountains") direct subtree); archived files without a folder: $orphans"

cat_ids=$(json_get "$(curl -fsS "$BASE/api/library?per_page=3")" '",".join(str(i["id"]) for i in d["items"])')
IFS=, read -r t1 t2 t3 <<<"$cat_ids"
post_json /api/library/tags/add "{\"file_ids\": [$t1, $t2], \"tags\": [\"cats\"]}" >/dev/null
post_json /api/library/tags/add "{\"file_ids\": [$t2, $t3], \"tags\": [\"cat\"]}" >/dev/null
cats_id=$(sql "SELECT id FROM tags WHERE name = 'cats'")
cat_id=$(sql "SELECT id FROM tags WHERE name = 'cat'")
expected=$(sql "SELECT count(DISTINCT file_id) FROM file_tags WHERE tag_id IN ($cats_id, $cat_id) AND NOT removed")
code=$(curl -s -o "$TMP/rename" -w '%{http_code}' -H 'Content-Type: application/json' \
    -d '{"name": "cat"}' "$BASE/api/tags/$cats_id/rename")
[[ "$code" == 409 ]] && grep -q '"merge_into"' "$TMP/rename" || fail "rename warning: $code $(cat "$TMP/rename")"
reply=$(post_json "/api/tags/$cats_id/rename" '{"name": "cat", "merge": true}')
[[ $(json_get "$reply" 'd["merged_from"]') == cats && $(json_get "$reply" 'd["tag"]["count"]') == "$expected" ]] ||
    fail "merge: $reply (expected $expected files)"
[[ $(sql "SELECT count(*) FROM tags WHERE name = 'cats'") == 0 ]] || fail "tag cats still exists"
grep -q 'data-tag-name="cat" data-count="'"$expected"'"' <<<"$(curl -fsS "$BASE/tags")" || fail "/tags count"
echo "PASS renaming 'cats' to existing 'cat' warns (409 merge_into), then merges: $(json_get "$reply" 'd["tag"]')"

# Phase 5: crawler add-on framework (demo add-on, run now, recrawl, schedule, cancel).
for path in /sources /jobs; do
    page=$(curl -fsS "$BASE$path") || fail "GET $path"
    grep -q "Coming in phase" <<<"$page" && fail "$path is still a placeholder"
done
grep -q '<option value="demo">' <<<"$(curl -fsS "$BASE/sources")" || fail "demo add-on not listed on /sources"
echo "PASS /sources lists the demo add-on (entry point)"

wait_run() { # wait_run <run_id> [seconds]: wait until the run ended and its files are ingested
    local body=""
    for _ in $(seq 1 "${2:-120}"); do
        body=$(curl -fsS "$BASE/api/jobs/$1") || fail "GET /api/jobs/$1"
        if [[ $(json_get "$body" 'd["status"] not in ("queued", "running") and d["stats"]["pending"] == 0') == True ]]; then
            echo "$body"
            return
        fi
        sleep 1
    done
    fail "run $1 did not finish: $body"
}

seed=$(date +%s)
smoke_folder=$(json_get "$(post_json /api/folders "{\"name\": \"Crawled $run_tag\"}")" 'd["id"]')
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/sources/new?addon=demo" \
    -F "name=Demo $run_tag" -F "enabled=on" -F "default_folder_id=$smoke_folder" \
    -F "default_tags=Smoke Source" -F "cfg.count=10" -F "cfg.seed=$seed" \
    -F $'cfg.tags=Sunset\nbeach' -F "max_runtime_minutes=10")
[[ "$code" == 303 ]] || fail "create demo source through the form returned $code"
demo_id=$(json_get "$(curl -fsS "$BASE/api/sources")" \
    "[s['id'] for s in d['sources'] if s['name'] == 'Demo $run_tag'][0]")
location=$(curl -s -o /dev/null -w '%{redirect_url}' -X POST "$BASE/sources/$demo_id/run")
run1=${location##*/}
[[ "$run1" =~ ^[0-9]+$ ]] || fail "Run now redirected to '$location'"
run=$(wait_run "$run1")
[[ $(json_get "$run" '(d["status"], d["stats"]["emitted"], d["stats"]["new"])') == "('succeeded', 10, 10)" ]] ||
    fail "first demo run: $run"
pending=$(sql "SELECT count(*) FROM files f JOIN file_sources s ON s.file_id = f.id WHERE s.job_run_id = $run1 AND s.source_id = $demo_id AND f.status = 'pending_review'")
[[ "$pending" == 10 ]] || fail "$pending files of run $run1 in the review queue"
crawled_file=$(sql "SELECT file_id FROM file_sources WHERE job_run_id = $run1 ORDER BY id LIMIT 1")
state=$(tag_state "$crawled_file")
[[ "$state" == "active=beach:crawler,smoke_source:crawler,sunset:crawler removed=" ]] ||
    fail "tags of crawled file $crawled_file: $state"
in_queue=$(curl -fsS "$BASE/api/review/queue?source_key=demo&limit=500" | python3 -c '
import json, sys
items = [i for i in json.load(sys.stdin)["items"] if i["id"] == int(sys.argv[1])]
print(items[0]["default_folder_id"] if items else "missing")' "$crawled_file")
[[ "$in_queue" == "$smoke_folder" ]] || fail "review queue default folder of $crawled_file: $in_queue"
grep -q "emitted 10 images" <<<"$(curl -fsS "$BASE/jobs/$run1")" || fail "job page log tail of run $run1"
echo "PASS demo source (count 10) Run now: run $run1 $(json_get "$run" 'd["stats"]'); 10 files pending review with $state; archive panel default folder $smoke_folder"

total_before=$(files_total)
run2=$(json_get "$(post_json "/api/sources/$demo_id/run" '{}')" 'd["run_id"]')
run=$(wait_run "$run2")
[[ $(json_get "$run" '(d["status"], d["stats"]["skipped_known_remote"], d["stats"]["new"])') == "('succeeded', 10, 0)" ]] ||
    fail "second demo run: $run"
[[ $(files_total) == "$total_before" ]] || fail "the second run added files"
row=$(curl -fsS "$BASE/jobs?source_id=$demo_id" | python3 -c '
import sys
html, marker = sys.stdin.read(), f"data-run-id=\"{sys.argv[1]}\""
start = html.find(marker)
print(html[start : html.index("</tr>", start)] if start >= 0 else "")' "$run2")
grep -q 'data-stat="skipped_known_remote">10<' <<<"$row" || fail "jobs page does not show 10 skipped: $row"
echo "PASS second run $run2 adds nothing: $(json_get "$run" 'd["stats"]')"

# Scheduled every minute: wait for two runs, check they are about a minute apart and never overlap.
sched_id=$(json_get "$(post_json /api/sources "{\"name\": \"Every minute $run_tag\", \"addon_key\": \"demo\",
    \"schedule\": \"* * * * *\", \"config\": {\"count\": 2, \"seed\": $((seed + 1)), \"delay_seconds\": 1}}")" 'd["id"]')
runs=""
for _ in $(seq 1 200); do
    runs=$(curl -fsS "$BASE/api/jobs?source_id=$sched_id")
    [[ $(json_get "$runs" 'sum(r["status"] == "succeeded" for r in d["runs"])') -ge 2 ]] && break
    sleep 1
done
patch_json "/api/sources/$sched_id" '{"enabled": false}' >/dev/null
check=$(python3 -c '
import json, sys
from datetime import datetime
runs = sorted(json.loads(sys.argv[1])["runs"], key=lambda r: r["id"])
done = [r for r in runs if r["status"] == "succeeded"]
if len(done) < 2:
    print("fewer than two runs:", [(r["id"], r["status"]) for r in runs]); sys.exit()
t = lambda s: datetime.fromisoformat(s)
gap = (t(done[1]["queued_at"]) - t(done[0]["queued_at"])).total_seconds()
overlap = any(t(b["started_at"]) < t(a["finished_at"]) for a, b in zip(done, done[1:]))
print(f"ok gap={gap:.0f}s" if 45 <= gap <= 75 and not overlap else f"bad gap={gap:.0f}s overlap={overlap}")' "$runs")
[[ "$check" == ok* ]] || fail "scheduled runs: $check"
echo "PASS source scheduled every minute: runs $(json_get "$runs" '[r["id"] for r in sorted(d["runs"], key=lambda r: r["id"])][:2]') queued $check apart, no overlap"

long_id=$(json_get "$(post_json /api/sources "{\"name\": \"Long $run_tag\", \"addon_key\": \"demo\",
    \"config\": {\"count\": 1000, \"seed\": $((seed + 2)), \"delay_seconds\": 1}}")" 'd["id"]')
long_run=$(json_get "$(post_json "/api/sources/$long_id/run" '{}')" 'd["run_id"]')
for _ in $(seq 1 60); do
    [[ $(json_get "$(curl -fsS "$BASE/api/jobs/$long_run")" 'd["status"]') == running ]] && break
    sleep 0.5
done
sleep 2
start=$(date +%s.%N)
post_json "/api/jobs/$long_run/cancel" '{}' >/dev/null
status=""
for _ in $(seq 1 100); do
    status=$(json_get "$(curl -fsS "$BASE/api/jobs/$long_run")" 'd["status"]')
    [[ "$status" == cancelled ]] && break
    sleep 0.1
done
elapsed=$(python3 -c "import sys; print(f'{float(sys.argv[2]) - float(sys.argv[1]):.1f}')" "$start" "$(date +%s.%N)")
[[ "$status" == cancelled ]] || fail "long run $long_run is $status after cancel"
python3 -c "import sys; sys.exit(float(sys.argv[1]) > 5)" "$elapsed" || fail "cancel took ${elapsed}s"
emitted=$(json_get "$(curl -fsS "$BASE/api/jobs/$long_run")" 'd["stats"]["emitted"]')
[[ "$emitted" -lt 1000 ]] || fail "long run emitted everything"
in_app test ! -e "$data_dir/work/$long_run" || fail "work dir of run $long_run left behind"
echo "PASS cancelling long run $long_run: cancelled ${elapsed}s after the request (emitted $emitted of 1000), work dir removed"

# Phase 6: built-in add-ons with the real tools. A throwaway HTTP server next to the worker serves
# generated media, so yt-dlp (generic extractor), gallery-dl (directlink) and direct run offline.
in_worker() {
    if [[ "$LOCAL" == 1 ]]; then
        "$@"
    else
        "${compose[@]}" exec -T worker "$@"
    fi
}

versions=""
for _ in $(seq 1 120); do
    versions=$(in_worker cat "$data_dir/tools/versions.json" 2>/dev/null || true)
    [[ -n "$versions" ]] && break
    sleep 1
done
[[ -n "$versions" ]] || fail "no $data_dir/tools/versions.json"
[[ $(json_get "$versions" 'all(d[k] for k in ("yt-dlp", "yt-dlp-ejs", "gallery-dl")) and not d["error"]') == True ]] ||
    fail "crawler tools not installed: $versions"
in_app python -c 'import importlib.util, sys; sys.exit(any(importlib.util.find_spec(m) for m in ("yt_dlp", "gallery_dl")))' ||
    fail "yt-dlp or gallery-dl is importable in the app environment"
echo "PASS crawler tool venv: $(json_get "$versions" '{k: d[k] for k in ("yt-dlp", "yt-dlp-ejs", "gallery-dl", "deno")}'); not in the app venv"
if [[ "$LOCAL" != 1 ]]; then
    [[ $(json_get "$versions" 'bool(d["deno"])') == True ]] || fail "deno missing in the image: $versions"
fi

crawl_tag="p6$run_tag"
crawl_dir="/tmp/smoke-crawl-$crawl_tag"
in_worker python scripts/smoke_crawl_media.py "$crawl_dir" "$crawl_tag"
media_port=8791
if [[ "$LOCAL" == 1 ]]; then
    python3 -m http.server "$media_port" --bind 127.0.0.1 --directory "$crawl_dir" >/dev/null 2>&1 &
    media_server=$!
    trap 'kill "$media_server" 2>/dev/null; rm -rf "$TMP"' EXIT
else
    "${compose[@]}" exec -d worker python -m http.server "$media_port" --bind 127.0.0.1 --directory "$crawl_dir"
fi
media_url="http://127.0.0.1:$media_port"
served=""
for _ in $(seq 1 30); do
    in_worker python -c 'import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=2)' \
        "$media_url/direct-$crawl_tag-1.png" >/dev/null 2>&1 && served=1 && break
    sleep 1
done
[[ -n "$served" ]] || fail "media server on port $media_port not reachable from the worker"

new_source() { # new_source <name> <addon> <config json>: create a source, print its id
    json_get "$(post_json /api/sources "{\"name\": \"$1 $run_tag\", \"addon_key\": \"$2\", \"config\": $3}")" 'd["id"]'
}
run_source() { # run_source <source_id>: Run now and wait; prints the run JSON
    wait_run "$(json_get "$(post_json "/api/sources/$1/run" '{}')" 'd["run_id"]')" 300
}

yt_id=$(new_source "yt-dlp" ytdlp "{\"urls\": [\"$media_url/clip-$crawl_tag.mp4\"], \"extra_args\": [\"--verbose\"]}")
run=$(run_source "$yt_id")
yt_run=$(json_get "$run" 'd["id"]')
[[ $(json_get "$run" '(d["status"], d["stats"]["emitted"], d["stats"]["new"])') == "('succeeded', 1, 1)" ]] ||
    fail "yt-dlp run: $run"
remote=$(sql "SELECT remote_id FROM file_sources WHERE job_run_id = $yt_run")
[[ "$remote" == "generic clip-$crawl_tag" ]] || fail "yt-dlp remote id: $remote"
kind=$(sql "SELECT f.media_kind FROM files f JOIN file_sources s ON s.file_id = f.id WHERE s.job_run_id = $yt_run")
[[ "$kind" == video ]] || fail "yt-dlp file is $kind"
yt_log=$(curl -fsS "$BASE/api/jobs/$yt_run/log")
runtimes=$(grep -o '\[debug\] JS runtimes: [^"\\]*' <<<"$yt_log" | head -1)
if [[ "$LOCAL" != 1 ]]; then
    [[ "$runtimes" == *deno-* ]] || fail "yt-dlp did not detect deno: '$runtimes'"
    worker_log | grep -q "JS runtimes: deno-" || fail "worker log lacks the JS runtime line"
fi
echo "PASS ytdlp source: run $yt_run new 1 (video, remote id '$remote'); --verbose shows '$runtimes'"
run=$(run_source "$yt_id")
yt_run2=$(json_get "$run" 'd["id"]')
[[ $(json_get "$run" '(d["status"], d["stats"]["emitted"], d["stats"]["new"])') == "('succeeded', 0, 0)" ]] ||
    fail "second yt-dlp run: $run"
grep -q "has already been recorded in the archive" <<<"$(curl -fsS "$BASE/api/jobs/$yt_run2/log")" ||
    fail "second yt-dlp run did not use the archive"
echo "PASS ytdlp second run $yt_run2 downloads nothing (archive hit)"

gd_id=$(new_source "gallery-dl" gallerydl "{\"urls\": [\"$media_url/gallery-$crawl_tag-1.png\", \"$media_url/gallery-$crawl_tag-2.png\"],
    \"tag_fields\": [\"extension=type\", \"category\"]}")
run=$(run_source "$gd_id")
gd_run=$(json_get "$run" 'd["id"]')
[[ $(json_get "$run" '(d["status"], d["stats"]["new"])') == "('succeeded', 2)" ]] || fail "gallery-dl run: $run"
remotes=$(sql "SELECT string_agg(remote_id, ',' ORDER BY remote_id) FROM file_sources WHERE job_run_id = $gd_run")
[[ "$remotes" == "directlink:gallery-$crawl_tag-1,directlink:gallery-$crawl_tag-2" ]] || fail "gallery-dl remote ids: $remotes"
gd_file=$(sql "SELECT file_id FROM file_sources WHERE job_run_id = $gd_run ORDER BY id LIMIT 1")
state=$(tag_state "$gd_file")
[[ "$state" == "active=directlink:crawler,type:png:crawler removed=" ]] || fail "gallery-dl tags: $state"
echo "PASS gallerydl source: run $gd_run new 2 ($remotes) with tags from tag_fields ($state)"

di_id=$(new_source "direct" direct "{\"urls\": [\"$media_url/direct-$crawl_tag-1.png\", \"$media_url/direct-$crawl_tag-2.png\"], \"tags\": [\"Direct Smoke\"]}")
run=$(run_source "$di_id")
di_run=$(json_get "$run" 'd["id"]')
[[ $(json_get "$run" '(d["status"], d["stats"]["new"])') == "('succeeded', 2)" ]] || fail "direct run: $run"
di_file=$(sql "SELECT file_id FROM file_sources WHERE job_run_id = $di_run ORDER BY id LIMIT 1")
[[ $(tag_state "$di_file") == "active=direct_smoke:crawler removed=" ]] || fail "direct tags: $(tag_state "$di_file")"
echo "PASS direct source: run $di_run new 2 image(s) with tags"

in_app python -c 'from app.crawl.safety import check_ytdlp_args as c
for a in (["--exec", "id"], ["--exec=id"], ["-o", "x"], ["--batch-fil", "x"]):
    try:
        c(a)
    except ValueError:
        continue
    raise SystemExit(f"accepted {a}")' || fail "extra_args denylist"
code=$(curl -s -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' -X POST "$BASE/api/sources" \
    -d "{\"name\": \"Bad $run_tag\", \"addon_key\": \"ytdlp\", \"config\": {\"urls\": [\"https://example.org\"], \"extra_args\": [\"--exec=id\"]}}")
[[ "$code" == 400 ]] || fail "source with --exec accepted ($code)"
echo "PASS extra_args with --exec refused by the API ($code)"

# Phase 7: search. Files in Animals, Animals / Cats-<tag> and Other-<tag> with cat/dog tags;
# the acceptance query returns exactly what an independent SQL query finds.
page=$(curl -fsS "$BASE/search") || fail "GET /search"
grep -q "Coming in phase" <<<"$page" && fail "/search is still a placeholder"
grep -q 'class="nav-search"' <<<"$page" || fail "no search box in the navigation"
search_args=()
for f in "$media"/search/*; do search_args+=(-F "files=@$f"); done
reply=$(curl -fsS "${search_args[@]}" "$BASE/api/uploads") || fail "POST search files"
search_batch=$(json_get "$reply" 'd["batch_id"]')
status=$(wait_batch "$search_batch")
[[ $(json_get "$status" 'd["counts"]["new"]') == 7 ]] || fail "search upload: $status"
sfile() { # sfile <original filename>: id of that file of the search batch
    sql "SELECT id FROM files WHERE original_filename = '$1' AND staging_path LIKE 'staging/$search_batch/%'"
}
s_cat=$(sfile search00.png); s_sub=$(sfile search01.png); s_both=$(sfile search02.png)
s_other=$(sfile search03.png); s_plain=$(sfile search04.png); s_pending=$(sfile search05.png)
s_video=$(sfile clip.mp4)
animals=$(curl -fs "$BASE/api/folders/resolve?path=Animals" | python3 -c 'import json, sys; print(json.load(sys.stdin)["id"])' 2>/dev/null || true)
[[ -n "$animals" ]] || animals=$(json_get "$(post_json /api/folders '{"name": "Animals"}')" 'd["id"]')
cats_sub=$(json_get "$(post_json /api/folders "{\"name\": \"Cats $run_tag\", \"parent_id\": $animals}")" 'd["id"]')
other=$(json_get "$(post_json /api/folders "{\"name\": \"Other $run_tag\"}")" 'd["id"]')
archive_into() { # archive_into <folder_id> <tags json> <file ids...>
    local folder=$1 tags=$2
    shift 2
    local ids
    ids=$(IFS=,; echo "$*")
    reply=$(post_json /api/review/archive "{\"file_ids\": [$ids], \"folder_id\": $folder, \"add_tags\": $tags}")
    [[ $(json_get "$reply" 'd["ok"]') == True ]] || fail "archive $ids: $reply"
}
archive_into "$animals" '["cat"]' "$s_cat" "$s_video"
archive_into "$cats_sub" '["cat"]' "$s_sub"
archive_into "$animals" '["cat", "dog"]' "$s_both"
archive_into "$other" '["cat"]' "$s_other"
archive_into "$animals" '[]' "$s_plain"
post_json "/api/files/$s_pending/tags" '{"name": "cat"}' >/dev/null  # pending: not archived

search_ids() { # search_ids <query> [extra curl args]: sorted ids of every result page
    python3 - "$BASE" "$1" <<'PY'
import json, sys, urllib.parse, urllib.request
base, query = sys.argv[1], sys.argv[2]
ids, cursor = [], None
while True:
    params = {"q": query, "limit": 100}
    if cursor:
        params["cursor"] = cursor
    with urllib.request.urlopen(f"{base}/api/search?{urllib.parse.urlencode(params)}") as r:
        d = json.load(r)
    ids += [i["id"] for i in d["items"]]
    cursor = d["next_cursor"]
    if not cursor:
        break
assert len(ids) == len(set(ids)) == d["total"], (len(ids), d["total"])
print(",".join(str(i) for i in sorted(ids)))
PY
}
query='cat -dog folder:"Animals" collected:2026-01-01..2026-12-31 type:image'
got=$(search_ids "$query") || fail "search $query"
expected=$(sql "WITH RECURSIVE sub(id) AS (SELECT $animals::bigint UNION ALL SELECT f.id FROM folders f JOIN sub ON f.parent_id = sub.id)
SELECT string_agg(id::text, ',' ORDER BY id) FROM files f WHERE status = 'archived' AND media_kind = 'image'
 AND folder_id IN (SELECT id FROM sub)
 AND collected_at >= timestamptz '2026-01-01 00:00 Asia/Seoul' AND collected_at < timestamptz '2027-01-01 00:00 Asia/Seoul'
 AND EXISTS (SELECT 1 FROM file_tags ft JOIN tags t ON t.id = ft.tag_id WHERE ft.file_id = f.id AND t.name = 'cat' AND NOT ft.removed)
 AND NOT EXISTS (SELECT 1 FROM file_tags ft JOIN tags t ON t.id = ft.tag_id WHERE ft.file_id = f.id AND t.name = 'dog' AND NOT ft.removed)")
[[ "$got" == "$expected" ]] || fail "search '$query' returned $got, SQL says $expected"
for id in "$s_cat" "$s_sub"; do [[ ",$got," == *",$id,"* ]] || fail "$id missing from $got"; done
for id in "$s_both" "$s_other" "$s_plain" "$s_pending" "$s_video"; do
    [[ ",$got," == *",$id,"* ]] && fail "$id should not match"
done
echo "PASS search '$query' returns exactly the files SQL finds ($got; includes $s_cat in Animals and $s_sub in Animals / Cats $run_tag, excludes dog, Other, untagged, pending and video)"

page=$(curl -fsS -G "$BASE/search" --data-urlencode "q=$query") || fail "GET /search?q=..."
for want in 'name="tags_all" value="cat"' 'name="tags_none" value="dog"' 'name="collected_from" value="2026-01-01"' \
    'name="collected_to" value="2026-12-31"' "<option value=\"$animals\" selected>Animals</option>" \
    'name="type" value="image" checked' 'name="status" value="archived" checked'; do
    grep -qF "$want" <<<"$page" || fail "filter panel lacks $want"
done
[[ $(grep -c 'class="tile library-tile' <<<"$page") == $(tr ',' '\n' <<<"$got" | wc -l) ]] || fail "result grid size"
echo "PASS /search shows the same conditions in the filter panel and $(grep -c 'class="tile library-tile' <<<"$page") tiles"

location=$(curl -s -o /dev/null -w '%{redirect_url}' -G "$BASE/search" --data-urlencode panel=1 \
    --data-urlencode tags_all=cat --data-urlencode tags_none=dog --data-urlencode "folder=$animals" \
    --data-urlencode collected_from=2026-01-01 --data-urlencode collected_to=2026-12-31 \
    --data-urlencode type=image --data-urlencode type=video --data-urlencode status=archived)
new_q=$(python3 -c 'import sys, urllib.parse as u; print(u.parse_qs(u.urlsplit(sys.argv[1]).query)["q"][0])' "$location")
[[ "$new_q" == "$query type:video" ]] || fail "panel change redirected to $location"
echo "PASS changing the panel (adding type video) redirects to $location"

saved=$(post_json /api/saved-searches "{\"name\": \"Cats $run_tag\", \"query\": $(python3 -c 'import json, sys; print(json.dumps(sys.argv[1]))' "$query")}")
saved_id=$(json_get "$saved" 'd["id"]')
reopened=$(search_ids "$(json_get "$(curl -fsS "$BASE/api/saved-searches/$saved_id")" 'd["query"]')")
[[ "$reopened" == "$got" ]] || fail "saved search $saved_id returned $reopened, not $got"
grep -q "data-saved-id=\"$saved_id\"" <<<"$(curl -fsS "$BASE/search")" || fail "saved search not in the sidebar"
echo "PASS saved search $saved_id reopens with the same results ($reopened)"

moved=$(json_get "$(post_json /api/folders "{\"name\": \"Moved $run_tag\"}")" 'd["id"]')
reply=$(post_json /api/library/move "{\"file_ids\": [$got], \"folder_id\": $moved}")
[[ $(json_get "$reply" 'd["ok"]') == True ]] || fail "bulk move of search results: $reply"
in_moved=$(search_ids "folder=:\"Moved $run_tag\"")
[[ "$in_moved" == "$got" ]] || fail "Moved $run_tag holds $in_moved, not $got"
[[ $(search_ids "$query") == "" ]] || fail "results still in Animals after the move"
echo "PASS bulk-moved the search results to Moved $run_tag ($in_moved)"

# Phase 8: operations. Authentication (a second server with APP_PASSWORD), settings, backup,
# integrity, tool updates, worker restart during a crawl, then make restore on a fresh database.
in_app python scripts/smoke_auth.py || fail "authentication checks"

for path in / /upload /review /library /search /tags /sources /jobs /settings; do
    grep -q 'id="no-password-banner"' <<<"$(curl -fsS "$BASE$path")" || fail "no 'No password set' banner on $path"
done
grep -q 'id="disk-usage"' <<<"$(curl -fsS "$BASE/")" || fail "dashboard disk usage"
echo "PASS without APP_PASSWORD every page shows the 'No password set' banner"
code=$(command curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/api/debug/ping")
[[ "$code" == 403 ]] || fail "POST without Origin returned $code"
echo "PASS POST without an Origin or Referer header is refused (403)"

settings_page=$(curl -fsS "$BASE/settings")
grep -q 'id="settings-form"' <<<"$settings_page" || fail "/settings is not the settings page"
location=$(curl -s -o /dev/null -w '%{redirect_url}' -X POST "$BASE/settings" --data-urlencode "image_phash_threshold=7")
[[ "$location" == *"/settings?notice=thresholds" ]] || fail "threshold change redirected to $location"
grep -q 'id="recompute-similar"' <<<"$(curl -fsS "$location")" || fail "no recompute offer after a threshold change"
[[ $(sql "SELECT value FROM app_settings WHERE key = 'image_phash_threshold'") == 7 ]] || fail "setting not stored"
curl -fsS -o /dev/null -X POST "$BASE/settings" --data-urlencode "image_phash_threshold=6"
[[ $(sql "SELECT count(*) FROM app_settings WHERE key = 'image_phash_threshold'") == 0 ]] || fail "default not restored"
echo "PASS /settings stores an override, offers recompute-similar after a threshold change, and drops the row at the default"

wait_job() { # wait_job <run_id> [seconds]: wait until a maintenance run ended; print its JSON
    local body=""
    for _ in $(seq 1 "${2:-120}"); do
        body=$(curl -fsS "$BASE/api/jobs/$1") || fail "GET /api/jobs/$1"
        [[ $(json_get "$body" 'd["status"] not in ("queued", "running")') == True ]] && { echo "$body"; return; }
        sleep 1
    done
    fail "job $1 did not finish: $body"
}

run_from_settings() { # run_from_settings <kind> [form fields...]: Run now; print the run id
    local kind=$1 location
    shift
    location=$(curl -s -o /dev/null -w '%{redirect_url}' -X POST "$BASE/settings/run/$kind" "$@")
    [[ "$location" =~ /jobs/([0-9]+)$ ]] || fail "Run now ($kind) redirected to '$location'"
    echo "${BASH_REMATCH[1]}"
}

# Backup from /settings: a dump pg_restore can read. Counts are kept for the restore check.
files_at_backup=$(sql "SELECT count(*) FROM files")
tags_at_backup=$(sql "SELECT count(*) FROM tags")
backup_run=$(run_from_settings backup)
job=$(wait_job "$backup_run")
[[ $(json_get "$job" 'd["status"]') == succeeded ]] || fail "backup run $backup_run: $job"
dump=$(json_get "$job" 'd["stats"]["file"]')
entries=$(in_app pg_restore --list "$data_dir/$dump" | grep -vc '^;')
[[ "$entries" -gt 20 ]] || fail "pg_restore --list $dump shows $entries entries"
grep -q "${dump#backups/}" <<<"$(curl -fsS "$BASE/settings")" || fail "/settings does not list $dump"
echo "PASS backup from /settings (run $backup_run): $dump, $(json_get "$job" 'd["stats"]["size_bytes"]') bytes, pg_restore --list reads $entries entries ($(json_get "$job" 'd["stats"]["pg_dump"]'))"

# Integrity: a clean report, then exactly the archive file deleted by hand.
integrity_run=$(run_from_settings integrity -d verify_hashes=1)
job=$(wait_job "$integrity_run" 300)
[[ $(json_get "$job" '(d["status"], d["stats"]["total_problems"])') == "('succeeded', 0)" ]] ||
    fail "baseline integrity run $integrity_run: $(json_get "$job" 'd["stats"]["counts"], d["stats"]["problems"]')"
victim=$(sql "SELECT id FROM files WHERE status = 'archived' ORDER BY id LIMIT 1")
victim_path=$(sql "SELECT 'archive/' || substr(h, 1, 2) || '/' || substr(h, 3, 2) || '/' || h || coalesce('.' || ext, '') FROM (SELECT encode(sha256, 'hex') AS h, ext FROM files WHERE id = $victim) f")
in_app rm "$data_dir/$victim_path"
integrity_run=$(run_from_settings integrity)
job=$(wait_job "$integrity_run" 300)
[[ $(json_get "$job" 'd["stats"]["total_problems"]') == 1 ]] || fail "integrity after deleting $victim_path: $(json_get "$job" 'd["stats"]["counts"]')"
[[ $(json_get "$job" "d['stats']['problems']['missing_archive'] == [{'file_id': $victim, 'path': '$victim_path'}]") == True ]] ||
    fail "integrity report: $(json_get "$job" 'd["stats"]["problems"]["missing_archive"]')"
report=$(curl -fsS "$BASE/jobs/$integrity_run")
grep -q 'data-count="missing_archive">1<' <<<"$report" && grep -qF "$victim_path" <<<"$report" ||
    fail "job page $integrity_run does not show the report"
echo "PASS integrity check (verify hashes) is clean, then reports exactly file $victim ($victim_path) after its archive file was deleted"

# Crawler tool update from /settings: the versions appear there.
tools_run=$(run_from_settings update_tools)
job=$(wait_job "$tools_run" 900)
[[ $(json_get "$job" 'd["status"]') == succeeded ]] || fail "update_tools run $tools_run: $job"
ytdlp=$(json_get "$job" 'd["stats"]["after"]["yt-dlp"]')
gdl=$(json_get "$job" 'd["stats"]["after"]["gallery-dl"]')
settings_page=$(curl -fsS "$BASE/settings")
grep -q "data-version=\"yt-dlp\">$ytdlp<" <<<"$settings_page" && grep -q "data-version=\"gallery-dl\">$gdl<" <<<"$settings_page" ||
    fail "/settings does not show yt-dlp $ytdlp and gallery-dl $gdl"
grep -q "\"gallery-dl\": \"$gdl\"" <<<"$(in_app cat "$data_dir/tools/versions.json")" || fail "versions.json"
echo "PASS tool update (run $tools_run): /settings shows yt-dlp $ytdlp, gallery-dl $gdl; before $(json_get "$job" 'd["stats"]["before"]')"

grep -q "data-run-id=\"$tools_run\"" <<<"$(curl -fsS "$BASE/jobs?kind=update_tools&status=succeeded")" || fail "/jobs kind filter"
grep -q "data-run-id=\"$tools_run\"" <<<"$(curl -fsS "$BASE/jobs?kind=backup")" && fail "/jobs?kind=backup lists an update_tools run"
echo "PASS /jobs filters by kind and status"

# Restart the worker during a crawl: the run becomes failed "interrupted"; the next run works.
restart_id=$(json_get "$(post_json /api/sources "{\"name\": \"Restart $run_tag\", \"addon_key\": \"demo\", \"config\": {\"count\": 300, \"seed\": $((seed + 3)), \"delay_seconds\": 1}, \"max_runtime_minutes\": 30}")" 'd["id"]')
restart_run=$(json_get "$(post_json "/api/sources/$restart_id/run" '{}')" 'd["run_id"]')
for _ in $(seq 1 60); do
    [[ $(json_get "$(curl -fsS "$BASE/api/jobs/$restart_run")" 'd["status"] == "running" and d["stats"]["emitted"] >= 2') == True ]] && break
    sleep 1
done
if [[ "$LOCAL" == 1 ]]; then
    bash -c "${SMOKE_RESTART_WORKER:?set SMOKE_RESTART_WORKER with SMOKE_LOCAL=1}"
else
    "${compose[@]}" restart worker
fi
status=""
for _ in $(seq 1 300); do
    run=$(curl -fsS "$BASE/api/jobs/$restart_run")
    status=$(json_get "$run" '(d["status"], d["error"])')
    [[ "$status" == "('failed', 'interrupted')" ]] && break
    sleep 1
done
[[ "$status" == "('failed', 'interrupted')" ]] || fail "run $restart_run after the worker restart: $status"
curl -fsS -X PUT -H 'Content-Type: application/json' "$BASE/api/sources/$restart_id" \
    -d "{\"name\": \"Restart $run_tag\", \"config\": {\"count\": 2, \"seed\": $((seed + 4))}, \"max_runtime_minutes\": 10}" >/dev/null ||
    fail "PUT source $restart_id"
next_run=$(json_get "$(post_json "/api/sources/$restart_id/run" '{}')" 'd["run_id"]')
run=$(wait_run "$next_run" 300)
[[ $(json_get "$run" '(d["status"], d["stats"]["new"])') == "('succeeded', 2)" ]] || fail "run after the restart: $run"
echo "PASS worker restarted during run $restart_run: it is failed with error 'interrupted'; the next run $next_run of the source succeeded with 2 new files"

# make restore on a fresh database brings the data of the backup back.
if [[ "${SMOKE_SKIP_RESTORE:-0}" == 1 ]]; then
    echo "SKIP make restore (SMOKE_SKIP_RESTORE=1)"
elif [[ "$LOCAL" == 1 ]]; then
    sql "DROP SCHEMA public CASCADE; CREATE SCHEMA public" >/dev/null
    in_app python -m app.db.migrate >/dev/null
    [[ $(sql "SELECT count(*) FROM files") == 0 ]] || fail "fresh database is not empty"
    in_app python -m app.cli restore --yes "${dump#backups/}" || fail "restore $dump"
    # Like make restore: web and worker start again on the restored database.
    bash -c "${SMOKE_RESTART_WEB:?set SMOKE_RESTART_WEB with SMOKE_LOCAL=1}"
    bash -c "$SMOKE_RESTART_WORKER"
    for _ in $(seq 1 120); do
        curl -fsS "$BASE/healthz" >/dev/null 2>&1 && break
        sleep 1
    done
    echo "(local: dropped and restored the stack's database; CI runs make restore on a new volume)"
else
    "${compose[@]}" down
    docker volume rm "$(docker volume ls -q --filter label=com.docker.compose.project=median --filter label=com.docker.compose.volume=pgdata)" >/dev/null
    make up >/dev/null
    for _ in $(seq 1 120); do
        curl -fsS "$BASE/healthz" >/dev/null 2>&1 && break
        sleep 1
    done
    [[ $(sql "SELECT count(*) FROM files") == 0 ]] || fail "the fresh stack is not empty"
    make restore FILE="${dump#backups/}" CONFIRM=yes || fail "make restore $dump"
    for _ in $(seq 1 180); do
        curl -fsS "$BASE/healthz" >/dev/null 2>&1 && break
        sleep 1
    done
fi
if [[ "${SMOKE_SKIP_RESTORE:-0}" != 1 ]]; then
    [[ $(sql "SELECT count(*) FROM files") == "$files_at_backup" ]] || fail "files after restore: $(sql "SELECT count(*) FROM files"), backup had $files_at_backup"
    [[ $(sql "SELECT count(*) FROM tags") == "$tags_at_backup" ]] || fail "tags after restore"
    [[ $(sql "SELECT version_num FROM alembic_version") == 0007 ]] || fail "migrations after restore"
    curl -fsS "$BASE/library" >/dev/null || fail "library after restore"
    job=$(wait_job "$(run_from_settings housekeeping)" 300)
    [[ $(json_get "$job" 'd["status"]') == succeeded ]] || fail "housekeeping after restore: $job"
    echo "PASS restore of $dump on a fresh database: $files_at_backup files and $tags_at_backup tags back, migrations current, jobs run again"
fi

echo "All smoke checks passed."
