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

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.styles import Style
    from rich.console import Console
    from rich.panel import Panel
except ImportError as exc:
    print(
        "Missing Python dependencies. Activate the virtualenv with "
        "'source ~/venvs/do-test/bin/activate' and install requirements.txt.",
        file=sys.stderr,
    )
    raise SystemExit(1) from exc


DEFAULT_DOCKER_COMPOSE_FILE = "/home/seko/RemoteProjects/ai/docker-agents/docker-compose.yml"
COMMANDS = ("plan", "implement", "review", "review-fix", "test")
ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*-[0-9]+$")
REVIEW_FILE_RE = re.compile(r"^review-(.+)-(\d+)\.md$")
REVIEW_REPLY_FILE_RE = re.compile(r"^review-reply-(.+)-(\d+)\.md$")
DEFAULT_CODEX_MODEL = "gpt-5.4"
DEFAULT_CLAUDE_REVIEW_MODEL = "opus"
DEFAULT_CLAUDE_SUMMARY_MODEL = "haiku"
HISTORY_FILE = Path("/home/seko/.codex/memories/do-task-history")
READY_TO_MERGE_FILE = "ready-to-merge.md"
console = Console()
error_console = Console(stderr=True)

# Prompts
BASE_PROMPT_HEADER = "Основная задача:"
EXTRA_PROMPT_HEADER = "Дополнительные указания:"
PLAN_PROMPT_TEMPLATE = (
    "Посмотри и проанализируй задачу в {jira_task_file}. "
    "Разработай системный дизайн решения, запиши в {design_file}. "
    "Разработай подробный план реализации и запиши его в {plan_file}. "
    "Разработай план тестирования для QA и запиши в {qa_file}. "
)
IMPLEMENT_PROMPT_TEMPLATE = (
    "Проанализируй системный дизайн {design_file}, план реализации {plan_file} и приступай к реализации по плану. "
    "По окончании обязательно прогони вне песочницы линтер, все тесты, сгенерируй make swagger. "
    "Исправь ошибки линтера и тестов, если будут."
)
REVIEW_PROMPT_TEMPLATE = (
    "Проведи код-ревью текущих изменений. "
    "Сверься с задачей в {jira_task_file}, дизайном {design_file} и планом {plan_file}. "
    "Замечания и комментарии запиши в {review_file}. "
    "Если больше нет блокеров, препятствующих merge - создай файл ready-to-merge.md."
)
REVIEW_REPLY_PROMPT_TEMPLATE = (
    "Твой коллега провёл код-ревью и записал комментарии в {review_file}. "
    "Проанализируй комментарии к код-ревью, сверься с задачей в {jira_task_file}, "
    "дизайном {design_file}, планом {plan_file} и запиши свои комментарии в {review_reply_file}."
)
REVIEW_SUMMARY_PROMPT_TEMPLATE = (
    "Посмотри в {review_file}. "
    "Сделай краткий список комментариев без подробностей, 3-7 пунктов. "
    "Запиши результат в {review_summary_file}."
)
REVIEW_REPLY_SUMMARY_PROMPT_TEMPLATE = (
    "Посмотри в {review_reply_file}. "
    "Сделай краткий список ответов и итоговых действий без подробностей, 3-7 пунктов. "
    "Запиши результат в {review_reply_summary_file}."
)
REVIEW_FIX_PROMPT_TEMPLATE = (
    "Проанализируй комментарии в {review_reply_file}. "
    "Исправь то, что содержится в дополнительных указаниях, а если таковых нет - исправь все пункты. "
    "По окончании обязательно прогони вне песочницы линтер, все тесты, сгенерируй make swagger. "
    "Исправь ошибки линтера и тестов, если будут. "
    "По завершении резюме запиши в {review_fix_file}."
)
TASK_SUMMARY_PROMPT_TEMPLATE = (
    "Посмотри в {jira_task_file}. "
    "Сделай краткое резюме задачи, на 1-2 абзаца, "
    "запиши в {task_summary_file}."
)


class TaskRunnerError(Exception):
    pass


