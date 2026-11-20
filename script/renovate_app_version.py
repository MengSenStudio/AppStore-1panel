#!/usr/bin/env python3
"""把 apps/<app>/<old_version> 目录名同步成 docker-compose.yml 中“主服务”的镜像版本。

由 .github/workflows/renovate-app-version.yml 在 Renovate 推送分支后调用。

规则（相对旧版 bash 脚本的增强）：

1. 主服务识别：按 compose 中的书写顺序，取第一个“不是中间件、且标签能解析出版本”的服务。
   postgres / redis / mysql / mongo / minio / meilisearch 等中间件镜像不参与版本判定，
   因此 glean、keygate-all-in-one、medusa-all-in-one、ztnet-pg 这类应用不会再被 pg 版本带偏。
   PRIMARY_SERVICE_OVERRIDES 用于极少数“主服务不在前面”的应用。
2. 严格版本解析：只接受 1.2.3、v1.2.3、1.2.3-alpine、2026-11-20 这类标签；
   commit hash、stable-xxxx、latest、dev、main 等视为“无版本”，不改名。
3. 只改“简单版本目录”：数字/点（可带 -alpine）或日期形式的目录名。
   pg14、pg16-vectors、16-3.5、v20250725、stable-e98f035、latest 等人工命名的目录保持不动。
4. 幂等：目录名已经是目标版本时什么都不做；目标目录已存在则报错退出（不覆盖）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml

APPS_DIR = Path("apps")
COMPOSE_NAME = "docker-compose.yml"

# 中间件镜像：不参与“应用版本”的判定（按镜像仓库名匹配，忽略 registry 前缀和 tag）
MIDDLEWARE_IMAGE_RE = re.compile(
    r"(?:^|/)(?:postgres|postgresql|redis|valkey|mysql|mariadb|mongo|mongodb|memcached|"
    r"rabbitmq|clickhouse|elasticsearch|opensearch|minio|pgbouncer|pgvecto|pgvector|meilisearch)"
    r"(?:$|[/:@_-])",
    re.IGNORECASE,
)

# 主服务被误判时的显式覆盖（应用名 -> 服务名）
PRIMARY_SERVICE_OVERRIDES: Dict[str, str] = {
    # wukongim 定义在 tsdd-server 前面，但它不是应用版本来源
    "tangsengdaodao": "tsdd-server",
}

# 需要保留 -alpine 后缀的应用
ALPINE_APPS = {"postgresql"}

# 只对这些形式的目录名做自动改名（其余视为人工命名）
RENAMABLE_DIR_RE = re.compile(
    r"^(?:"
    r"\d+(?:\.\d+){0,3}(?:-alpine)?"       # 1 / 1.2 / 1.2.3 / 16.16-alpine
    r"|\d{4}-\d{2}-\d{2}(?:-alpine)?"      # 2025-04-22
    r"|\d{2}-\d{2}-\d{2}(?:-alpine)?"      # 25-08-29
    r")$"
)

# 日期版本（minio 的 RELEASE.2026-11-20T...、rsshub 的 2026-11-20、aria2 的 25-08-29）
DATE_VERSION_RE = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2}|\d{2}-\d{2}-\d{2})(?!\d)")
# 纯版本前缀：1 / 1.2 / 1.2.3 / 1.2.3-rc1 / 1.2.3.4
NUMBER_VERSION_RE = re.compile(r"^(\d+(?:\.\d+){0,3})(?=$|[-_+@.])")


def image_repo(image: str) -> str:
    """取镜像仓库名（去掉 tag / digest，保留 registry 路径）。"""
    reference = image.split("@", 1)[0]
    head, separator, tail = reference.rpartition(":")
    # registry 端口（registry:5000/team/app）里冒号后的部分仍含 “/”，不算 tag
    if separator and "/" not in tail:
        return head
    return reference


def is_middleware_image(image: str) -> bool:
    return bool(MIDDLEWARE_IMAGE_RE.search(image_repo(image)))


def extract_version(image: str, app: str) -> Optional[str]:
    """从镜像标签解析版本；无法可靠解析时返回 None。"""
    if ":" not in image:
        return None
    tag = image.rsplit(":", 1)[1]
    if "/" in tag or "@" in tag:  # 无 tag（只有 registry 端口）或 digest
        return None

    cleaned = tag[1:] if re.match(r"^v\d", tag) else tag

    date_match = DATE_VERSION_RE.search(cleaned)
    if date_match:
        version = date_match.group(1)
    else:
        number_match = NUMBER_VERSION_RE.match(cleaned)
        if not number_match:
            return None
        version = number_match.group(1)

    if app in ALPINE_APPS and "alpine" in cleaned:
        version = f"{version}-alpine"
    return version


def pick_primary_service(
    services: Dict[str, Any], app: str
) -> Tuple[Optional[str], Optional[str]]:
    """选出决定应用版本的主服务，返回 (服务名, 镜像)。"""
    override = PRIMARY_SERVICE_OVERRIDES.get(app)
    if override:
        service = services.get(override)
        if isinstance(service, dict) and service.get("image"):
            return override, str(service["image"])
        print(f"[warn] 覆盖的主服务 {override} 不存在，回退到自动识别")

    fallback: Optional[Tuple[str, str]] = None
    for name, service in services.items():
        if not isinstance(service, dict):
            continue
        image = service.get("image")
        if not image:
            continue
        image = str(image)
        if extract_version(image, app) is None:
            continue  # 解析不出版本的 sidecar（如 browserless:latest）不参与
        if not is_middleware_image(image):
            return name, image
        if fallback is None:
            fallback = (name, image)
    return fallback if fallback else (None, None)


def load_services(compose_file: Path) -> Dict[str, Any]:
    document = yaml.safe_load(compose_file.read_text(encoding="utf-8")) or {}
    services = document.get("services")
    return services if isinstance(services, dict) else {}


def rename_version_dir(app: str, old_version: str) -> int:
    src = APPS_DIR / app / old_version
    if not src.is_dir():
        print(f"[skip] {src} 不存在（可能已经重命名过）")
        return 0

    if not RENAMABLE_DIR_RE.match(old_version):
        print(f"[skip] 目录名 {old_version!r} 属于人工命名（如 pg14/latest/stable-xxx），保持不动")
        return 0

    compose_file = src / COMPOSE_NAME
    if not compose_file.is_file():
        print(f"[warn] {compose_file} 不存在，跳过")
        return 0

    services = load_services(compose_file)
    if not services:
        print(f"[warn] {compose_file} 没有 services，跳过")
        return 0

    primary_name, primary_image = pick_primary_service(services, app)
    if not primary_name or not primary_image:
        print(f"[skip] {app}: 没有找到可用于版本判定的服务")
        return 0

    version = extract_version(primary_image, app)
    if not version:
        print(f"[skip] {app}: 主服务 {primary_name} 的镜像 {primary_image} 无法解析版本")
        return 0

    print(
        f"[info] {app}: 主服务 {primary_name} ({primary_image}) -> 版本 {version}，"
        f"当前目录 {old_version}"
    )

    if version == old_version:
        print(f"[info] {app}: 目录名已是最新，无需重命名")
        return 0

    target = APPS_DIR / app / version
    if target.exists():
        print(f"[err] 目标目录 {target} 已存在，拒绝覆盖，请人工处理", file=sys.stderr)
        return 1

    src.rename(target)
    print(f"[info] {app}: 已重命名 {src} -> {target}")
    return 0


def main(argv: list) -> int:
    if len(argv) != 3:
        print(f"用法: {argv[0]} <app_name> <old_version>", file=sys.stderr)
        return 1
    app_name, old_version = argv[1], argv[2]
    if not app_name or not old_version:
        print(f"用法: {argv[0]} <app_name> <old_version>", file=sys.stderr)
        return 1
    return rename_version_dir(app_name, old_version)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
