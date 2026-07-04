#!/usr/bin/env bash
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
sut="$here/compute-dev-tag.sh"
fail=0
check() { # desc expected VERSION HEAD_BRANCH WORKFLOW_EVENT RUN_ID
  local desc="$1" expected="$2" got
  got=$(VERSION="$3" HEAD_BRANCH="$4" WORKFLOW_EVENT="$5" RUN_ID="$6" bash "$sut")
  if [ "$got" = "$expected" ]; then echo "ok   - $desc"
  else echo "FAIL - $desc: expected '$expected' got '$got'"; fail=1; fi
}
check "main push -> rc"                 "dev-v3.1.0-rc123"        "3.1.0" "main"     "push"         "123"
check "PR branch -> branch build"       "dev-v3.1.0-feat-x-123"  "3.1.0" "feat-x"   "pull_request" "123"
check "slash in branch sanitised"       "dev-v3.1.0-feat-a-b-9"  "3.1.0" "feat/a/b" "pull_request" "9"
check "double dash collapsed"           "dev-v3.1.0-a-b-9"       "3.1.0" "a--b"     "pull_request" "9"
check "main via pull_request is branch" "dev-v3.1.0-main-9"      "3.1.0" "main"     "pull_request" "9"
[ "$fail" -eq 0 ] && echo "ALL PASS" || { echo "FAILURES"; exit 1; }
