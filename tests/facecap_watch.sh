#!/bin/sh
# facecap_watch.sh: copy short-lived WeChat fdv*.mp4/thumb* before unlink.
# Read-only with respect to the source directory; no WeChat or camera changes.
set -u
umask 077

SOURCE=/data/data/com.tencent.mm/face_detect
OUTPUT=/data/local/tmp/facecap
INTERVAL=0.1
STABLE_POLLS=3
DURATION=0

usage() {
    cat <<'EOF'
Usage: sh facecap_watch.sh [options]

Copy new fdv*.mp4 and thumb* files into an evidence directory about every
0.1 seconds. Copies are byte-for-byte snapshots; no transcoding is done.

Options:
  --source DIR       WeChat face_detect directory
                     (default: /data/data/com.tencent.mm/face_detect)
  --output DIR       Capture directory (default: /data/local/tmp/facecap)
  --interval SEC     Poll interval, decimal allowed (default: 0.1)
  --stable-polls N   Unchanged size/mtime polls before marking stable (default: 3)
  --duration SEC     Stop after N whole seconds; 0 means forever (default: 0)
  -h, --help         Show this help

The output contains capture_log.tsv, original-name snapshots, and internal
.state/.watcher.lock directories. A snapshot can be incomplete if WeChat
unlinks it while it is still growing; source_removed and the stable flag in
capture_log.tsv preserve that fact. A second watcher on the same output exits
with status 2. Stop a running watcher with TERM or INT.

Example (container side):
  sh /data/local/tmp/facecap_watch.sh --duration 120
EOF
}

fail() {
    echo "facecap_watch: $*" >&2
    exit 2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --source) [ "$#" -ge 2 ] || fail "--source needs a value"; SOURCE=$2; shift 2 ;;
        --output) [ "$#" -ge 2 ] || fail "--output needs a value"; OUTPUT=$2; shift 2 ;;
        --interval) [ "$#" -ge 2 ] || fail "--interval needs a value"; INTERVAL=$2; shift 2 ;;
        --stable-polls) [ "$#" -ge 2 ] || fail "--stable-polls needs a value"; STABLE_POLLS=$2; shift 2 ;;
        --duration) [ "$#" -ge 2 ] || fail "--duration needs a value"; DURATION=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) fail "unknown option: $1 (try --help)" ;;
    esac
done

case "$STABLE_POLLS" in *[!0-9]*|'') fail "--stable-polls must be a positive integer" ;; esac
case "$DURATION" in *[!0-9]*|'') fail "--duration must be a non-negative integer" ;; esac
[ "$STABLE_POLLS" -ge 1 ] || fail "--stable-polls must be at least 1"
case "$INTERVAL" in ''|*[!0-9.]*) fail "--interval must be numeric" ;; esac
[ -d "$SOURCE" ] || fail "source directory does not exist: $SOURCE"
[ "$SOURCE" != "$OUTPUT" ] || fail "source and output must differ"
command -v stat >/dev/null 2>&1 || fail "stat is required (Android toybox provides it)"
stat -c %s "$SOURCE" >/dev/null 2>&1 || fail "stat -c is required"
sleep "$INTERVAL" 2>/dev/null || fail "invalid --interval: $INTERVAL"

mkdir -p "$OUTPUT" "$OUTPUT/.state" || fail "cannot create output: $OUTPUT"
LOCK=$OUTPUT/.watcher.lock
if mkdir "$LOCK" 2>/dev/null; then
    :
elif [ -r "$LOCK/pid" ] && read -r OTHER_PID < "$LOCK/pid" && [ -n "$OTHER_PID" ] && kill -0 "$OTHER_PID" 2>/dev/null; then
    echo "facecap_watch: already running with pid $OTHER_PID (output $OUTPUT)" >&2
    exit 2
else
    # Only remove a lock whose owner is gone. mkdir below resolves stale-lock races.
    rm -f "$LOCK/pid" 2>/dev/null
    rmdir "$LOCK" 2>/dev/null || fail "cannot reclaim stale lock: $LOCK"
    mkdir "$LOCK" 2>/dev/null || fail "another watcher acquired the lock"
fi
echo $$ > "$LOCK/pid" || fail "cannot write watcher pid"

LOG=$OUTPUT/capture_log.tsv
if [ ! -s "$LOG" ]; then
    printf 'timestamp	event	source_name	capture_name	bytes	stable_polls	stable	sha256	detail
' > "$LOG"
fi

SEQUENCE=0
END_TIME=0
if [ "$DURATION" -gt 0 ]; then
    END_TIME=$(( $(date +%s) + DURATION ))
fi

