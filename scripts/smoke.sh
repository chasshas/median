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

echo "All smoke checks passed."
