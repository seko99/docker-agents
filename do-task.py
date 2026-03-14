#!/usr/bin/env python3

from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_DOCKER_COMPOSE_FILE = "/home/seko/RemoteProjects/ai/docker-agents/docker-compose.yml"
ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*-[0-9]+$")
REVIEW_FILE_RE = re.compile(r"^review-(\d+)\.md$")
REVIEW_REPLY_FILE_RE = re.compile(r"^review-reply-(\d+)\.md$")
REVIEW_FIX_FILE_RE = re.compile(r"^review-fix-(\d+)\.md$")
PLAN_ARTIFACTS = ("design-1.md", "plan-1.md")


class TaskRunnerError(Exception):
    pass


@dataclass
class Config:
    command: str
    jira_ref: str
    review_fix_points: str | None
    dry_run: bool
    verbose: bool
    docker_compose_file: str
    docker_compose_cmd: list[str]
    codex_cmd: str
    claude_cmd: str
    jira_issue_key: str
    jira_browse_url: str
    jira_api_url: str
    jira_task_file: str


def load_env_file(env_file: Path) -> None:
    if not env_file.is_file():
        return

    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue

        value = value.strip()
        if value:
            try:
                parsed = shlex.split(value, posix=True)
            except ValueError:
                parsed = [value]
            value = parsed[0] if len(parsed) == 1 else value

        os.environ.setdefault(key, value)