cleanup() {
    trap - EXIT INT TERM
    rm -f "$OUTPUT"/.*.$$.partial 2>/dev/null
    if [ -r "$LOCK/pid" ] && read -r LOCK_PID < "$LOCK/pid" && [ "$LOCK_PID" = "$$" ]; then
        rm -f "$LOCK/pid"
        rmdir "$LOCK" 2>/dev/null
    fi
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

hash_file() {
    if command -v sha256sum >/dev/null 2>&1; then
        HASH_OUTPUT=$(sha256sum "$1" 2>/dev/null) || { printf '-'; return; }
        printf '%s' "${HASH_OUTPUT%% *}"
    else
        printf '-'
    fi
}

log_event() {
    # Names accepted below cannot contain tabs/newlines, so TSV stays unambiguous.
    SEQUENCE=$((SEQUENCE + 1))
    printf '%s	%s	%s	%s	%s	%s	%s	%s	%s
' \
        "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$1" "$2" "$3" "$4" "$5" "$6" "$7" "$8" >> "$LOG"
}

save_state() {
    # id|size|mtime|stable_count|complete|missing_reported|name
    STATE_TMP=$OUTPUT/.state/.$1.$$.tmp
    printf '%s|%s|%s|%s|%s|%s|%s
' "$2" "$3" "$4" "$5" "$6" "$7" "$1" > "$STATE_TMP" &&
        mv -f "$STATE_TMP" "$OUTPUT/.state/$1.state"
}

archive_existing() {
    ARCHIVE_NAME=$1
    ARCHIVE_OLD_ID=$2
    ARCHIVE_PATH=$OUTPUT/$ARCHIVE_NAME
    [ -f "$ARCHIVE_PATH" ] || return 0
    ARCHIVE_STAMP=$(date '+%Y%m%d-%H%M%S')
    case "$ARCHIVE_NAME" in
        *.*) ARCHIVE_BASE=${ARCHIVE_NAME%.*}; ARCHIVE_EXT=.${ARCHIVE_NAME##*.} ;;
        *) ARCHIVE_BASE=$ARCHIVE_NAME; ARCHIVE_EXT= ;;
    esac
    ARCHIVE_TARGET=$ARCHIVE_BASE.replaced-$ARCHIVE_STAMP-$$-$SEQUENCE$ARCHIVE_EXT
    if mv "$ARCHIVE_PATH" "$OUTPUT/$ARCHIVE_TARGET" 2>/dev/null; then
        ARCHIVE_SIZE=$(stat -c %s "$OUTPUT/$ARCHIVE_TARGET" 2>/dev/null || printf 0)
        log_event archived "$ARCHIVE_NAME" "$ARCHIVE_TARGET" "$ARCHIVE_SIZE" 0 - - "old_identity=$ARCHIVE_OLD_ID"
    fi
}

