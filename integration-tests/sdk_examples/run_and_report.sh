#!/usr/bin/env bash
#
# Run the PipesHub Go SDK example apps via the SDK repo's own run.sh, then emit a
# JUnit XML the daily integration-tests workflow can fold into its Slack report.
#
# Expects (env):
#   GO_SDK_DIR            path to the checked-out pipeshub-sdk-go repo (required)
#   SDK_EXAMPLES_ENV_PATH .env written by seed_sdk_examples.py (default: $GO_SDK_DIR/examples/.env)
#   SDK_CONNECTOR_NAME    seeded connector name (default: "ABC News RSS")
#
# Exit code mirrors run.sh (non-zero if any example failed) so the suite is blocking.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)" # integration-tests/
REPORTS_DIR="$IT_DIR/reports"
XML="$REPORTS_DIR/gosdk-results.xml"

GO_SDK_DIR="${GO_SDK_DIR:?GO_SDK_DIR must point at the pipeshub-sdk-go checkout}"
ENV_FILE="${SDK_EXAMPLES_ENV_PATH:-$GO_SDK_DIR/examples/.env}"
CONNECTOR_NAME="${SDK_CONNECTOR_NAME:-ABC News RSS}"

mkdir -p "$REPORTS_DIR"

if [ ! -f "$ENV_FILE" ]; then
    echo "::error::SDK examples env file not found: $ENV_FILE (did seeding run?)"
    exit 1
fi

# --- Reconcile hardcoded connector name in the throwaway CI checkout ----------
# The two connector examples match the connector by EXACT name but use different
# strings ("ABC News RSS" vs "abc news"). We seed a single connector, so align the
# semantic_search example to it. This patches only the CI checkout, never the
# source repo; it becomes unnecessary once the examples move to CRUD lookups.
SS_CONN="$GO_SDK_DIR/examples/semantic_search/connector/main.go"
if [ -f "$SS_CONN" ]; then
    sed -i "s/^const connectorName = .*/const connectorName = \"${CONNECTOR_NAME}\"/" "$SS_CONN"
    echo "[gosdk] aligned semantic_search/connector name -> \"${CONNECTOR_NAME}\""
fi

# --- Ensure modules are available (mirrors the README setup step) -------------
( cd "$GO_SDK_DIR/examples" && go mod tidy ) || {
    echo "::error::go mod tidy failed in $GO_SDK_DIR/examples"
    exit 1
}

# --- Run the SDK's own runner -------------------------------------------------
# Keep the full runner output under reports/ so it is collected into reports.zip
# (the JUnit XML only carries pass/fail; the log has the actual per-example errors).
LOG="$REPORTS_DIR/gosdk-output.log"
"$GO_SDK_DIR/examples/run.sh" "$ENV_FILE" 2>&1 | tee "$LOG"
RUN_EXIT=${PIPESTATUS[0]}

# --- Parse per-example PASS/FAIL into JUnit XML -------------------------------
python3 - "$LOG" "$XML" "$RUN_EXIT" <<'PY'
import html, re, sys

log_path, xml_path, run_exit = sys.argv[1], sys.argv[2], int(sys.argv[3])

# run.sh prints:  "  <name padded to 52>  PASS"  (or FAIL)
pat = re.compile(r'^\s{2}(\S.*?)\s+(PASS|FAIL)\s*$')
cases = []
for line in open(log_path, encoding='utf-8', errors='replace'):
    m = pat.match(line.rstrip('\n'))
    if m:
        cases.append((m.group(1).strip(), m.group(2)))

total = len(cases)
failures = sum(1 for _, r in cases if r == 'FAIL')
parts = []
for name, result in cases:
    nm = html.escape(name, quote=True)
    if result == 'FAIL':
        parts.append(
            f'    <testcase classname="gosdk" name="{nm}">'
            f'<failure message="example failed">see reports/gosdk-output.log in reports.zip</failure>'
            f'</testcase>'
        )
    else:
        parts.append(f'    <testcase classname="gosdk" name="{nm}"/>')

# If nothing parsed, record a single synthetic case reflecting the run outcome
# so the suite still shows up (and goes red) in the report.
if total == 0:
    total = 1
    if run_exit != 0:
        failures = 1
        parts.append(
            '    <testcase classname="gosdk" name="run.sh">'
            '<failure message="no example results parsed">'
            'run.sh produced no parseable PASS/FAIL lines</failure></testcase>'
        )
    else:
        parts.append('    <testcase classname="gosdk" name="run.sh"/>')

xml = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    f'<testsuite name="go-sdk-examples" tests="{total}" failures="{failures}" '
    f'errors="0" skipped="0">\n' + '\n'.join(parts) + '\n</testsuite>\n'
)
with open(xml_path, 'w', encoding='utf-8') as fh:
    fh.write(xml)
print(f"[gosdk] wrote {xml_path}: tests={total} failures={failures} run_exit={run_exit}")
PY

exit "$RUN_EXIT"