def usage() -> str:
    return """Usage:
  ./do-task.py plan [--dry] [--verbose] <jira-browse-url|jira-issue-key>
  ./do-task.py implement [--dry] [--verbose] <jira-browse-url|jira-issue-key>
  ./do-task.py review [--dry] [--verbose] <jira-browse-url|jira-issue-key>
  ./do-task.py review-fix [--dry] [--verbose] --items <items> <jira-browse-url|jira-issue-key>

Flags:
  --dry           Fetch Jira task, but print docker/codex/claude commands
                  instead of executing them
  --verbose       Show live stdout/stderr of launched commands

Required environment variables:
  JIRA_API_KEY    Jira API key used in Authorization: Bearer <token> for --plan

Optional environment variables:
  JIRA_BASE_URL   Jira base URL like https://jira.example.ru
                  Required when passing only a Jira issue key like MON-3288
  DOCKER_COMPOSE_FILE
                  Path to docker-compose.yml for implement mode
  DOCKER_COMPOSE_BIN
                  Explicit docker compose command, for example "docker compose"
                  or "docker-compose"
  CODEX_BIN       Explicit path to codex binary
  CLAUDE_BIN      Explicit path to claude binary
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False, usage=usage())
    parser.add_argument("--help", "-h", action="store_true")
    subparsers = parser.add_subparsers(dest="command")

    for command_name in ("plan", "implement", "review", "review-fix"):
        subparser = subparsers.add_parser(command_name, add_help=False)
        subparser.add_argument("--dry", action="store_true")
        subparser.add_argument("--verbose", action="store_true")
        subparser.add_argument("--help", "-h", action="store_true")
        if command_name == "review-fix":
            subparser.add_argument("--items", required=False)
        subparser.add_argument("jira_ref", nargs="?")

    return parser


def require_cmd(cmd_name: str) -> None:
    if shutil.which(cmd_name) is None:
        raise TaskRunnerError(f"Missing required command: {cmd_name}")


def find_cmd_path(cmd_name: str, env_var_name: str) -> str | None:
    configured_path = os.environ.get(env_var_name)
    if configured_path and os.access(configured_path, os.X_OK):
        return configured_path

    candidate = shutil.which(cmd_name)
    if candidate:
        return candidate

    try:
        result = subprocess.run(
            ["bash", "-ic", f"type -a -- {shlex.quote(cmd_name)}"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None

    if result.returncode != 0:
        return None

    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith(f"{cmd_name} is aliased to "):
            alias_value = line.split(" is aliased to ", 1)[1].strip("`'")
            if os.access(alias_value, os.X_OK):
                return alias_value
            continue
        if line.startswith("/") and os.access(line, os.X_OK):
            return line

    return None


def resolve_cmd(cmd_name: str, env_var_name: str) -> str:
    candidate = find_cmd_path(cmd_name, env_var_name)
    if candidate:
        return candidate
    raise TaskRunnerError(f"Missing required command: {cmd_name}")


def require_docker_compose() -> None:
    require_cmd("docker")
    result = subprocess.run(
        ["docker", "compose", "version"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        raise TaskRunnerError("Missing required docker compose plugin")


def resolve_docker_compose_cmd() -> list[str]:
    configured = os.environ.get("DOCKER_COMPOSE_BIN", "").strip()
    if configured:
        parts = shlex.split(configured)
        if not parts:
            raise TaskRunnerError("DOCKER_COMPOSE_BIN is set but empty.")
        executable = parts[0]
        if os.path.isabs(executable):
            if os.access(executable, os.X_OK):
                return parts
        elif shutil.which(executable):
            return parts
        raise TaskRunnerError(f"Configured docker compose command is not executable: {configured}")

    if shutil.which("docker-compose"):
        return ["docker-compose"]

    require_docker_compose()
    return ["docker", "compose"]


def extract_issue_key(jira_ref: str) -> str:
    normalized_ref = jira_ref.rstrip("/")
    if "://" in normalized_ref:
        issue_key = normalized_ref.rsplit("/", 1)[-1]
        if "/browse/" not in normalized_ref or not issue_key:
            raise TaskRunnerError(
                "Expected Jira browse URL like https://jira.example.ru/browse/MON-3288"
            )
        return issue_key

    issue_key = normalized_ref
    if not ISSUE_KEY_RE.match(issue_key):
        raise TaskRunnerError(
            "Expected Jira issue key like MON-3288 or browse URL like https://jira.example.ru/browse/MON-3288"
        )
    return issue_key


def build_jira_browse_url(jira_ref: str) -> str:
    if "://" in jira_ref:
        return jira_ref.rstrip("/")

    base_url = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
    if not base_url:
        raise TaskRunnerError("JIRA_BASE_URL is required when passing only a Jira issue key.")

    return f"{base_url}/browse/{extract_issue_key(jira_ref)}"


def build_jira_api_url(jira_ref: str) -> str:
    browse_url = build_jira_browse_url(jira_ref)
    issue_key = extract_issue_key(jira_ref)
    base_url = browse_url.rsplit("/browse/", 1)[0]
    return f"{base_url}/rest/api/2/issue/{issue_key}"


def fetch_jira_issue(jira_api_url: str, jira_task_file: str) -> None:
    jira_api_key = os.environ.get("JIRA_API_KEY")
    if not jira_api_key:
        raise TaskRunnerError("JIRA_API_KEY is required for plan mode.")

    request = Request(
        jira_api_url,
        headers={
            "Authorization": f"Bearer {jira_api_key}",
            "Accept": "application/json",
        },
    )

    try:
        with urlopen(request) as response:
            Path(jira_task_file).write_bytes(response.read())
    except HTTPError as exc:
        raise TaskRunnerError(f"Failed to fetch Jira issue: HTTP {exc.code}") from exc
    except URLError as exc:
        raise TaskRunnerError(f"Failed to fetch Jira issue: {exc.reason}") from exc


def require_jira_task_file(jira_task_file: str) -> None:
    if not Path(jira_task_file).is_file():
        raise TaskRunnerError(f"Jira issue JSON not found: {jira_task_file}\nRun plan mode first to download the Jira task.")


def require_artifacts(paths: tuple[str, ...] | list[str], message: str) -> None:
    missing = [path for path in paths if not Path(path).is_file()]
    if missing:
        missing_list = ", ".join(missing)
        raise TaskRunnerError(f"{message}\nMissing files: {missing_list}")


def next_review_iteration(workdir: Path) -> int:
    max_index = 0
    for entry in workdir.iterdir():
        if not entry.is_file():
            continue
        match = REVIEW_FILE_RE.match(entry.name) or REVIEW_REPLY_FILE_RE.match(entry.name)
        if match:
            max_index = max(max_index, int(match.group(1)))
    return max_index + 1


def latest_review_reply_iteration(workdir: Path) -> int | None:
    max_index: int | None = None
    for entry in workdir.iterdir():
        if not entry.is_file():
            continue
        match = REVIEW_REPLY_FILE_RE.match(entry.name)
        if match:
            current = int(match.group(1))
            max_index = current if max_index is None else max(max_index, current)
    return max_index


def format_command(argv: list[str], env: dict[str, str] | None = None) -> str:
    env_prefix = ""
    if env:
        env_prefix = " ".join(
            f"{key}={shlex.quote(value)}"
            for key, value in env.items()
            if os.environ.get(key) != value
        )
        if env_prefix:
            env_prefix = f"{env_prefix} "
    return f"{env_prefix}{shlex.join(argv)}"


def format_duration(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    minutes, secs = divmod(total_seconds, 60)
    return f"{minutes:02d}:{secs:02d}"


def print_prompt(tool_name: str, prompt: str) -> None:
    print(f"{tool_name} prompt:")
    print(prompt)


def run_command(
    argv: list[str],
    env: dict[str, str] | None = None,
    dry_run: bool = False,
    verbose: bool = False,
) -> None:
    if dry_run:
        print(format_command(argv, env))
        return

    if verbose:
        subprocess.run(argv, check=True, env=env)
        return

    process = subprocess.Popen(
        argv,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output_chunks: list[str] = []

    def _collect_output() -> None:
        assert process.stdout is not None
        for chunk in process.stdout:
            output_chunks.append(chunk)

    reader = threading.Thread(target=_collect_output, daemon=True)
    reader.start()

    spinner_frames = "|/-\\"
    spinner_index = 0
    started_at = time.monotonic()
    while process.poll() is None:
        elapsed = time.monotonic() - started_at
        sys.stdout.write(
            f"\r{spinner_frames[spinner_index % len(spinner_frames)]} {format_duration(elapsed)}"
        )
        sys.stdout.flush()
        spinner_index += 1
        time.sleep(0.5)

    reader.join()
    elapsed = time.monotonic() - started_at
    sys.stdout.write(f"\rDone {format_duration(elapsed)}\n")
    sys.stdout.flush()

    return_code = process.returncode
    output = "".join(output_chunks)
    if return_code != 0:
        if output:
            sys.stderr.write(output)
            if not output.endswith("\n"):
                sys.stderr.write("\n")
        raise subprocess.CalledProcessError(return_code, argv)


def build_config(args: argparse.Namespace) -> Config:
    if args.help:
        print(usage(), end="")
        raise SystemExit(0)

    if not getattr(args, "command", None):
        print(usage(), file=sys.stderr, end="")
        raise SystemExit(1)

    if getattr(args, "help", False) and not getattr(args, "jira_ref", None):
        print(usage(), end="")
        raise SystemExit(0)

    if not args.jira_ref:
        print(usage(), file=sys.stderr, end="")
        raise SystemExit(1)

    if args.command == "review-fix" and not args.items:
        print(usage(), file=sys.stderr, end="")
        raise SystemExit(1)

    jira_issue_key = extract_issue_key(args.jira_ref)
    jira_browse_url = build_jira_browse_url(args.jira_ref)
    jira_api_url = build_jira_api_url(args.jira_ref)
    jira_task_file = f"./{jira_issue_key}.json"

    return Config(
        command=args.command,
        jira_ref=args.jira_ref,
        review_fix_points=getattr(args, "items", None),
        dry_run=args.dry,
        verbose=args.verbose,
        docker_compose_file=os.environ.get("DOCKER_COMPOSE_FILE", DEFAULT_DOCKER_COMPOSE_FILE),
        docker_compose_cmd=[],
        codex_cmd=os.environ.get("CODEX_BIN", "codex"),
        claude_cmd=os.environ.get("CLAUDE_BIN", "claude"),
        jira_issue_key=jira_issue_key,
        jira_browse_url=jira_browse_url,
        jira_api_url=jira_api_url,
        jira_task_file=jira_task_file,
    )


def check_prerequisites(config: Config) -> tuple[str, str, list[str]]:
    codex_cmd = config.codex_cmd
    claude_cmd = config.claude_cmd
    docker_compose_cmd = config.docker_compose_cmd

    if config.command == "plan":
        codex_cmd = resolve_cmd("codex", "CODEX_BIN")

    if config.command in {"implement", "review-fix"}:
        docker_compose_cmd = resolve_docker_compose_cmd()
        if not Path(config.docker_compose_file).is_file():
            raise TaskRunnerError(f"docker-compose file not found: {config.docker_compose_file}")

    if config.command == "review":
        claude_cmd = resolve_cmd("claude", "CLAUDE_BIN")
        codex_cmd = resolve_cmd("codex", "CODEX_BIN")

    return codex_cmd, claude_cmd, docker_compose_cmd


def main() -> int:
    load_env_file(Path(".env"))
    args = build_parser().parse_args()

    try:
        config = build_config(args)
        codex_cmd, claude_cmd, docker_compose_cmd = check_prerequisites(config)

        os.environ["JIRA_BROWSE_URL"] = config.jira_browse_url
        os.environ["JIRA_API_URL"] = config.jira_api_url
        os.environ["JIRA_TASK_FILE"] = config.jira_task_file

        codex_plan_prompt = (
            f"Посмотри и проанализируй задачу в {config.jira_task_file}. "
            "Разработай системный дизайн решения, запиши в design-1.md. "
            "Разработай подробный план реализации и запиши его в plan-1.md."
        )
        codex_implement_prompt = (
            "Проанализируй системный дизайн design-1.md, план реализации plan-1.md и приступай к реализации по плану. "
            "По окончании обязательно прогони вне песочницы линтер, все тесты, сгенерируй make swagger. "
            "Исправь ошибки линтера и тестов, если будут."
        )

        if config.command == "plan":
            if config.verbose:
                print(f"Fetching Jira issue from browse URL: {config.jira_browse_url}")
                print(f"Resolved Jira API URL: {config.jira_api_url}")
                print(f"Saving Jira issue JSON to: {config.jira_task_file}")
            fetch_jira_issue(config.jira_api_url, config.jira_task_file)
            print("Running Codex planning mode")
            print_prompt("Codex", codex_plan_prompt)
            run_command(
                [codex_cmd, "exec", "--full-auto", codex_plan_prompt],
                env=os.environ.copy(),
                dry_run=config.dry_run,
                verbose=config.verbose,
            )
            require_artifacts(
                PLAN_ARTIFACTS,
                "Plan mode did not produce the required artifacts.",
            )

        if config.command == "implement":
            require_jira_task_file(config.jira_task_file)
            require_artifacts(
                PLAN_ARTIFACTS,
                "Implement mode requires plan artifacts from the planning phase.",
            )
            print("Running Codex implementation mode")
            implement_env = os.environ.copy()
            implement_env["CODEX_PROMPT"] = codex_implement_prompt
            print_prompt("Codex", codex_implement_prompt)
            run_command(
                docker_compose_cmd
                + [
                    "-f",
                    config.docker_compose_file,
                    "run",
                    "--rm",
                    "codex-exec",
                ],
                env=implement_env,
                dry_run=config.dry_run,
                verbose=config.verbose,
            )

        if config.command == "review":
            require_jira_task_file(config.jira_task_file)
            require_artifacts(
                PLAN_ARTIFACTS,
                "Review mode requires plan artifacts from the planning phase.",
            )
            iteration = next_review_iteration(Path.cwd())
            review_file = f"review-{iteration}.md"
            review_reply_file = f"review-reply-{iteration}.md"
            claude_review_prompt = (
                "Проведи код-ревью текущей ветки against dev. "
                f"Сверься с задачей в {config.jira_task_file}, дизайном design-1.md и планом plan-1.md. "
                f"Замечания и комментарии запиши в {review_file}."
            )
            codex_review_reply_prompt = (
                f"Твой коллега провёл код-ревью и записал комментарии в {review_file}. "
                f"Проанализируй комментарии к код-ревью, сверься с задачей в {config.jira_task_file}, "
                f"дизайном design-1.md, планом plan-1.md и запиши свои комментарии в {review_reply_file}."
            )

            print(f"Running Claude review mode (iteration {iteration})")
            print_prompt("Claude", claude_review_prompt)
            run_command(
                [
                    claude_cmd,
                    "-p",
                    "--allowedTools",
                    "Read,Write,Edit",
                    "--output-format",
                    "stream-json",
                    "--verbose",
                    "--include-partial-messages",
                    claude_review_prompt,
                ],
                env=os.environ.copy(),
                dry_run=config.dry_run,
                verbose=config.verbose,
            )
            if not config.dry_run:
                require_artifacts(
                    [review_file],
                    "Claude review did not produce the required review artifact.",
                )
            print_prompt("Codex", codex_review_reply_prompt)
            run_command(
                [codex_cmd, "exec", "--full-auto", codex_review_reply_prompt],
                env=os.environ.copy(),
                dry_run=config.dry_run,
                verbose=config.verbose,
            )

        if config.command == "review-fix":
            require_jira_task_file(config.jira_task_file)
            require_artifacts(
                PLAN_ARTIFACTS,
                "Review-fix mode requires plan artifacts from the planning phase.",
            )
            latest_iteration = latest_review_reply_iteration(Path.cwd())
            if latest_iteration is None:
                raise TaskRunnerError("Review-fix mode requires at least one review-reply-N.md artifact.")

            review_reply_file = f"review-reply-{latest_iteration}.md"
            review_fix_file = f"review-fix-{latest_iteration}.md"
            review_fix_prompt = (
                f"Проанализируй комментарии в {review_reply_file}. "
                f"Давай исправим п.п. {config.review_fix_points}. "
                f"По завершении резюме запиши в {review_fix_file}."
            )

            print(f"Running Codex review-fix mode (iteration {latest_iteration})")
            fix_env = os.environ.copy()
            fix_env["CODEX_PROMPT"] = review_fix_prompt
            print_prompt("Codex", review_fix_prompt)
            run_command(
                docker_compose_cmd
                + [
                    "-f",
                    config.docker_compose_file,
                    "run",
                    "--rm",
                    "codex-exec",
                ],
                env=fix_env,
                dry_run=config.dry_run,
                verbose=config.verbose,
            )
            if not config.dry_run:
                require_artifacts(
                    [review_fix_file],
                    "Review-fix mode did not produce the required review-fix artifact.",
                )

    except TaskRunnerError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        return exc.returncode or 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