scan_file() {
    SCAN_PATH=$1
    SCAN_NAME=${SCAN_PATH##*/}
    case "$SCAN_NAME" in
        ''|*[!A-Za-z0-9._-]*)
            log_event skipped "$SCAN_NAME" - 0 0 - - unsafe_or_empty_name
            return
            ;;
    esac
    [ -f "$SCAN_PATH" ] || return 0

    SCAN_META=$(stat -c '%d:%i|%s|%Y' "$SCAN_PATH" 2>/dev/null) || return 0
    IFS='|' read -r OLD_ID OLD_SIZE OLD_MTIME OLD_STABLE OLD_COMPLETE OLD_MISSING OLD_NAME < "$OUTPUT/.state/$SCAN_NAME.state" 2>/dev/null || {
        OLD_ID=; OLD_SIZE=; OLD_MTIME=; OLD_STABLE=0; OLD_COMPLETE=0; OLD_MISSING=0
    }
    SCAN_ID=${SCAN_META%%|*}; SCAN_REST=${SCAN_META#*|}
    SCAN_SIZE=${SCAN_REST%%|*}; SCAN_MTIME=${SCAN_REST#*|}
    [ "$SCAN_SIZE" = "$SCAN_REST" ] && return 0

    if [ "$OLD_ID" != "$SCAN_ID" ]; then
        OLD_STABLE=0
        OLD_COMPLETE=0
    fi
    [ -f "$OUTPUT/$SCAN_NAME" ] || OLD_COMPLETE=0

    if [ "$OLD_ID" != "$SCAN_ID" ] || [ "$OLD_SIZE" != "$SCAN_SIZE" ] || [ "$OLD_MTIME" != "$SCAN_MTIME" ] || [ ! -f "$OUTPUT/$SCAN_NAME" ]; then
        SCAN_TMP=$OUTPUT/.$SCAN_NAME.$$.partial
        rm -f "$SCAN_TMP" 2>/dev/null
        if cp "$SCAN_PATH" "$SCAN_TMP" 2>/dev/null; then
            if [ -n "$OLD_ID" ] && [ "$OLD_ID" != "$SCAN_ID" ]; then
                archive_existing "$SCAN_NAME" "$OLD_ID"
            fi
            if ! mv -f "$SCAN_TMP" "$OUTPUT/$SCAN_NAME" 2>/dev/null; then
                rm -f "$SCAN_TMP" 2>/dev/null
                log_event copy_error "$SCAN_NAME" - "$SCAN_SIZE" "$OLD_STABLE" 0 - cannot_replace_capture
                return
            fi
            COPIED_SIZE=$(stat -c %s "$OUTPUT/$SCAN_NAME" 2>/dev/null || printf 0)
            AFTER_META=$(stat -c '%d:%i|%s|%Y' "$SCAN_PATH" 2>/dev/null || printf missing)
            if [ "$AFTER_META" = "$SCAN_META" ]; then
                OLD_STABLE=$((OLD_STABLE + 1))
            else
                OLD_STABLE=0
            fi
            log_event snapshot "$SCAN_NAME" "$SCAN_NAME" "$COPIED_SIZE" "$OLD_STABLE" 0 - capture_replaced
        else
            rm -f "$SCAN_TMP" 2>/dev/null
            log_event copy_error "$SCAN_NAME" - "$SCAN_SIZE" "$OLD_STABLE" 0 - source_changed_or_unreadable
            return
        fi
    else
        if [ "$OLD_STABLE" -lt "$STABLE_POLLS" ]; then
            OLD_STABLE=$((OLD_STABLE + 1))
        fi
    fi

    if [ "$OLD_STABLE" -ge "$STABLE_POLLS" ] && [ "$OLD_COMPLETE" != 1 ] && [ -f "$OUTPUT/$SCAN_NAME" ]; then
        OLD_COMPLETE=1
        CAPTURE_SIZE=$(stat -c %s "$OUTPUT/$SCAN_NAME" 2>/dev/null || printf 0)
        CAPTURE_HASH=$(hash_file "$OUTPUT/$SCAN_NAME")
        log_event stable "$SCAN_NAME" "$SCAN_NAME" "$CAPTURE_SIZE" "$OLD_STABLE" 1 "$CAPTURE_HASH" unchanged_during_copy_and_polls
    fi
    save_state "$SCAN_NAME" "$SCAN_ID" "$SCAN_SIZE" "$SCAN_MTIME" "$OLD_STABLE" "$OLD_COMPLETE" 0
}

scan_missing() {
    for STATE_FILE in "$OUTPUT"/.state/*.state; do
        [ -f "$STATE_FILE" ] || continue
        IFS='|' read -r MISS_ID MISS_SIZE MISS_MTIME MISS_STABLE MISS_COMPLETE MISS_MISSING MISS_NAME < "$STATE_FILE" 2>/dev/null || continue
        case "$MISS_NAME" in ''|*[!A-Za-z0-9._-]*) continue ;; esac
        if [ "$MISS_MISSING" = 0 ] && [ ! -f "$SOURCE/$MISS_NAME" ]; then
            if [ -f "$OUTPUT/$MISS_NAME" ]; then
                MISS_CAPTURE_SIZE=$(stat -c %s "$OUTPUT/$MISS_NAME" 2>/dev/null || printf 0)
                MISS_CAPTURE_NAME=$MISS_NAME
            else
                MISS_CAPTURE_SIZE=0
                MISS_CAPTURE_NAME=-
            fi
            MISS_HASH=-
            if [ "$MISS_COMPLETE" = 1 ] && [ -f "$OUTPUT/$MISS_NAME" ]; then
                MISS_HASH=$(hash_file "$OUTPUT/$MISS_NAME")
            fi
            log_event source_removed "$MISS_NAME" "$MISS_CAPTURE_NAME" "$MISS_CAPTURE_SIZE" "$MISS_STABLE" "$MISS_COMPLETE" "$MISS_HASH" capture_retained
            save_state "$MISS_NAME" "$MISS_ID" "$MISS_SIZE" "$MISS_MTIME" "$MISS_STABLE" "$MISS_COMPLETE" 1
        fi
    done
}

log_event watcher_started - - 0 0 - - "pid=$$ interval=$INTERVAL stable_polls=$STABLE_POLLS"
while :; do
    for SCAN_PATH in "$SOURCE"/fdv*.mp4 "$SOURCE"/thumb*; do
        [ -e "$SCAN_PATH" ] || continue
        scan_file "$SCAN_PATH"
    done
    scan_missing
    [ "$DURATION" -gt 0 ] && [ "$(date +%s)" -ge "$END_TIME" ] && break
    sleep "$INTERVAL" || fail "invalid --interval: $INTERVAL"
done
log_event watcher_stopped - - 0 0 - - "pid=$$"
exit 0
