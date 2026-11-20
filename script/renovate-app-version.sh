#!/usr/bin/env bash
# 把 docker-compose.yml 里的镜像版本同步到应用目录名：apps/<app>/<version>。
#
# 由 .github/workflows/renovate-app-version.yml 调用，脚本是幂等的：
# 目录已经改好、或解析不出可用的版本号时直接正常退出。

set -uo pipefail

app_name="${1:-}"
old_version="${2:-}"

if [[ -z "$app_name" || -z "$old_version" ]]; then
    echo "用法: $0 <app_name> <old_version>" >&2
    exit 1
fi

if ! command -v yq >/dev/null 2>&1; then
    echo "[err] 需要 yq 来读取 docker-compose.yml" >&2
    exit 1
fi

src_dir="apps/${app_name}/${old_version}"
if [[ ! -d "$src_dir" ]]; then
    echo "目录 $src_dir 不存在（可能已经重命名过），跳过"
    exit 0
fi

# 应用名 -> 需要读取版本的服务名（这些应用不能只看第一个服务）
declare -A app_keys
app_keys=(
    ["immich"]="immich-server immich-machine-learning"
    ["immich-no-machine"]="immich-server"
    ["stream-rec"]="stream-rec-backend stream-rec-frontend"
    ["rsshub"]="rsshub"
    ["ztnet-pg"]="ztnet"
    ["nexus-media"]="backend"
    ["nexus-media-mysql"]="backend"
    ["nexus-media-postgresql"]="backend"
    ["keygate-all-in-one"]="keygate"
    ["medusa-all-in-one"]="medusa"
    ["glean"]="web backend admin"
)

# 这些应用需要保留 -alpine 后缀
app_alpine_names=("postgresql")

parsed_version=""
cleaned_version=""

extract_version() {
    # $1 = 镜像，$2 = 应用名；解析结果写入全局 parsed_version
    local image="$1"
    local app="$2"
    local tag trimmed found_alpine=false candidate pattern

    parsed_version=""

    if [[ "$image" != *:* ]]; then
        echo "[skip] 镜像没有 tag: $image"
        return
    fi

    tag="${image##*:}"
    trimmed="${tag#v}"
    echo "[info] 镜像 $image -> 版本 $trimmed"

    for item in "${app_alpine_names[@]}"; do
        if [[ "$item" == "$app" && "$trimmed" == *alpine* ]]; then
            found_alpine=true
        fi
    done

    # 优先识别日期版本（例如 minio 的 RELEASE.2026-11-20T...）
    for pattern in '[0-9]{4}-[0-9]{2}-[0-9]{2}' '[0-9]{2}-[0-9]{2}-[0-9]{2}'; do
        candidate="$(grep -oE "$pattern" <<< "$trimmed" | head -n1 || true)"
        if [[ -n "$candidate" ]]; then
            parsed_version="$candidate"
            [[ "$found_alpine" == true ]] && parsed_version+="-alpine"
            return
        fi
    done

    candidate="$(grep -oE '[0-9]+(\.[0-9]+){0,3}' <<< "$trimmed" | head -n1 || true)"
    if [[ -n "$candidate" ]]; then
        parsed_version="$candidate"
        [[ "$found_alpine" == true ]] && parsed_version+="-alpine"
        return
    fi

    echo "[skip] 无法从镜像 $image 解析出版本号"
}

docker_compose_files="$(find "$src_dir" -name docker-compose.yml)"
if [[ -z "$docker_compose_files" ]]; then
    echo "[warn] $src_dir 下没有 docker-compose.yml，跳过"
    exit 0
fi

while IFS= read -r compose_file; do
    [[ -z "$compose_file" ]] && continue
    echo "[info] 处理 $compose_file"

    if [[ -n "${app_keys[$app_name]:-}" ]]; then
        services="${app_keys[$app_name]}"
    else
        services="$(yq '.services | keys | .[0]' "$compose_file")"
    fi

    for service in $services; do
        if [[ "$(yq ".services | keys | .[] | select(. == \"$service\")" "$compose_file")" == "" ]]; then
            echo "[warn] 服务 $service 不存在于 $compose_file"
            continue
        fi

        image="$(yq ".services.\"$service\".image" "$compose_file")"
        echo "[info] 服务 $service 的镜像: $image"
        extract_version "$image" "$app_name"

        if [[ -n "$parsed_version" ]]; then
            if [[ -z "$cleaned_version" ]]; then
                cleaned_version="$parsed_version"
            elif [[ "$cleaned_version" != "$parsed_version" ]]; then
                echo "[warn] 服务 $service 的版本 $parsed_version 与 $cleaned_version 不一致，沿用 $cleaned_version"
            fi
        fi
    done
done <<< "$docker_compose_files"

if [[ -z "$cleaned_version" ]]; then
    echo "[skip] 未能确定版本，保留目录 $src_dir 不变"
    exit 0
fi

if [[ "$cleaned_version" == "$old_version" ]]; then
    echo "[info] 版本目录已是最新（$cleaned_version），无需重命名"
    exit 0
fi

target_dir="apps/${app_name}/${cleaned_version}"
if [[ -e "$target_dir" ]]; then
    echo "[err] 目标目录 $target_dir 已存在，拒绝覆盖，请人工处理" >&2
    exit 1
fi

if ! mv "$src_dir" "$target_dir"; then
    echo "[err] 重命名 $src_dir -> $target_dir 失败" >&2
    exit 1
fi

echo "[info] 已重命名 $src_dir -> $target_dir"
