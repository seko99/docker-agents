#!/usr/bin/env bash

set -euo pipefail

load_env_file() {
  local env_file="$1"

  if [[ -f "$env_file" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$env_file"
    set +a
  fi
}

usage() {
  cat <<'EOF'
Usage: ./do-task.sh [--plan] [--implement] [--review] [--all] <jira-browse-url|jira-issue-key>

Required environment variables:
  JIRA_API_KEY    Jira API key used in Authorization: Bearer <token> for --plan

Optional environment variables:
  JIRA_BASE_URL   Jira base URL like https://jira.example.ru
                  Required when passing only a Jira issue key like MON-3288
EOF
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

extract_issue_key() {
  local jira_ref="$1"
  local normalized_ref issue_key

  normalized_ref="${jira_ref%/}"

  if [[ "$normalized_ref" == *"://"* ]]; then
    issue_key="${normalized_ref##*/}"

    if [[ "$normalized_ref" != */browse/* ]] || [[ -z "$issue_key" ]]; then
      echo "Expected Jira browse URL like https://jira.example.ru/browse/MON-3288" >&2
      exit 1
    fi

    printf '%s\n' "$issue_key"
    return
  fi

  issue_key="$normalized_ref"

  if [[ ! "$issue_key" =~ ^[A-Z][A-Z0-9_]*-[0-9]+$ ]]; then
    echo "Expected Jira issue key like MON-3288 or browse URL like https://jira.example.ru/browse/MON-3288" >&2
    exit 1
  fi

  printf '%s\n' "$issue_key"
}

build_jira_browse_url() {
  local jira_ref="$1"
  local issue_key base_url

  if [[ "$jira_ref" == *"://"* ]]; then
    printf '%s\n' "${jira_ref%/}"
    return
  fi

  if [[ -z "${JIRA_BASE_URL:-}" ]]; then
    echo "JIRA_BASE_URL is required when passing only a Jira issue key." >&2
    exit 1
  fi

  issue_key="$(extract_issue_key "$jira_ref")"
  base_url="${JIRA_BASE_URL%/}"

  printf '%s/browse/%s\n' "$base_url" "$issue_key"
}

build_jira_api_url() {
  local jira_ref="$1"
  local browse_url base_url issue_key

  browse_url="$(build_jira_browse_url "$jira_ref")"
  issue_key="$(extract_issue_key "$jira_ref")"
  base_url="${browse_url%/browse/*}"

  printf '%s/rest/api/2/issue/%s\n' "$base_url" "$issue_key"
}

fetch_jira_issue() {
  local jira_browse_url="$1"
  local jira_api_url="$2"
  local jira_task_file="$3"

  if [[ -z "${JIRA_API_KEY:-}" ]]; then
    echo "JIRA_API_KEY is required for plan mode." >&2
    exit 1
  fi

  require_cmd curl

  echo "Fetching Jira issue from browse URL: $jira_browse_url"
  echo "Resolved Jira API URL: $jira_api_url"
  echo "Saving Jira issue JSON to: $jira_task_file"
  curl --fail --silent --show-error \
    --header "Authorization: Bearer $JIRA_API_KEY" \
    --header "Accept: application/json" \
    --output "$jira_task_file" \
    "$jira_api_url"
}

require_jira_task_file() {
  local jira_task_file="$1"

  if [[ ! -f "$jira_task_file" ]]; then
    echo "Jira issue JSON not found: $jira_task_file" >&2
    echo "Run plan mode first to download the Jira task." >&2
    exit 1
  fi
}

load_env_file ".env"

run_plan=false
run_implement=false
run_review=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --plan)
      run_plan=true
      shift
      ;;
    --implement)
      run_implement=true
      shift
      ;;
    --review)
      run_review=true
      shift
      ;;
    --all)
      run_plan=true
      run_implement=true
      run_review=true
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    --*)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
    *)
      break
      ;;
  esac
done

if [[ $# -ne 1 ]]; then
  usage >&2
  exit 1
fi

if [[ "$run_plan" == true || "$run_implement" == true ]]; then
  require_cmd codex
fi

if [[ "$run_review" == true ]]; then
  require_cmd claude
fi

jira_ref="$1"
issue_key="$(extract_issue_key "$jira_ref")"
jira_browse_url="$(build_jira_browse_url "$jira_ref")"
jira_api_url="$(build_jira_api_url "$jira_ref")"
jira_task_file="./${issue_key}.json"

export JIRA_BROWSE_URL="$jira_browse_url"
export JIRA_API_URL="$jira_api_url"
export JIRA_TASK_FILE="$jira_task_file"

CODEX_PLAN_PROMPT="Посмотри и проанализируй задачу в $JIRA_TASK_FILE. Разработай системный дизайн решения, запиши в design-1.md. Разработай подробный план реализации и запиши его в plan-1.md.}"
CODEX_IMPLEMENT_PROMPT="Проанализируй системный дизайн design-1.md, план реализации plan-1.md и приступай к реализации по плану. По окончании обязательно прогони вне песочницы линтер, все тесты, сгенерируй make swagger. Исправь ошибки линтера и тестов, если будут."
CLAUDE_REVIEW_PROMPT="Проведи код-ревью текущей ветки against dev. Сверься с задачей в $JIRA_TASK_FILE, дизайном design-1.md и планом plan-1.md. Замечания и комментарии запиши в review-1.md."
CODEX_REVIEW_REPLY_PROMPT="Твой коллега провёл код-ревью и записал комментарии в review-1.md. Проанализируй комментарии к код-ревью, сверься с задачей в $JIRA_TASK_FILE, дизайном design-1.md, планом plan-1.md и запиши свои комментарии в review-reply-1.md."

if [[ "$run_plan" != true && "$run_implement" != true && "$run_review" != true ]]; then
  echo "No execution mode selected."
  exit 0
fi

if [[ "$run_plan" == true ]]; then
  fetch_jira_issue "$jira_browse_url" "$jira_api_url" "$jira_task_file"
  echo "Running Codex planning mode"
  codex exec --full-auto "$CODEX_PLAN_PROMPT"
fi

if [[ "$run_implement" == true ]]; then
  require_jira_task_file "$jira_task_file"
  echo "Running Codex implementation mode"
  #codex exec --full-auto "$CODEX_IMPLEMENT_PROMPT"
  docker-compose -f ~/RemoteProjects/ai/docker-agents/docker-compose.yml run --rm codex-exec "$CODEX_IMPLEMENT_PROMPT"
fi

if [[ "$run_review" == true ]]; then
  require_jira_task_file "$jira_task_file"
  echo "Running Claude review mode"
  claude -p --permission-mode auto "$CLAUDE_REVIEW_PROMPT"
  codex exec --full-auto "$CODEX_REVIEW_REPLY_PROMPT"
fi
