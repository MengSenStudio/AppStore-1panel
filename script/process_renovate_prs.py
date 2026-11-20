#!/usr/bin/env python3
"""自动合并 Renovate 的 patch / minor PR，并处理冲突重试。

该脚本由 ``.github/workflows/auto_merge_renovate_prs.yml`` 调用。

设计要点（与旧实现相比）：

1. **单次快速评估，绝不阻塞等待 CI**。旧实现会对每个 PR 轮询 ``mergeable_state``
   最多 600 秒，而且会把自己这次运行产生的 pending check 也算进去，于是永远
   “检查超时”，再触发 rebase，形成死循环。
2. **事件驱动重试**：PR 事件、版本目录工作流完成、Renovate 运行完成、定时兜底，
   检查通过后自然会有一次运行来合并；没有事件时不会空转。
3. **幂等**：每个 PR 只维护一条“自动合并状态”评论（隐藏 JSON 标记），内容没变
   就不写，彻底避免旧实现每小时刷重复评论的问题（历史 PR 上有 100 条相同评论）。
4. **有界重试**：冲突 PR 通过勾选 Renovate 的 rebase/retry 复选框重试，有次数上限
   （默认 3 次）和冷却时间（默认 60 分钟），耗尽后停止并交回人工。
5. **只合并 patch / minor**。major 交给人工（Renovate 配置本来也是禁用自动合并）。

只依赖标准库，无需 pip install。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

API_ROOT = "https://api.github.com"
TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")
BASE_BRANCH = os.environ.get("BASE_BRANCH") or "custom"
SELF_CHECK_NAME = os.environ.get("SELF_CHECK_NAME", "process-renovate-prs")
MERGE_METHOD = os.environ.get("MERGE_METHOD", "auto")
MAX_REBASE_ATTEMPTS = int(os.environ.get("MAX_REBASE_ATTEMPTS", "3"))
REBASE_COOLDOWN_MINUTES = int(os.environ.get("REBASE_COOLDOWN_MINUTES", "60"))
DRY_RUN = "--dry-run" in sys.argv[1:]

# 只认这个账号维护的状态评论，避免被 PR 里的普通评论干扰
BOT_LOGINS = {"github-actions[bot]", "github-actions"}

# 运行后需要再次触发的工作流（见 workflow 文件里的说明）
PROCESS_APPS_WORKFLOW = "process-apps.yml"

STATE_MARKER_RE = re.compile(r"<!--\s*automerge-state:(?P<json>.*?)\s*-->", re.DOTALL)
REBASE_CHECKBOX_RE = re.compile(r"-\s*\[\s*\]\s*<!--\s*rebase-check\s*-->", re.IGNORECASE)
REBASE_CHECKED_RE = re.compile(r"-\s*\[\s*[xX]\s*\]\s*<!--\s*rebase-check\s*-->", re.IGNORECASE)
REBASE_TICK_RE = re.compile(r"(-\s*\[)\s*(\]\s*<!--\s*rebase-check\s*-->)", re.IGNORECASE)

# 这些 mergeable_state 表示 GitHub 允许合并（behind 只是基线前进了，没有冲突）
MERGEABLE_STATES = {"clean", "has_hooks", "behind", "unstable"}

# 统计信息，供 main() 触发后续工作流
SUMMARY = {"merged": 0, "rebased": 0, "manual": 0}


# --------------------------------------------------------------------------- #
# GitHub API
# --------------------------------------------------------------------------- #
def api(
    method: str,
    path: str,
    payload: Optional[Dict[str, Any]] = None,
    allow: Tuple[int, ...] = (),
) -> Tuple[int, Any]:
    """调用 GitHub API，做有限的限流/服务端错误重试。

    返回 ``(status_code, parsed_json)``。``allow`` 中的状态码不会抛错，
    便于调用方按业务处理（例如 405 合并冲突）。
    """
    url = path if path.startswith("http") else f"{API_ROOT}{path}"
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "appstore-renovate-automerge",
    }

    if DRY_RUN and method != "GET":
        print(f"[dry-run] {method} {path} {json.dumps(payload, ensure_ascii=False)}")
        return (200, {"dry_run": True})

    for attempt in range(4):
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                parsed = json.loads(raw) if raw else {}
            except ValueError:
                parsed = {"message": raw.decode("utf-8", "replace")}
            if error.code in allow:
                return error.code, parsed
            if error.code in (403, 429) and attempt < 3:
                # 限流：等到 reset 或 Retry-After，最多等 2 分钟
                delay = 30
                retry_after = error.headers.get("Retry-After")
                reset = error.headers.get("X-RateLimit-Reset")
                if retry_after and retry_after.isdigit():
                    delay = int(retry_after)
                elif reset and reset.isdigit():
                    delay = int(reset) - int(time.time())
                delay = max(5, min(120, delay))
                print(f"[warn] {method} {path} 被限流（{error.code}），{delay}s 后重试")
                time.sleep(delay)
                continue
            if error.code >= 500 and attempt < 2:
                time.sleep(2**attempt)
                continue
            return error.code, parsed
        except (urllib.error.URLError, TimeoutError) as error:
            if attempt < 2:
                print(f"[warn] {method} {path} 网络错误（{error}），重试")
                time.sleep(2**attempt)
                continue
            raise
    return 0, {}


def paginate(path: str, max_pages: int = 10) -> List[Dict[str, Any]]:
    """按 100 条/页读取列表接口。"""
    separator = "&" if "?" in path else "?"
    items: List[Dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        status, data = api("GET", f"{path}{separator}per_page=100&page={page}")
        if status != 200 or not isinstance(data, list):
            break
        items.extend(data)
        if len(data) < 100:
            break
    return items


# --------------------------------------------------------------------------- #
# PR 解析
# --------------------------------------------------------------------------- #
def is_renovate_pr(pr: Dict[str, Any]) -> bool:
    """是否是 Renovate / Mend 创建的 PR。"""
    author = ((pr.get("user") or {}).get("login") or "").lower()
    return "renovate" in author or "mend" in author


def parse_update_types(body: str) -> Optional[str]:
    """从 Renovate 的 PR 描述表格中解析更新类型。

    返回 ``major`` / ``minor`` / ``patch``；无法判定时返回 ``None``。
    只要有一行是 major 就返回 major（宁可少合并，也不误合并大版本）。
    """
    if not body:
        return None

    found: List[str] = []
    update_column: Optional[int] = None

    for line in body.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        normalized = [re.sub(r"[*`_\[\]]", "", cell).strip().lower() for cell in cells]

        if update_column is None:
            # 表头形如 | Package | Update | Change |，也可能多一列 Type
            if "update" in normalized and "change" in normalized:
                update_column = normalized.index("update")
            continue

        if all(re.fullmatch(r":?-{2,}:?", cell) for cell in cells if cell):
            continue  # 分隔行 |---|---|
        if update_column >= len(cells):
            continue

        value = normalized[update_column]
        match = re.search(r"\b(major|minor|patch)\b", value)
        if match:
            found.append(match.group(1))
        elif value:
            found.append("other")

    if not found:
        return None
    if "major" in found:
        return "major"
    if "minor" in found:
        return "minor"
    if "patch" in found:
        return "patch"
    return "other"


def has_rebase_checkbox(body: str) -> bool:
    return bool(REBASE_CHECKBOX_RE.search(body))


def is_rebase_checked(body: str) -> bool:
    return bool(REBASE_CHECKED_RE.search(body))


def tick_rebase_checkbox(body: str) -> Optional[str]:
    """把 Renovate 的 rebase/retry 复选框从未勾选改为勾选。"""
    if not has_rebase_checkbox(body):
        return None
    new_body, count = REBASE_TICK_RE.subn(r"\1x\2", body, count=1)
    return new_body if count else None


# --------------------------------------------------------------------------- #
# 检查状态 / 合并状态
# --------------------------------------------------------------------------- #
def check_state(commit_sha: str) -> Tuple[str, List[str]]:
    """汇总提交上的检查结果。

    返回 ``(success|pending|failure, 失败的检查名)``。忽略本工作流自己的检查，
    否则会“等自己通过”，这正是旧实现卡死的原因之一。
    """
    pending: List[str] = []
    failed: List[str] = []

    status, data = api("GET", f"/repos/{REPOSITORY}/commits/{commit_sha}/check-runs?per_page=100")
    if status == 200 and isinstance(data, dict):
        for run in data.get("check_runs") or []:
            name = run.get("name") or "check"
            if name == SELF_CHECK_NAME:
                continue
            conclusion = (run.get("conclusion") or "").lower()
            if run.get("status") != "completed":
                pending.append(name)
            elif conclusion not in ("success", "neutral", "skipped"):
                failed.append(name)

    status, data = api("GET", f"/repos/{REPOSITORY}/commits/{commit_sha}/status")
    if status == 200 and isinstance(data, dict):
        # 注意：提交上没有任何 commit status 时，GitHub 会返回 state=pending 且
        # statuses 为空。不能据此判定“检查进行中”，否则只用 Actions check-runs
        # 的 PR 会被永远挡住（这是本仓库的真实情况）。
        combined_statuses = data.get("statuses") or []
        if combined_statuses:
            state = (data.get("state") or "").lower()
            if state == "pending":
                pending.append("commit-status")
            elif state in ("failure", "error"):
                failed.append("commit-status")

    if failed:
        return "failure", sorted(set(failed))
    if pending:
        return "pending", sorted(set(pending))
    return "success", []


def get_pr_detail(pr_number: int) -> Optional[Dict[str, Any]]:
    """读取 PR 详情；mergeable 为 null 时短暂重试几次（GitHub 异步计算）。"""
    for attempt in range(3):
        status, data = api("GET", f"/repos/{REPOSITORY}/pulls/{pr_number}")
        if status != 200 or not isinstance(data, dict):
            return None
        if data.get("mergeable") is not None:
            return data
        if attempt < 2:
            time.sleep(2)
    return data if status == 200 and isinstance(data, dict) else None


# --------------------------------------------------------------------------- #
# 状态评论（幂等，一个 PR 只有一条）
# --------------------------------------------------------------------------- #
def get_state(pr_number: int) -> Tuple[Optional[int], Dict[str, Any], str]:
    """读取本脚本维护的状态评论及其 JSON 状态（只认 bot 自己的评论）。"""
    comments = paginate(f"/repos/{REPOSITORY}/issues/{pr_number}/comments")
    for comment in comments:
        author = ((comment.get("user") or {}).get("login") or "").lower()
        if author not in BOT_LOGINS:
            continue
        body = comment.get("body") or ""
        match = STATE_MARKER_RE.search(body)
        if not match:
            continue
        try:
            state = json.loads(match.group("json"))
        except ValueError:
            state = {}
        return safe_int(comment.get("id")), state if isinstance(state, dict) else {}, body
    return None, {}, ""


def safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def render_state_text(state: Dict[str, Any]) -> str:
    """把状态渲染成给人看的评论正文（前面会加隐藏 JSON 标记）。"""
    result = state.get("result")
    attempts = safe_int(state.get("attempts"))
    detail = state.get("detail") or ""
    head = (state.get("head") or "")[:7]
    updated = state.get("last_rebase") or ""
    if updated.endswith("Z"):
        updated = updated[:-1]
    updated = updated.replace("T", " ")
    if len(updated) >= 16:
        updated = f"{updated[:16]} UTC"

    titles = {
        "rebase": f"已触发 Renovate rebase/retry（第 {attempts}/{MAX_REBASE_ATTEMPTS} 次）",
        "manual": "已停止自动重试，请人工处理",
        "failure": "等待人工处理",
        "blocked": "等待人工处理",
        "merge-failed": "自动合并失败，请人工处理",
        "merged": "已自动合并 ✅",
    }
    lines = [f"### 🤖 自动合并状态：{titles.get(result, result or '未知')}"]
    if detail:
        lines.append("")
        lines.append(f"- 说明：{detail}")
    if head:
        lines.append(f"- 提交：`{head}`")
    if updated:
        lines.append(f"- 最近一次 rebase：{updated}")
    lines.append("")
    lines.append("> 本评论由工作流自动维护，不会重复发布。")
    return "\n".join(lines)


def save_state(
    pr_number: int,
    comment_id: Optional[int],
    previous: Dict[str, Any],
    new_state: Dict[str, Any],
) -> None:
    """写入状态：内容没变化时什么都不做，避免刷评论。"""
    if new_state == previous:
        return
    marker = f"<!-- automerge-state:{json.dumps(new_state, ensure_ascii=False, sort_keys=True)} -->"
    body = f"{marker}\n{render_state_text(new_state)}"
    if comment_id:
        status, data = api("PATCH", f"/repos/{REPOSITORY}/issues/comments/{comment_id}", {"body": body})
        if status != 200:
            print(f"[warn] 更新 #{pr_number} 状态评论失败: {status} {(data or {}).get('message')}")
    else:
        status, data = api("POST", f"/repos/{REPOSITORY}/issues/{pr_number}/comments", {"body": body})
        if status not in (200, 201):
            print(f"[warn] 创建 #{pr_number} 状态评论失败: {status} {(data or {}).get('message')}")


def record_state_once(
    pr_number: int,
    head_sha: str,
    update_type: str,
    result: str,
    detail: str,
) -> None:
    """记录“需要人工/检查失败”等终态，同一状态只写一次。"""
    comment_id, state, _ = get_state(pr_number)
    previous = dict(state)
    new_state = dict(previous)
    new_state.update(
        {
            "result": result,
            "head": head_sha,
            "update": update_type,
            "detail": detail,
        }
    )
    save_state(pr_number, comment_id, previous, new_state)


# --------------------------------------------------------------------------- #
# 动作
# --------------------------------------------------------------------------- #
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def within_cooldown(last_iso: Optional[str]) -> bool:
    if not last_iso:
        return False
    try:
        last = datetime.fromisoformat(last_iso.replace("Z", "+00:00"))
    except ValueError:
        return False
    return now_utc() - last < timedelta(minutes=REBASE_COOLDOWN_MINUTES)


def attempt_rebase(
    pr_number: int,
    head_sha: str,
    body: str,
    update_type: str,
) -> None:
    """冲突/阻塞时，通过勾选 Renovate 复选框触发一次有界 rebase。"""
    comment_id, state, _ = get_state(pr_number)
    previous = dict(state)
    attempts = safe_int(previous.get("attempts"))

    if attempts >= MAX_REBASE_ATTEMPTS:
        if previous.get("result") != "manual":
            new_state = dict(previous)
            new_state.update(
                {
                    "result": "manual",
                    "head": head_sha,
                    "update": update_type,
                    "detail": f"已 rebase {attempts} 次仍无法合并，请人工处理",
                }
            )
            save_state(pr_number, comment_id, previous, new_state)
            SUMMARY["manual"] += 1
        print(f"#{pr_number} 已重试 {attempts} 次，停止自动 rebase")
        return

    if within_cooldown(previous.get("last_rebase")):
        print(f"#{pr_number} 距上次 rebase 不足 {REBASE_COOLDOWN_MINUTES} 分钟，等待下一次事件")
        return

    if is_rebase_checked(body):
        print(f"#{pr_number} rebase 复选框已勾选，等待 Renovate 处理")
        return

    new_body = tick_rebase_checkbox(body)
    if not new_body:
        # 没有复选框（例如 Renovate 改版），只记录一次，交给人工
        if previous.get("result") != "manual":
            new_state = dict(previous)
            new_state.update(
                {
                    "result": "manual",
                    "head": head_sha,
                    "update": update_type,
                    "detail": "PR 中没有 rebase 复选框，请人工解决冲突",
                }
            )
            save_state(pr_number, comment_id, previous, new_state)
            SUMMARY["manual"] += 1
        print(f"#{pr_number} 没有 rebase 复选框，无法自动重试")
        return

    status, data = api("PATCH", f"/repos/{REPOSITORY}/pulls/{pr_number}", {"body": new_body})
    if status != 200:
        print(f"[warn] #{pr_number} 更新 PR 描述失败: {status} {(data or {}).get('message')}")
        return

    attempts += 1
    new_state = dict(previous)
    new_state.update(
        {
            "result": "rebase",
            "attempts": attempts,
            "head": head_sha,
            "update": update_type,
            "last_rebase": now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "detail": "已勾选 rebase/retry，等待 Renovate 重新推送",
        }
    )
    save_state(pr_number, comment_id, previous, new_state)
    SUMMARY["rebased"] += 1
    print(f"#{pr_number} 已勾选 rebase 复选框（第 {attempts}/{MAX_REBASE_ATTEMPTS} 次）")


def merge_pr(pr_number: int, head_sha: str, update_type: str) -> bool:
    """合并 PR（默认 squash，与 renovate.json 一致），并校验合并的正是评估过的提交。"""
    method = resolve_merge_method()
    payload = {"merge_method": method, "sha": head_sha}
    status, data = api(
        "PUT",
        f"/repos/{REPOSITORY}/pulls/{pr_number}/merge",
        payload,
        allow=(405, 409, 422),
    )
    if status == 200:
        SUMMARY["merged"] += 1
        print(f"#{pr_number} 已合并（{method}）")
        comment_id, state, _ = get_state(pr_number)
        if comment_id:
            previous = dict(state)
            new_state = dict(previous)
            new_state.update({"result": "merged", "head": head_sha, "detail": "已自动合并"})
            save_state(pr_number, comment_id, previous, new_state)
        return True

    message = (data or {}).get("message") or ""
    print(f"[warn] #{pr_number} 合并失败: {status} {message}")

    # 连续合并时 GitHub 偶尔返回 “Base branch was modified”，稍等重试一次即可
    if status == 405 and "again" in message.lower():
        time.sleep(2)
        status, data = api(
            "PUT",
            f"/repos/{REPOSITORY}/pulls/{pr_number}/merge",
            payload,
            allow=(405, 409, 422),
        )
        if status == 200:
            SUMMARY["merged"] += 1
            print(f"#{pr_number} 已合并（{method}，重试成功）")
            comment_id, state, _ = get_state(pr_number)
            if comment_id:
                previous = dict(state)
                new_state = dict(previous)
                new_state.update({"result": "merged", "head": head_sha, "detail": "已自动合并"})
                save_state(pr_number, comment_id, previous, new_state)
            return True
        message = (data or {}).get("message") or message

    if status == 409 or (status == 405 and "not mergeable" in message.lower()):
        # 分支在评估之后又被推送 / 计算出的可合并状态已过期：
        # 不写任何评论，下一次事件（或下一次评估）会按最新状态处理。
        return False

    record_state_once(pr_number, head_sha, update_type, "merge-failed", message or f"HTTP {status}")
    return False


def dispatch_workflow(workflow_file: str) -> None:
    """触发后续工作流（workflow_dispatch 是少数不会被 GITHUB_TOKEN 抑制的事件）。"""
    status, data = api(
        "POST",
        f"/repos/{REPOSITORY}/actions/workflows/{workflow_file}/dispatches",
        {"ref": BASE_BRANCH},
        allow=(204, 403, 404, 422),
    )
    if status in (200, 204):
        print(f"已触发工作流 {workflow_file}")
    else:
        print(f"[warn] 触发工作流 {workflow_file} 失败: {status} {(data or {}).get('message')}")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def process_pr(pr: Dict[str, Any]) -> None:
    number = int(pr["number"])
    base_ref = ((pr.get("base") or {}).get("ref")) or ""
    if base_ref != BASE_BRANCH:
        print(f"#{number} 目标分支为 {base_ref}，跳过")
        return
    if pr.get("draft"):
        print(f"#{number} 是草稿 PR，跳过")
        return

    body = pr.get("body") or ""
    update_type = parse_update_types(body)
    if update_type == "major":
        # Renovate 已把 major 标记为人工合并，这里保持安静，不再每次评论
        print(f"#{number} major 更新，保留人工合并")
        return
    if update_type not in ("minor", "patch"):
        print(f"#{number} 无法判定为 minor/patch（{update_type or 'unknown'}），跳过")
        return

    detail = get_pr_detail(number)
    if not detail or detail.get("state") != "open" or detail.get("merged"):
        return
    head_sha = ((detail.get("head") or {}).get("sha")) or ""
    if not head_sha:
        return
    mergeable = detail.get("mergeable")
    mergeable_state = (detail.get("mergeable_state") or "unknown").lower()

    # 有冲突：走有界 rebase 重试
    if mergeable is False or mergeable_state in ("dirty", "conflicting"):
        attempt_rebase(number, head_sha, body, update_type)
        return

    if mergeable is None or mergeable_state == "unknown":
        print(f"#{number} GitHub 仍在计算可合并状态，等下一次事件")
        return

    state, failing = check_state(head_sha)
    if state == "pending":
        print(f"#{number} 检查仍在进行，等待事件触发（不阻塞本次运行）")
        return
    if state == "failure":
        record_state_once(
            number,
            head_sha,
            update_type,
            "failure",
            "检查未通过：" + ", ".join(failing[:5]),
        )
        print(f"#{number} 检查未通过: {', '.join(failing[:5])}")
        return

    if mergeable_state in MERGEABLE_STATES:
        merge_pr(number, head_sha, update_type)
    elif mergeable_state == "blocked":
        record_state_once(
            number,
            head_sha,
            update_type,
            "blocked",
            "被分支保护规则阻塞（需要审核或检查未满足）",
        )
        print(f"#{number} 被分支保护阻塞")
    else:
        print(f"#{number} 未处理的可合并状态: {mergeable_state}")


def select_candidates() -> List[Dict[str, Any]]:
    """列出所有打开的 Renovate PR。

    每次运行都做一次全量评估。单个 PR 只需 2~3 次 API 调用，十来个 PR 也只要
    十几秒；而“只处理触发事件对应的 PR”会在事件密集时漏掉 PR —— GitHub 的
    concurrency 队列只保留最新一条排队运行、会取消更早的排队运行，被取消的那些
    PR 就再也等不到处理（这正是之前一批 PR 迟迟不合并的原因）。
    """
    prs = [p for p in paginate(f"/repos/{REPOSITORY}/pulls?state=open") if p.get("state") == "open"]
    candidates = [p for p in prs if is_renovate_pr(p)]
    print(f"打开中的 PR {len(prs)} 个，其中 Renovate/Mend 的 {len(candidates)} 个")
    return candidates


def resolve_merge_method() -> str:
    """按仓库允许的合并方式选择：优先 squash（与 renovate.json 一致）。"""
    global MERGE_METHOD
    if MERGE_METHOD and MERGE_METHOD != "auto":
        return MERGE_METHOD
    status, repo = api("GET", f"/repos/{REPOSITORY}")
    if status == 200 and isinstance(repo, dict):
        for method, key in (("squash", "allow_squash_merge"), ("merge", "allow_merge_commit"), ("rebase", "allow_rebase_merge")):
            if repo.get(key):
                MERGE_METHOD = method
                break
        else:
            MERGE_METHOD = "merge"
    else:
        MERGE_METHOD = "squash"
    print(f"合并方式: {MERGE_METHOD}")
    return MERGE_METHOD


def main() -> int:
    if not TOKEN:
        print("[err] 缺少 GITHUB_TOKEN", file=sys.stderr)
        return 1
    if not REPOSITORY:
        print("[err] 缺少 GITHUB_REPOSITORY", file=sys.stderr)
        return 1

    candidates = select_candidates()

    for pr in candidates:
        try:
            process_pr(pr)
        except Exception as error:  # 单个 PR 失败不影响其它 PR
            print(f"[err] 处理 #{pr.get('number')} 时出错: {error}", file=sys.stderr)

    print(
        "本次结果: 合并 {merged} 个, 触发 rebase {rebased} 个, 转人工 {manual} 个".format(**SUMMARY)
    )

    # 有合并 => 重新生成 appstore/dockge 并同步
    # （workflow_dispatch 可以绕过 GITHUB_TOKEN 的事件抑制）。
    # 勾选的 rebase 复选框由 Renovate 自己在下一次运行时处理（本仓库的 Renovate
    # 是 Mend App，没有自托管 Action 可以立即触发）。
    if SUMMARY["merged"]:
        dispatch_workflow(PROCESS_APPS_WORKFLOW)

    return 0


if __name__ == "__main__":
    sys.exit(main())