@dataclass
class Config:
    command: str
    jira_ref: str
    review_fix_points: str | None
    extra_prompt: str | None
    dry_run: bool
    verbose: bool
    docker_compose_file: str
    docker_compose_cmd: list[str]
    codex_cmd: str
    claude_cmd: str
    jira_issue_key: str
    task_key: str
    jira_browse_url: str
    jira_api_url: str
    jira_task_file: str


def artifact_file(prefix: str, task_key: str, iteration: int) -> str:
    return f"{prefix}-{task_key}-{iteration}.md"


def design_file(task_key: str) -> str:
    return artifact_file("design", task_key, 1)


def plan_file(task_key: str) -> str:
    return artifact_file("plan", task_key, 1)


def qa_file(task_key: str) -> str:
    return artifact_file("qa", task_key, 1)


def task_summary_file(task_key: str) -> str:
    return artifact_file("task", task_key, 1)


def plan_artifacts(task_key: str) -> tuple[str, ...]:
    return (design_file(task_key), plan_file(task_key), qa_file(task_key))


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
  ./do-task.py <jira-browse-url|jira-issue-key>
  ./do-task.py --force <jira-browse-url|jira-issue-key>
  ./do-task.py plan [--dry] [--verbose] [--prompt <text>] <jira-browse-url|jira-issue-key>
  ./do-task.py implement [--dry] [--verbose] [--prompt <text>] <jira-browse-url|jira-issue-key>
  ./do-task.py review [--dry] [--verbose] [--prompt <text>] <jira-browse-url|jira-issue-key>
  ./do-task.py review-fix [--dry] [--verbose] [--prompt <text>] <jira-browse-url|jira-issue-key>
  ./do-task.py test [--dry] [--verbose] <jira-browse-url|jira-issue-key>

Interactive Mode:
  When started with only a Jira task, the script opens an interactive shell.
  Available slash commands: /plan, /implement, /review, /review-fix, /test, /help, /exit

Flags:
  --force         In interactive mode, force refresh Jira task and task summary
  --dry           Fetch Jira task, but print docker/codex/claude commands
                  instead of executing them
  --verbose       Show live stdout/stderr of launched commands
  --prompt        Extra prompt text appended to the base prompt

Required environment variables:
  JIRA_API_KEY    Jira API key used in Authorization: Bearer <token> for plan

Optional environment variables:
  JIRA_BASE_URL   Jira base URL like https://jira.example.ru
                  Required when passing only a Jira issue key like DEMO-3288
  DOCKER_COMPOSE_FILE
                  Path to docker-compose.yml for docker-based modes
  DOCKER_COMPOSE_BIN
                  Explicit docker compose command, for example "docker compose"
                  or "docker-compose"
  CODEX_BIN       Explicit path to codex binary
  CODEX_MODEL     Codex model for local and docker exec runs, defaults to "gpt-5.4"
  CLAUDE_BIN      Explicit path to claude binary
  CLAUDE_REVIEW_MODEL
                  Claude model for review runs, defaults to "opus"
  CLAUDE_SUMMARY_MODEL
                  Claude model for summary runs, defaults to "haiku"
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False, usage=usage())
    parser.add_argument("--help", "-h", action="store_true")
    subparsers = parser.add_subparsers(dest="command")

    for command_name in COMMANDS:
        subparser = subparsers.add_parser(command_name, add_help=False)
        subparser.add_argument("--dry", action="store_true")
        subparser.add_argument("--verbose", action="store_true")
        subparser.add_argument("--prompt")
        subparser.add_argument("--help", "-h", action="store_true")
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
                "Expected Jira browse URL like https://jira.example.ru/browse/DEMO-3288"
            )
        return issue_key

    issue_key = normalized_ref
    if not ISSUE_KEY_RE.match(issue_key):
        raise TaskRunnerError(
            "Expected Jira issue key like DEMO-3288 or browse URL like https://jira.example.ru/browse/DEMO-3288"
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
        raise TaskRunnerError(f"{message}\nMissing files: {', '.join(missing)}")


def next_review_iteration_for_task(workdir: Path, task_key: str) -> int:
    max_index = 0
    for entry in workdir.iterdir():
        if not entry.is_file():
            continue
        match = REVIEW_FILE_RE.match(entry.name) or REVIEW_REPLY_FILE_RE.match(entry.name)
        if match and match.group(1) == task_key:
            max_index = max(max_index, int(match.group(2)))
    return max_index + 1


def latest_review_reply_iteration(workdir: Path, task_key: str) -> int | None:
    max_index: int | None = None
    for entry in workdir.iterdir():
        if not entry.is_file():
            continue
        match = REVIEW_REPLY_FILE_RE.match(entry.name)
        if match and match.group(1) == task_key:
            current = int(match.group(2))
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


def format_prompt(base_prompt: str, extra_prompt: str | None = None) -> str:
    sections = [f"{BASE_PROMPT_HEADER}\n{base_prompt.strip()}"]

    if extra_prompt and extra_prompt.strip():
        sections.append(f"{EXTRA_PROMPT_HEADER}\n{extra_prompt.strip()}")

    return "\n\n".join(sections)


def run_codex_in_docker(
    config: Config,
    docker_compose_cmd: list[str],
    prompt: str,
    *,
    label_text: str,
) -> None:
    docker_env = os.environ.copy()
    docker_env["CODEX_PROMPT"] = prompt
    docker_env["CODEX_EXEC_FLAGS"] = (
        f"--model {shlex.quote(codex_model())} --dangerously-bypass-approvals-and-sandbox"
    )

    print_info(label_text)
    print_prompt("Codex", prompt)
    run_command(
        docker_compose_cmd
        + [
            "-f",
            config.docker_compose_file,
            "run",
            "--rm",
            "codex-exec",
        ],
        env=docker_env,
        dry_run=config.dry_run,
        verbose=config.verbose,
        label=f"codex:{codex_model()}",
    )


def run_verify_build_in_docker(
    config: Config,
    docker_compose_cmd: list[str],
    *,
    label_text: str,
) -> None:
    print_info(label_text)
    try:
        run_command(
            docker_compose_cmd
            + [
                "-f",
                config.docker_compose_file,
                "run",
                "--rm",
                "verify-build",
            ],
            env=os.environ.copy(),
            dry_run=config.dry_run,
            verbose=config.verbose,
            label="verify-build",
        )
    except subprocess.CalledProcessError as exc:
        print_error(f"Build verification failed with exit code {exc.returncode}")
        raise


def print_prompt(tool_name: str, prompt: str) -> None:
    console.print(Panel(prompt, title=f"{tool_name} Prompt", border_style="blue"))


def print_info(message: str) -> None:
    console.print(f"[bold cyan]{message}[/]")


def print_error(message: str) -> None:
    error_console.print(f"[bold red]{message}[/]")


def print_summary(title: str, text: str) -> None:
    console.print(Panel(text.strip() or "Empty summary", title=title, border_style="yellow"))


def print_ready_to_merge() -> None:
    console.print(
        Panel(
            "[bold green]Изменения готовы к merge[/]\nФайл ready-to-merge.md создан.",
            title="Ready To Merge",
            border_style="green",
        )
    )


def codex_model() -> str:
    return os.environ.get("CODEX_MODEL", DEFAULT_CODEX_MODEL).strip() or DEFAULT_CODEX_MODEL


def claude_review_model() -> str:
    return os.environ.get("CLAUDE_REVIEW_MODEL", DEFAULT_CLAUDE_REVIEW_MODEL).strip() or DEFAULT_CLAUDE_REVIEW_MODEL


def claude_summary_model() -> str:
    return os.environ.get("CLAUDE_SUMMARY_MODEL", DEFAULT_CLAUDE_SUMMARY_MODEL).strip() or DEFAULT_CLAUDE_SUMMARY_MODEL


def run_command(
    argv: list[str],
    env: dict[str, str] | None = None,
    dry_run: bool = False,
    verbose: bool = False,
    label: str | None = None,
) -> None:
    if dry_run:
        console.print(format_command(argv, env))
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

    def collect_output() -> None:
        assert process.stdout is not None
        for chunk in process.stdout:
            output_chunks.append(chunk)

    reader = threading.Thread(target=collect_output, daemon=True)
    reader.start()

    started_at = time.monotonic()
    status_label = label or (Path(argv[0]).name or argv[0])
    with console.status("", spinner="dots") as status:
        while process.poll() is None:
            elapsed = format_duration(time.monotonic() - started_at)
            status.update(f"[cyan]{status_label}[/] [dim]{elapsed}[/]")
            time.sleep(0.2)

    reader.join()
    elapsed = format_duration(time.monotonic() - started_at)
    console.print(f"[green]Done[/] {elapsed}")

    output = "".join(output_chunks)
    if process.returncode != 0:
        if output:
            sys.stderr.write(output)
            if not output.endswith("\n"):
                sys.stderr.write("\n")
        raise subprocess.CalledProcessError(process.returncode or 1, argv)


def build_config(
    command: str,
    jira_ref: str,
    *,
    review_fix_points: str | None = None,
    extra_prompt: str | None = None,
    dry_run: bool = False,
    verbose: bool = False,
) -> Config:
    if command not in COMMANDS:
        raise TaskRunnerError(f"Unsupported command: {command}")

    jira_issue_key = extract_issue_key(jira_ref)
    jira_browse_url = build_jira_browse_url(jira_ref)
    jira_api_url = build_jira_api_url(jira_ref)
    jira_task_file = f"./{jira_issue_key}.json"

    return Config(
        command=command,
        jira_ref=jira_ref,
        review_fix_points=review_fix_points,
        extra_prompt=extra_prompt,
        dry_run=dry_run,
        verbose=verbose,
        docker_compose_file=os.environ.get("DOCKER_COMPOSE_FILE", DEFAULT_DOCKER_COMPOSE_FILE),
        docker_compose_cmd=[],
        codex_cmd=os.environ.get("CODEX_BIN", "codex"),
        claude_cmd=os.environ.get("CLAUDE_BIN", "claude"),
        jira_issue_key=jira_issue_key,
        task_key=jira_issue_key,
        jira_browse_url=jira_browse_url,
        jira_api_url=jira_api_url,
        jira_task_file=jira_task_file,
    )


def check_prerequisites(config: Config) -> tuple[str, str, list[str]]:
    codex_cmd = config.codex_cmd
    claude_cmd = config.claude_cmd
    docker_compose_cmd = config.docker_compose_cmd

    if config.command in {"plan", "review"}:
        codex_cmd = resolve_cmd("codex", "CODEX_BIN")

    if config.command == "review":
        claude_cmd = resolve_cmd("claude", "CLAUDE_BIN")

    if config.command in {"implement", "review-fix", "test"}:
        docker_compose_cmd = resolve_docker_compose_cmd()
        if not Path(config.docker_compose_file).is_file():
            raise TaskRunnerError(f"docker-compose file not found: {config.docker_compose_file}")

    return codex_cmd, claude_cmd, docker_compose_cmd


def execute_command(config: Config) -> None:
    codex_cmd, claude_cmd, docker_compose_cmd = check_prerequisites(config)

    os.environ["JIRA_BROWSE_URL"] = config.jira_browse_url
    os.environ["JIRA_API_URL"] = config.jira_api_url
    os.environ["JIRA_TASK_FILE"] = config.jira_task_file

    plan_prompt = format_prompt(
        PLAN_PROMPT_TEMPLATE.format(
            jira_task_file=config.jira_task_file,
            design_file=design_file(config.task_key),
            plan_file=plan_file(config.task_key),
            qa_file=qa_file(config.task_key),
        ),
        config.extra_prompt,
    )
    implement_prompt = format_prompt(
        IMPLEMENT_PROMPT_TEMPLATE.format(
            design_file=design_file(config.task_key),
            plan_file=plan_file(config.task_key),
        ),
        config.extra_prompt,
    )

    if config.command == "plan":
        if config.verbose:
            console.print(f"Fetching Jira issue from browse URL: {config.jira_browse_url}")
            console.print(f"Resolved Jira API URL: {config.jira_api_url}")
            console.print(f"Saving Jira issue JSON to: {config.jira_task_file}")
        fetch_jira_issue(config.jira_api_url, config.jira_task_file)
        print_info("Running Codex planning mode")
        print_prompt("Codex", plan_prompt)
        run_command(
            [codex_cmd, "exec", "--model", codex_model(), "--full-auto", plan_prompt],
            env=os.environ.copy(),
            dry_run=config.dry_run,
            verbose=config.verbose,
            label=f"codex:{codex_model()}",
        )
        require_artifacts(
            plan_artifacts(config.task_key),
            "Plan mode did not produce the required artifacts.",
        )
        return

    if config.command == "implement":
        require_jira_task_file(config.jira_task_file)
        require_artifacts(
            plan_artifacts(config.task_key),
            "Implement mode requires plan artifacts from the planning phase.",
        )
        run_codex_in_docker(
            config,
            docker_compose_cmd,
            implement_prompt,
            label_text="Running Codex implementation mode in isolated Docker",
        )
        run_verify_build_in_docker(
            config,
            docker_compose_cmd,
            label_text="Running build verification in isolated Docker",
        )
        return

    if config.command == "review":
        require_jira_task_file(config.jira_task_file)
        require_artifacts(
            plan_artifacts(config.task_key),
            "Review mode requires plan artifacts from the planning phase.",
        )
        iteration = next_review_iteration_for_task(Path.cwd(), config.task_key)
        review_file = artifact_file("review", config.task_key, iteration)
        review_reply_file = artifact_file("review-reply", config.task_key, iteration)
        review_summary_file = artifact_file("review-summary", config.task_key, iteration)
        review_reply_summary_file = artifact_file("review-reply-summary", config.task_key, iteration)
        claude_prompt = format_prompt(
            REVIEW_PROMPT_TEMPLATE.format(
                jira_task_file=config.jira_task_file,
                design_file=design_file(config.task_key),
                plan_file=plan_file(config.task_key),
                review_file=review_file,
            ),
            config.extra_prompt,
        )
        codex_reply_prompt = format_prompt(
            REVIEW_REPLY_PROMPT_TEMPLATE.format(
                review_file=review_file,
                jira_task_file=config.jira_task_file,
                design_file=design_file(config.task_key),
                plan_file=plan_file(config.task_key),
                review_reply_file=review_reply_file,
            ),
            config.extra_prompt,
        )

        print_info(f"Running Claude review mode (iteration {iteration})")
        print_prompt("Claude", claude_prompt)
        run_command(
            [
                claude_cmd,
                "--model",
                claude_review_model(),
                "-p",
                "--allowedTools=Read,Write,Edit",
                "--output-format",
                "stream-json",
                "--verbose",
                "--include-partial-messages",
                claude_prompt,
            ],
            env=os.environ.copy(),
            dry_run=config.dry_run,
            verbose=config.verbose,
            label=f"claude:{claude_review_model()}",
        )
        if not config.dry_run:
            require_artifacts(
                [review_file],
                "Claude review did not produce the required review artifact.",
            )
            review_summary_prompt = REVIEW_SUMMARY_PROMPT_TEMPLATE.format(
                review_file=review_file,
                review_summary_file=review_summary_file,
            )
            review_summary_text = run_claude_summary(
                claude_cmd,
                review_summary_file,
                review_summary_prompt,
                verbose=config.verbose,
            )
            print_summary("Claude Comments", review_summary_text)
        print_info(f"Running Codex review reply mode (iteration {iteration})")
        print_prompt("Codex", codex_reply_prompt)
        run_command(
            [codex_cmd, "exec", "--model", codex_model(), "--full-auto", codex_reply_prompt],
            env=os.environ.copy(),
            dry_run=config.dry_run,
            verbose=config.verbose,
            label=f"codex:{codex_model()}",
        )
        if not config.dry_run:
            require_artifacts(
                [review_reply_file],
                "Codex review reply did not produce the required review-reply artifact.",
            )
            review_reply_summary_prompt = REVIEW_REPLY_SUMMARY_PROMPT_TEMPLATE.format(
                review_reply_file=review_reply_file,
                review_reply_summary_file=review_reply_summary_file,
            )
            review_reply_summary_text = run_claude_summary(
                claude_cmd,
                review_reply_summary_file,
                review_reply_summary_prompt,
                verbose=config.verbose,
            )
            print_summary("Codex Reply Summary", review_reply_summary_text)
            if Path(READY_TO_MERGE_FILE).is_file():
                print_ready_to_merge()
        return

    if config.command == "review-fix":
        require_jira_task_file(config.jira_task_file)
        require_artifacts(
            plan_artifacts(config.task_key),
            "Review-fix mode requires plan artifacts from the planning phase.",
        )
        latest_iteration = latest_review_reply_iteration(Path.cwd(), config.task_key)
        if latest_iteration is None:
            raise TaskRunnerError(
                f"Review-fix mode requires at least one review-reply-{config.task_key}-N.md artifact."
            )

        review_reply_file = artifact_file("review-reply", config.task_key, latest_iteration)
        review_fix_file = artifact_file("review-fix", config.task_key, latest_iteration)
        review_fix_prompt = format_prompt(
            REVIEW_FIX_PROMPT_TEMPLATE.format(
                review_reply_file=review_reply_file,
                items=config.review_fix_points,
                review_fix_file=review_fix_file,
            ),
            config.extra_prompt,
        )

        run_codex_in_docker(
            config,
            docker_compose_cmd,
            review_fix_prompt,
            label_text=f"Running Codex review-fix mode in isolated Docker (iteration {latest_iteration})",
        )
        if not config.dry_run:
            require_artifacts(
                [review_fix_file],
                "Review-fix mode did not produce the required review-fix artifact.",
            )
        run_verify_build_in_docker(
            config,
            docker_compose_cmd,
            label_text="Running build verification in isolated Docker",
        )
        return

    if config.command == "test":
        require_jira_task_file(config.jira_task_file)
        require_artifacts(
            plan_artifacts(config.task_key),
            "Test mode requires plan artifacts from the planning phase.",
        )
        run_verify_build_in_docker(
            config,
            docker_compose_cmd,
            label_text="Running build verification in isolated Docker",
        )
        return

    raise TaskRunnerError(f"Unsupported command: {config.command}")


def run_claude_summary(
    claude_cmd: str,
    output_file: str,
    prompt: str,
    *,
    verbose: bool = False,
) -> str:
    print_info(f"Preparing summary in {output_file}")
    print_prompt("Claude", prompt)
    run_command(
        [
            claude_cmd,
            "--model",
            claude_summary_model(),
            "-p",
            "--allowedTools=Read,Write,Edit",
            prompt,
        ],
        env=os.environ.copy(),
        dry_run=False,
        verbose=verbose,
        label=f"claude:{claude_summary_model()}",
    )
    require_artifacts([output_file], f"Claude summary did not produce {output_file}.")
    summary_text = Path(output_file).read_text(encoding="utf-8").strip()
    return summary_text


def summarize_task(jira_ref: str) -> tuple[str, str]:
    config = build_config("plan", jira_ref)
    claude_cmd = resolve_cmd("claude", "CLAUDE_BIN")

    fetch_jira_issue(config.jira_api_url, config.jira_task_file)

    summary_prompt = TASK_SUMMARY_PROMPT_TEMPLATE.format(
        jira_task_file=config.jira_task_file,
        task_summary_file=task_summary_file(config.task_key),
    )

    summary_text = run_claude_summary(claude_cmd, task_summary_file(config.task_key), summary_prompt)
    return config.jira_issue_key, summary_text


def resolve_task_identity(jira_ref: str) -> tuple[str, str]:
    config = build_config("plan", jira_ref)
    summary_path = Path(task_summary_file(config.task_key))
    summary_text = summary_path.read_text(encoding="utf-8").strip() if summary_path.is_file() else ""
    return config.jira_issue_key, summary_text


def parse_cli_args(argv: list[str]) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.help:
        console.print(usage())
        raise SystemExit(0)

    if not getattr(args, "command", None):
        console.print(usage(), file=sys.stderr)
        raise SystemExit(1)

    if getattr(args, "help", False) and not getattr(args, "jira_ref", None):
        console.print(usage())
        raise SystemExit(0)

    if not args.jira_ref:
        console.print(usage(), file=sys.stderr)
        raise SystemExit(1)

    return args


def build_config_from_args(args: argparse.Namespace) -> Config:
    return build_config(
        args.command,
        args.jira_ref,
        extra_prompt=getattr(args, "prompt", None),
        dry_run=args.dry,
        verbose=args.verbose,
    )


def interactive_help() -> None:
    console.print(
        Panel(
            "/plan [extra prompt]\n"
            "/implement [extra prompt]\n"
            "/review [extra prompt]\n"
            "/review-fix [extra prompt]\n"
            "/test\n"
            "/help\n"
            "/exit",
            title="Interactive Commands",
            border_style="magenta",
        )
    )


def parse_interactive_command(line: str, jira_ref: str) -> Config | None:
    try:
        parts = shlex.split(line)
    except ValueError as exc:
        raise TaskRunnerError(f"Cannot parse command: {exc}") from exc

    if not parts:
        return None

    command = parts[0]
    if not command.startswith("/"):
        raise TaskRunnerError("Interactive mode expects slash commands. Use /help.")

    command_name = command[1:]
    if command_name == "help":
        interactive_help()
        return None
    if command_name in {"exit", "quit"}:
        raise EOFError
    if command_name not in COMMANDS:
        raise TaskRunnerError(f"Unknown command: {command}")

    return build_config(
        command_name,
        jira_ref,
        extra_prompt=" ".join(parts[1:]) or None,
    )


def run_interactive(jira_ref: str, *, force_refresh: bool = False) -> int:
    config = build_config("plan", jira_ref)
    jira_task_path = Path(config.jira_task_file)

    if force_refresh or not jira_task_path.is_file():
        issue_key, summary_text = summarize_task(jira_ref)
    else:
        issue_key, summary_text = resolve_task_identity(jira_ref)

    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    bindings = KeyBindings()

    @bindings.add("tab")
    def _(event) -> None:
        buffer = event.app.current_buffer
        if buffer.complete_state:
            buffer.complete_next()
        else:
            buffer.start_completion(select_first=True)

    session = PromptSession(
        completer=WordCompleter(
            ["/plan", "/implement", "/review", "/review-fix", "/test", "/help", "/exit"],
            ignore_case=True,
            pattern=re.compile(r"[/a-zA-Z0-9_-]+"),
        ),
        history=FileHistory(str(HISTORY_FILE)),
        key_bindings=bindings,
        complete_while_typing=False,
        style=Style.from_dict({"prompt": "bold #5f87ff"}),
    )

    console.print(
        Panel(
            (
                f"Interactive mode for [bold]{issue_key}[/]\n"
                f"{summary_text}\n\n"
                "Use /help to see commands."
            )
            if summary_text
            else (
                f"Interactive mode for [bold]{issue_key}[/]\n"
                "Using existing Jira task file.\n\n"
                "Use /help to see commands."
            ),
            title="do-task",
            border_style="green",
        )
    )

    while True:
        try:
            line = session.prompt([("class:prompt", "do-task> ")])
            config = parse_interactive_command(line, jira_ref)
            if config is None:
                continue
            execute_command(config)
        except EOFError:
            console.print("[cyan]Bye[/]")
            return 0
        except KeyboardInterrupt:
            console.print()
            continue
        except TaskRunnerError as exc:
            print_error(str(exc))
        except subprocess.CalledProcessError as exc:
            print_error(f"Command failed with exit code {exc.returncode}")


def main(argv: list[str] | None = None) -> int:
    load_env_file(Path(".env"))
    argv = list(sys.argv[1:] if argv is None else argv)
    force_refresh = False

    if argv and argv[0] == "--force":
        force_refresh = True
        argv = argv[1:]

    try:
        if len(argv) == 1 and not argv[0].startswith("-") and argv[0] not in COMMANDS:
            return run_interactive(argv[0], force_refresh=force_refresh)

        args = parse_cli_args(argv)
        config = build_config_from_args(args)
        execute_command(config)
    except TaskRunnerError as exc:
        print_error(str(exc))
        return 1
    except subprocess.CalledProcessError as exc:
        print_error(f"Command failed with exit code {exc.returncode}")
        return exc.returncode or 1
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
