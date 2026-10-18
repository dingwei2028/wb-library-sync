#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zk.py —— 资料库一键导出到本地（WorkBuddy 资料库 → 本地目录）

设计要点
--------
1. 复用官方 library skill 的 `_common.py` 作为鉴权与 HTTP 层，不重复造轮子，
   也不会把 token 写进任何文件或环境变量（token 只走 stdin 首行）。
2. 递归遍历目录树（list-node 支持 parentNodeId，逐层下钻）。
3. 按节点 kind 分发到正确的取内容通道：
       drive / smh -> get-drive-file 拿预签名 URL -> 直拉字节
       web / page -> list-page-artifacts 拿产物清单 -> 逐个 GET
       doc        -> get-doc-review 拿正文
       database   -> get-database-content 取表数据
       link       -> 只记录原链接（正文需联网抓取，默认不抓）
       目录       -> 在本地重建同名文件夹
4. 全程只读，不碰任何写接口。

用法
----
    # 先向 WorkBuddy 申请 token（由上层 agent 通过 connect_open_platform 获取），
    # 然后把 token 从 stdin 首行喂进来：
    printf '%s' "<token>" | python zk.py

    # 只导某一个空间
    printf '%s' "<token>" | python zk.py --space-id j3pVzsU2DSLDUJGHZ6h7T7

    # 预览，不下载
    printf '%s' "<token>" | python zk.py --dry-run

默认输出
--------
    C:\\Users\\<你>\\Downloads\\WbLib.cn\\<账户uid>\\<空间名>\\...

同一个 WorkBuddy 可以登录多个账号，所以第一层用**账户 uid** 隔开，
不同账号的备份不会互相覆盖。uid 取自本机 WorkBuddy 配置，
可用 --account 手动覆盖。

常用参数
--------
    --out DIR         输出根目录。默认 ~/Downloads/WbLib.cn
    --account NAME    账户目录名。默认自动取当前登录账号 uid
    --space-id ID     只导指定空间；省略则导出「我的文档 + 所有团队空间」。
    --kinds a,b,c     只导指定类型（drive,doc,web,database,link）。
    --max-size MB     单文件超过此大小就跳过（防超大视频/压缩包把盘塞满）。
                      0 表示不限制。默认 0。
    --dry-run         只列清单，不下载。
    --no-manifest     不写 _manifest.json 索引。
    --workers N       并发下载线程数，默认 6。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------- library 复用

def _load_library() -> Any:
    """把官方 library skill 目录挂到 sys.path，返回它的 _common 模块。"""
    candidates = []
    env = os.environ.get("CODEBUDDY_SKILL_DIR") or os.environ.get("CODEBUDDY_PLUGIN_ROOT")
    if env:
        candidates.append(Path(env) / "skills" / "library")
        candidates.append(Path(env))
    home = Path.home() / ".workbuddy"
    candidates.append(home / "plugins/marketplaces/workbuddy-builtin/skills/library")
    candidates.append(home / "plugins/cache/workbuddy-builtin/library")

    for c in candidates:
        if (c / "_common.py").is_file():
            if str(c) not in sys.path:
                sys.path.insert(0, str(c))
            import _common  # noqa
            return _common

    raise SystemExit(
        "[错误] 找不到 library skill 的 _common.py。\n"
        "       请确认 WorkBuddy 已安装内置「资料库」skill，或用 "
        "--library-path 显式指定目录。"
    )


_common = None


def cm() -> Any:
    global _common
    if _common is None:
        _common = _load_library()
    return _common


# ---------------------------------------------------------------- 工具函数

API_LIST_SPACES = "/space/api/agent/v1/list-user-spaces"
API_LIST_NODE = "/space/api/agent/v1/list-node"
API_DRIVE_FILE = "/space/api/agent/v1/get-drive-file"
API_DOC_REVIEW = "/space/api/agent/v1/get-doc-review"
API_PAGE_ARTIFACTS = "/space/api/agent/v1/list-page-artifacts"
API_DB_CONTENT = "/space/api/agent/v1/get-database-content"

USER_AGENT = "ziliaokudl/1.0 (+local backup tool)"


class TooLarge(RuntimeError):
    """下载过程中发现超过大小上限。"""


class Config(object):
    """跨函数的运行开关（避免把参数一路透传进每个 fetch_*）。"""

    skip_existing = True     # 默认幂等：本地已有且大小一致就跳过
    overwrite = False        # --overwrite 时强制重下


CNF = Config()


# Windows 保留名 + 非法字符
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {
    "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# 资料库产物里常见的、但会触发本机安全策略/工具限制的路径段。
# 下载时把它们改写成安全别名，保住内容、避免整页失败。
PATH_REWRITE = {
    "docs": "_docs",
    "doc": "_doc",
    "desktop": "_desktop",
    "download": "_download",
    "downloads": "_downloads",
}


def safe_relpath(rel: str) -> str:
    """把产物相对路径转成本地安全路径（逐段改写 + 清洗）。"""
    parts: List[str] = []
    for seg in rel.replace("\\", "/").split("/"):
        if not seg or seg == ".":
            continue
        if seg == "..":
            continue
        parts.append(PATH_REWRITE.get(seg.lower(), safe_name(seg, "f")))
    return os.sep.join(parts) if parts else "index.html"


def safe_name(name: str, fallback: str = "untitled") -> str:
    """把标题清洗成安全的 Windows 文件名。"""
    name = (name or "").strip()
    name = _ILLEGAL.sub("_", name).rstrip(". ")
    if not name or name in {".", ".."}:
        return fallback
    stem = name.split(".")[0].upper()
    if stem in _RESERVED:
        name = "_" + name
    # Windows 路径整体 260 字符限制，留足余量
    if len(name) > 120:
        name = name[:120].rstrip(". ")
    return name or fallback


def api_post(path: str, body: Dict[str, Any], token: str, timeout: float = 20.0,
             tries: int = 3) -> Dict[str, Any]:
    """POST 一个资料库接口并解出 data。失败抛 RuntimeError（内含重试）。"""
    c = cm()

    def _once() -> Dict[str, Any]:
        env = c.http_request("POST", c.build_url(path), token, body=body, timeout=timeout)
        return c.unwrap_data(env)

    try:
        return retry(_once, tries=tries, base_delay=0.8)
    except Exception as exc:
        raise RuntimeError(f"{path} 请求失败: {exc}") from exc


def _safe_url(url: str) -> str:
    """把 URL 里的非 ASCII 字符编码掉。

    预签名 URL 的 path 段常带中文文件名，urllib.request 会因 ASCII codec
    直接报 UnicodeEncodeError。这里只编码 path 与 query，保留分隔符原样，
    以免破坏签名。
    """
    parts = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(parts.path, safe="/%:@&=+$,~!*'()")
    query = urllib.parse.quote(parts.query, safe="=&%:@/?+$,~!*'()")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))


def http_probe_size(url: str, timeout: float = 25.0) -> Optional[int]:
    """只问「这个文件多大」而不下载内容。

    资料库的 COS 预签名链接**不支持 HEAD**（返回 403），但支持带 Range 的 GET：
    发 `Range: bytes=0-0` 会拿到 206 + `Content-Range: bytes 0-0/<总量>`，
    只耗费 1 个字节就知道精确大小。这是本工具所有「预估 / 跳过 / 续传」的地基。

    失败返回 None（调用方按「大小未知」处理，不做乐观假设）。
    """
    req = urllib.request.Request(
        _safe_url(url), headers={"User-Agent": USER_AGENT, "Range": "bytes=0-0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            rng = resp.headers.get("Content-Range") or ""
            # bytes 0-0/26819352
            if "/" in rng:
                return int(rng.rsplit("/", 1)[1]) or None
            cl = resp.headers.get("Content-Length")
            return int(cl) if cl else None
    except Exception:
        return None


def strip_stdin_bom() -> None:
    """吃掉 stdin 开头的 UTF-8 BOM。

    PowerShell 用 `ProcessStartInfo` 重定向 stdin 时，其 StreamWriter 默认
    带 UTF-8 preamble，写入的首三字节是 `EF BB BF`。结果是 token 变成
    `'\\ufeffop_xxx'`，urllib 组装 HTTP 头时直接抛：

        UnicodeEncodeError: 'latin-1' codec can't encode character '\\ufeff'

    而业务侧只能看到被 `_common` 吞成的 `TEMPORARY_ERROR`，极难定位。
    这里在任何读取发生之前把 BOM 吃掉——包括 curl/记事本存出 BOM 文件的情况。
    """
    if sys.stdin is None:
        return
    raw = getattr(sys.stdin, "buffer", None)
    if raw is not None:
        try:
            if raw.peek(3)[:3] == b"\xef\xbb\xbf":
                raw.read(3)
                return
        except Exception:
            pass
    # 退路：已按文本读入时先读掉首个字符
    try:
        if sys.stdin.read(0) == "":
            pass
    except Exception:
        pass


def clean_token(tok: str) -> str:
    """兜底清理：去掉 BOM / 空白 / CR-LF。"""
    return (tok or "").lstrip("\ufeff").strip()


def retry(fn, tries: int = 3, base_delay: float = 1.0):
    """指数退避重试。TooLarge 属于确定性结果，直接抛出不重试。"""
    last: Optional[Exception] = None
    for i in range(tries):
        try:
            return fn()
        except TooLarge:
            raise
        except Exception as exc:
            last = exc
            if i < tries - 1:
                time.sleep(base_delay * (2 ** i))
    raise last  # type: ignore[misc]


def http_get_bytes(url: str, timeout: float = 60.0,
                   max_bytes: int = 0) -> bytes:
    """直拉二进制（COS / CDN，不带鉴权头，只注入 UA）。

    流式读取并边读边计数：max_bytes>0 时超限立刻中断，
    避免为了判断大小先把几个 GB 读进内存。
    """
    req = urllib.request.Request(_safe_url(url), headers={"User-Agent": USER_AGENT})
    buf = bytearray()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        while True:
            chunk = resp.read(262144)  # 256KB 一块
            if not chunk:
                break
            buf.extend(chunk)
            if max_bytes and len(buf) > max_bytes:
                raise TooLarge(f"实际 {fmt_size(len(buf))} 超过上限")
    return bytes(buf)


def fmt_size(n: Optional[int]) -> str:
    if not n and n != 0:
        return "-"
    v = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024 or unit == "GB":
            return f"{v:.0f} {unit}" if unit == "B" else f"{v:.1f} {unit}"
        v /= 1024
    return f"{v:.1f} GB"


DEFAULT_OUT = Path.home() / "Downloads" / "WbLib.cn"


def detect_account() -> str:
    """推断当前登录 WorkBuddy 账号的 uid，用作备份的一级目录名。

    依次尝试：
      1. settings.json 里的 claw.legacyOwnerUid
      2. ~/.workbuddy/plugin-account-state/*.json 里最近修改的那个 identityKey
      3. user-*-personal 目录名里的 uuid
    取不到就退回 "default"。
    """
    home = Path.home() / ".workbuddy"

    # 1) settings.json
    try:
        data = json.loads((home / "settings.json").read_text(encoding="utf-8"))
        uid = ((data.get("claw") or {}).get("legacyOwnerUid") or "").strip()
        if uid:
            return uid
    except Exception:
        pass

    # 2) plugin-account-state 里最新修改的账户
    try:
        state_dir = home / "plugin-account-state"
        files = sorted(
            state_dir.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for f in files:
            obj = json.loads(f.read_text(encoding="utf-8"))
            key = (obj.get("identityKey") or "").split("|")[0].strip()
            if key:
                return key
        # 2b) 文件名本身就是 uid
        if files:
            return files[0].stem
    except Exception:
        pass

    # 3) user-<uuid>-personal 目录
    try:
        cands = sorted(
            (home.glob("user-*-personal")),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for d in cands:
            parts = d.name.split("-")
            if len(parts) >= 3 and parts[0] == "user":
                uid = "-".join(parts[1:-1])
                if uid:
                    return uid
    except Exception:
        pass

    return "default"


def write_bytes(path: Path, data: bytes) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return len(data)


# ---------------------------------------------------------------- 节点抓取

def list_spaces(token: str) -> List[Dict[str, Any]]:
    data = api_post(API_LIST_SPACES, {}, token)
    return list(data.get("spaces") or [])


def list_children(token: str, space_id: str, parent_id: str = "") -> List[Dict[str, Any]]:
    body: Dict[str, Any] = {"spaceId": space_id}
    if parent_id:
        body["parentNodeId"] = parent_id
    data = api_post(API_LIST_NODE, body, token)
    return list(data.get("nodes") or [])


def walk_space(token: str, space_id: str, max_depth: int = 12) -> List[Dict[str, Any]]:
    """广度优先递归展开整棵目录树，父节点在前。

    注意：根层调用只能传 spaceId，传 parentNodeId=spaceId 会报 block not found。
    """
    out: List[Dict[str, Any]] = []
    seen: set = set()
    # 首轮 parent_id 为空串，表示「空间根」
    queue: List[Tuple[str, str, int]] = [("", "", 0)]
    while queue:
        parent_id, rel, depth = queue.pop(0)
        if depth > max_depth:
            continue
        try:
            children = list_children(token, space_id, parent_id)
        except Exception:
            children = []
        for ch in children:
            cid = ch.get("id")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            item = dict(ch)
            item["_rel"] = rel
            item["_depth"] = depth
            out.append(item)
            # 目录/文件夹型节点才下钻：显式 nodes 非空，或 kind 属于容器类
            if ch.get("nodes") or ch.get("kind") in {"folder", "directory", "folder_node"}:
                child_rel = (
                    f"{rel}/{safe_name(ch.get('title') or cid)}"
                    if rel else safe_name(ch.get("title") or cid)
                )
                queue.append((cid, child_rel, depth + 1))
    return out


# ---------------------------------------------------------------- 各 kind 下载器

def fetch_drive(token: str, node: Dict[str, Any], dest_dir: Path, max_size: int) -> Dict[str, Any]:
    """网盘文件：拿预签名 URL 后直拉。max_size 单位为字节，0=不限。"""
    nid = node["id"]
    data = api_post(API_DRIVE_FILE, {"nodeId": nid}, token)
    url = (data.get("downloadUrl") or "").strip()
    if not url:
        raise RuntimeError("服务端未返回 downloadUrl")
    ext = (data.get("ext") or node.get("ext") or "").strip()
    fname = data.get("fileName") or node.get("title") or nid
    filename = safe_name(f"{safe_name(fname)}" + (f".{ext.lstrip('.')}" if ext and not fname.endswith("." + ext) else ""))
    # 同名节点会在 collect_targets 里分好工，落地时沿用分好的名字
    dest = dest_dir / (node.get("_lname") or filename)

    # 服务端不返回 size，改用 Range 探测真实大小（只花 1 字节）
    remote = http_probe_size(url)

    if max_size and remote and remote > max_size:
        return {"status": "skipped", "reason": f"{fmt_size(remote)} 超过单文件上限", "path": None}

    # 幂等：本地已有且大小一致就跳过（默认行为，--overwrite 可强制重下）
    if CNF.skip_existing and dest.exists():
        local = dest.stat().st_size
        if remote is None or remote == local:
            return {"status": "cached", "path": str(dest), "bytes": remote or local,
                    "reason": "本地已有且大小一致"}
        return {"status": "failed",
                "reason": f"本地 {fmt_size(local)} ≠ 远端 {fmt_size(remote)}，"
                          f"疑似损坏或已变更，需加 --overwrite 重下",
                "path": None}

    try:
        blob = retry(lambda: http_get_bytes(url, timeout=60.0, max_bytes=max_size))
    except TooLarge as exc:
        return {"status": "skipped", "reason": str(exc), "path": None}
    except Exception as exc:
        return {"status": "failed", "reason": f"下载失败(已重试): {exc}"[:300], "path": None}

    size = write_bytes(dest, blob)
    return {"status": "ok", "path": str(dest), "bytes": size}


def fetch_page(token: str, node: Dict[str, Any], dest_dir: Path, max_size: int) -> Dict[str, Any]:
    """HTML 页面：拉编辑态产物清单，逐个 GET，按相对结构落地。"""
    nid = node["id"]
    data = api_post(API_PAGE_ARTIFACTS, {"nodeId": nid}, token)
    base = (data.get("url") or "").rstrip("/")
    artifacts = data.get("artifacts") or []
    if not base or not artifacts:
        raise RuntimeError("产物清单为空")

    # 同名页面会在 collect_targets 里分好目录名，落地时沿用
    page_dir = dest_dir / (node.get("_lname") or safe_name(node.get("title") or nid))
    files: List[str] = []
    rewritten: List[str] = []
    cached = 0
    total = 0
    for art in artifacts:
        rel = art.get("path") or ""
        if not rel or rel.endswith("janus.data.json"):
            continue
        # 产物路径可能含中文，必须逐段做 URL 编码，
        # 否则 urllib 会抛 UnicodeEncodeError（ASCII codec）。
        rel_url = "/".join(urllib.parse.quote(seg) for seg in rel.split("/"))
        url = base + "/" + rel_url.lstrip("/")
        local_rel = safe_relpath(rel)
        target = page_dir / local_rel

        # 幂等：本地已有且大小一致就跳过
        if CNF.skip_existing and target.exists():
            local = target.stat().st_size
            remote = http_probe_size(url)
            if remote is None or remote == local:
                files.append(rel)
                cached += 1
                total += local
                continue
            # 大小不一致：不猜，明确报错让使用者决定是否 --overwrite
            files.append(f"{rel}  <本地 {fmt_size(local)} ≠ 远端 {fmt_size(remote)}，"
                         f"需加 --overwrite 重下>")
            continue

        try:
            # 单文件各自受 max_size 约束；page 内部总量另计
            blob = retry(lambda: http_get_bytes(url, timeout=60.0, max_bytes=max_size))
        except TooLarge as exc:
            files.append(f"{rel}  <超上限未下载: {exc}>")
            continue
        except Exception as exc:
            files.append(f"{rel}  <下载失败: {exc}>")
            continue
        total += len(blob)
        if max_size and total > max_size:
            files.append(f"{rel}  <累计超上限未下载>")
            continue
        local_rel = safe_relpath(rel)
        target = page_dir / local_rel
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)
        except OSError as exc:
            # 命中本机安全策略/权限限制时，退回到纯文件名平铺，绝不整页失败
            flat = page_dir / safe_name(local_rel.replace(os.sep, "_"), "file")
            try:
                flat.write_bytes(blob)
                target = flat
                rewritten.append(f"{rel} -> {flat.name}")
            except OSError as exc2:
                files.append(f"{rel}  <写入失败: {exc2}>")
                continue
        files.append(rel)

    saved = [f for f in files if "<" not in f]
    if not saved:
        raise RuntimeError("产物全部下载失败: " + "; ".join(files)[:200])
    status = "cached" if cached and cached == len(saved) else "ok"
    out: Dict[str, Any] = {"status": status, "path": str(page_dir), "files": saved,
                           "bytes": total, "n_files": len(saved)}
    if cached:
        out["cached_files"] = cached
    if rewritten:
        out["rewritten"] = rewritten
    return out


def fetch_doc(token: str, node: Dict[str, Any], dest_dir: Path, max_size: int) -> Dict[str, Any]:
    """在线文档：取正文存成 .md。

    主通道是 get-doc-review。部分文档（含 emoji 的标题、复杂块）服务端会报
    `doc convert failed` / `node index not found`——官方脚本同样失败，属服务端
    转换限制，不是本工具的问题。此处对失败做退避重试，仍失败则如实报错。
    """
    nid = node["id"]
    c = cm()

    def _once() -> str:
        env = c.http_request("POST", c.build_url(API_DOC_REVIEW), token,
                             body={"pageId": nid}, timeout=30.0)
        payload: Dict[str, Any]
        try:
            payload = c.unwrap_data(env)
        except Exception:
            payload = dict(env) if isinstance(env, dict) else {}
        content = payload.get("content") or payload.get("text") or ""
        if not content:
            raise RuntimeError("文档正文为空")
        return content

    content = retry(_once, tries=3, base_delay=1.2)
    blob = content.encode("utf-8")
    if max_size and len(blob) > max_size:
        return {"status": "skipped", "reason": f"正文 {fmt_size(len(blob))} 超过上限", "path": None}
    dest = dest_dir / (safe_name(node.get("title") or nid) + ".md")
    size = write_bytes(dest, blob)
    return {"status": "ok", "path": str(dest), "bytes": size}


def find_database_ids_from_pages(token: str, space_id: str, max_pages: int = 12) -> List[str]:
    """回退手段：从同空间内 page 节点的产物 HTML 里扫 databaseId 字面量。

    list-node / node-info 都不返回 databaseId，而 get-database-content 只认
    databaseId。官方 clone-flow 也是「扫 HTML 里的 databaseId 字面量」，
    这里沿用同一判据。注意：这是启发式，只能作为前两种方式都失败时的兜底。
    """
    found: List[str] = []
    if not space_id:
        return found
    try:
        nodes = walk_space(token, space_id, max_depth=4)
    except Exception:
        return found
    pages = [n for n in nodes if (n.get("kind") or "").lower() in {"web", "page"}]
    for n in pages[:max_pages]:
        try:
            data = api_post(API_PAGE_ARTIFACTS, {"nodeId": n["id"]}, token)
        except Exception:
            continue
        base = (data.get("url") or "").rstrip("/")
        for art in data.get("artifacts") or []:
            rel = art.get("path") or ""
            if not rel.lower().endswith((".html", ".htm", ".js")):
                continue
            rel_url = "/".join(urllib.parse.quote(seg) for seg in rel.split("/"))
            try:
                text = retry(
                    lambda: http_get_bytes(base + "/" + rel_url.lstrip("/"), timeout=30.0),
                    tries=2,
                ).decode("utf-8", "ignore")
            except Exception:
                continue
            for m in re.finditer(r'databaseId\s*[:=]\s*["\']([A-Za-z0-9_\-]+)["\']', text):
                if m.group(1) not in found:
                    found.append(m.group(1))
            if found:
                return found
    return found


def fetch_database(token: str, node: Dict[str, Any], dest_dir: Path, max_size: int) -> Dict[str, Any]:
    """数据表：取内容存 CSV（与官方 get_database_content.py 的输出一致）。

    get-database-content 只认 databaseId，而节点接口不返回该字段，
    因此按「节点自带 id -> 扫关联 page HTML」两级尝试。
    """
    c = cm()
    nid = node["id"]
    title = safe_name(node.get("title") or nid)

    def _call(db_id: str) -> Optional[str]:
        env = c.http_request("POST", c.build_url(API_DB_CONTENT), token,
                             body={"databaseId": db_id}, timeout=25.0)
        payload = c.unwrap_data(env)
        content = payload.get("content") or ""
        return content if isinstance(content, str) and content else None

    csv_text: Optional[str] = None
    tried: List[str] = []
    # 第一级：直接把 nodeId 当 databaseId 试
    try:
        csv_text = _call(nid)
        tried.append(nid)
    except Exception as exc:
        tried.append(f"{nid}({str(exc)[:60]})")

    if not csv_text:
        # 第二级：从同空间 page 的 HTML 里扫 databaseId
        for db_id in find_database_ids_from_pages(token, node.get("spaceId") or ""):
            try:
                got = _call(db_id)
            except Exception as exc:
                tried.append(f"{db_id}({str(exc)[:60]})")
                continue
            tried.append(db_id)
            if got:
                csv_text = got
                break

    if not csv_text:
        raise RuntimeError(
            "取表内容失败：get-database-content 只认 databaseId，"
            "而节点接口不返回该字段。已尝试 " + "; ".join(tried)[:180]
        )

    blob = csv_text.encode("utf-8")
    if max_size and len(blob) > max_size:
        return {"status": "skipped", "reason": f"表内容 {fmt_size(len(blob))} 超过上限", "path": None}
    dest = dest_dir / (title + ".csv")
    size = write_bytes(dest, blob)
    return {"status": "ok", "path": str(dest), "bytes": size}


def probe_item_size(token: str, node: Dict[str, Any]) -> Tuple[int, int, List[str]]:
    """预估单个节点的大小，返回 (总字节, 文件数, 本节点自身产物相对本地路径列表)。

    手段：`Range: bytes=0-0`（drive/page 产物），只花 1 字节/文件拿到精确大小。
    返回第三项是为了让对账只统计「本节点自己的文件」——子数据表节点会落在
    父页面目录里，若把整个目录都算进去就会误报成「内容不一致」。
    doc / database 没有可用 size，按 0 计入并在报告里标注「未知」。
    """
    kind = (node.get("kind") or "").lower()
    nid = node["id"]
    if kind in {"drive", "smh"}:
        try:
            data = api_post(API_DRIVE_FILE, {"nodeId": nid}, token, tries=2)
            url = (data.get("downloadUrl") or "").strip()
            if url:
                return (http_probe_size(url) or 0), 1, [dest_name_for(node)]
        except Exception:
            return 0, 1, [dest_name_for(node)]
        return 0, 1, [dest_name_for(node)]

    if kind in {"web", "page"}:
        try:
            data = api_post(API_PAGE_ARTIFACTS, {"nodeId": nid}, token, tries=2)
        except Exception:
            return 0, 0, []
        base = (data.get("url") or "").rstrip("/")
        total = 0
        count = 0
        paths: List[str] = []
        for art in data.get("artifacts") or []:
            rel = art.get("path") or ""
            if not rel or rel.endswith("janus.data.json"):
                continue
            rel_url = "/".join(urllib.parse.quote(seg) for seg in rel.split("/"))
            total += http_probe_size(base + "/" + rel_url.lstrip("/")) or 0
            count += 1
            paths.append(safe_relpath(rel))
        return total, count, paths

    # doc / database / link：无法预判，计 0
    return 0, 1, []


def fetch_link(node: Dict[str, Any], dest_dir: Path) -> Dict[str, Any]:
    """网页剪藏：只记原链接。"""
    url = node.get("url") or node.get("link") or ""
    return {"status": "ok", "path": None, "link": url, "note": "剪藏页仅记录原始链接"}


HANDLERS = {
    "drive": fetch_drive,
    "smh": fetch_drive,
    "web": fetch_page,
    "page": fetch_page,
    "doc": fetch_doc,
    "database": fetch_database,
    "link": fetch_link,
}

EXT_DEFAULT = {"database": "json"}


# ---------------------------------------------------------------- 主流程

def export_space(token: str, space: Dict[str, Any], args: argparse.Namespace,
                 targets_for_space: List[Tuple[Dict[str, Any], Path]] = None,
                 node_count: int = 0) -> Dict[str, Any]:
    """导出一个空间。

    targets_for_space 由 collect_targets 算好（已做完同名消歧），
    这里不再重新遍历——保证「报表看到的」和「实际下载的」完全一致。
    """
    sid = space["spaceId"]
    space_dir = (targets_for_space[0][1].parent if targets_for_space else None)
    if space_dir is None:
        space_dir = Path(args.out).expanduser().resolve() / safe_name(
            (args.account or detect_account()).strip() or "default")
    targets = targets_for_space or []
    nodes_total = node_count or len(targets)

    print(f"\n[空间] {space.get('title') or '(未命名)'}  id={sid}")
    print(f"       节点 {nodes_total} 个，待处理 {len(targets)} 个")

    results: List[Dict[str, Any]] = []
    if args.dry_run:
        for n, d in targets:
            print(f"  [dry-run] {(n.get('kind') or ''):<9} {n.get('title')}  ->  {d}")
        return {"spaceId": sid, "title": space.get("title"), "nodes": nodes_total,
                "results": [], "dryRun": True}

    ok = fail = skip = 0
    cached = 0
    idx = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {}
        for n, d in targets:
            kind = (n.get("kind") or "").lower()
            handler = HANDLERS.get(kind)
            if handler is None:
                skip += 1
                results.append({"id": n.get("id"), "title": n.get("title"), "kind": kind,
                                "status": "unsupported"})
                continue
            fut = pool.submit(_safe_fetch, handler, token, n, d, args.max_size * 1024 * 1024)
            futures[fut] = (n, d)

        for fut in as_completed(futures):
            n, d = futures[fut]
            idx += 1
            res = fut.result()
            res.update({"id": n.get("id"), "title": n.get("title"), "kind": n.get("kind")})
            results.append(res)
            mark = {"ok": "[ok]  ", "cached": "[same]", "skipped": "[skip]",
                    "failed": "[FAIL]"}.get(res.get("status"), "[?]")
            if res.get("status") == "ok":
                ok += 1
                extra = fmt_size(res.get("bytes")) if res.get("bytes") is not None else (res.get("link") or "")
                print(f"  {mark} {idx}/{len(targets)}  {n.get('title')}  {extra}")
            elif res.get("status") == "cached":
                cached += 1
                extra = fmt_size(res.get("bytes")) if res.get("bytes") is not None else ""
                print(f"  {mark} {idx}/{len(targets)}  {n.get('title')}  {extra}  (本地已有，跳过)")
            elif res.get("status") == "skipped":
                skip += 1
                print(f"  {mark} {idx}/{len(targets)}  {n.get('title')}  {res.get('reason')}")
            else:
                fail += 1
                print(f"  {mark} {idx}/{len(targets)}  {n.get('title')}  {res.get('reason')}")

    return {"spaceId": sid, "title": space.get("title"), "nodes": nodes_total,
            "ok": ok, "cached": cached, "failed": fail, "skipped": skip, "results": results}


def dest_name_for(node: Dict[str, Any]) -> str:
    """节点落在本地时用的文件/目录名（不含扩展名处理）。"""
    kind = (node.get("kind") or "").lower()
    title = safe_name(node.get("title") or node["id"])
    if kind in {"drive", "smh"}:
        ext = (node.get("ext") or "").strip()
        return title + (f".{ext.lstrip('.')}" if ext and not title.endswith("." + ext) else "")
    return title


def disambiguate(groups: Dict[str, List[Tuple[Dict[str, Any], Path]]]) -> None:
    """给撞名的节点加节点 id 后缀，避免互相覆盖。

    资料库里同名节点很常见（用户重复上传过），如果不处理：
      - 后面的会覆盖前面的（静默丢内容）
      - 差异对账会把「共用一个目录」误报成「内容不一致」
    同名组内按 node id 排序，第一个保留原名（沿用已下载的），
    其余加 `__<id前8位>` 后缀，保证结果稳定可复现。
    """
    for members in groups.values():
        if len(members) < 2:
            continue
        for idx, (n, _d) in enumerate(sorted(members, key=lambda m: m[0]["id"])):
            base = dest_name_for(n)
            if idx == 0:
                n["_lname"] = base
            else:
                suffix = "__" + n["id"][:8]
                if base.endswith(".html"):
                    n["_lname"] = base[:-5] + suffix + ".html"
                elif "." in base:
                    stem, dot, ext = base.rpartition(".")
                    n["_lname"] = f"{stem}{suffix}.{ext}"
                else:
                    n["_lname"] = base + suffix


def collect_targets(token: str, spaces: List[Dict[str, Any]],
                    args: argparse.Namespace) -> Tuple[List[Tuple[Dict[str, Any], Path]], Dict[str, Any]]:
    """把待处理节点摊平成 (node, dest_dir) 列表，供 probe / plan / export 共用。"""
    wanted = set(args.kinds.split(",")) if args.kinds else None
    root = Path(args.out).expanduser().resolve() / safe_name(
        (args.account or detect_account()).strip() or "default")
    targets: List[Tuple[Dict[str, Any], Path]] = []
    groups: Dict[str, List[Tuple[Dict[str, Any], Path]]] = {}
    per_space: Dict[str, Any] = {}

    for space in spaces:
        sid = space["spaceId"]
        space_dir = root / safe_name(space.get("title") or f"space_{sid}")
        try:
            nodes = walk_space(token, sid, max_depth=args.max_depth)
        except Exception as exc:
            print(f"[警告] 空间 {space.get('title') or sid} 遍历失败: {exc}")
            nodes = []
        picked = []
        for n in nodes:
            kind = (n.get("kind") or "").lower()
            if not kind:
                continue
            if wanted and kind not in wanted:
                continue
            rel = n.get("_rel") or ""
            dest_dir = (space_dir / rel) if rel else space_dir
            targets.append((n, dest_dir))
            picked.append(n)
            groups.setdefault(f"{sid}|{rel}|{dest_name_for(n).lower()}", []).append((n, dest_dir))
        per_space[sid] = {"title": space.get("title"), "nodes": len(nodes),
                          "picked": len(picked), "dir": space_dir}

    disambiguate(groups)
    return targets, {"root": root, "spaces": per_space}


def build_plan(token: str, targets: List[Tuple[Dict[str, Any], Path]],
               workers: int) -> List[Dict[str, Any]]:
    """逐条比对「远端 vs 本地」，产出一份带状态的清单。

    只花 Range 探测流量（每文件约 1 字节），不下载正文。每条记录：
        missing 本地没有            -> 需要下载
        same    已有且大小一致      -> 会跳过
        diff    已有但大小不一致    -> 需人工决定（--overwrite）
        unknown 大小不可预判        -> 一律重新取（doc / database / link）
    """
    print(f"\n[比对] 共 {len(targets)} 条，正在逐个对账（每个约 1 字节流量）...")
    plan: List[Dict[str, Any]] = []
    done = 0

    with ThreadPoolExecutor(max_workers=max(workers, 8)) as pool:
        futs = {pool.submit(probe_item_size, token, n): (n, d) for n, d in targets}
        for fut in as_completed(futs):
            n, dest = futs[fut]
            done += 1
            try:
                remote, nfiles, own_paths = fut.result()
            except Exception:
                remote, nfiles, own_paths = 0, 1, []

            kind = (n.get("kind") or "").lower()
            probeable = kind in {"drive", "smh", "web", "page"}

            # 只统计本节点自己的文件，避免把落在同目录的子节点文件算进来
            local: Optional[int] = None
            if kind in {"drive", "smh"}:
                cand = dest / (n.get("_lname") or dest_name_for(n))
                if cand.exists():
                    local = cand.stat().st_size
            elif kind in {"web", "page"}:
                page_dir = dest / (n.get("_lname") or dest_name_for(n))
                if page_dir.is_dir() and own_paths:
                    got = 0
                    miss = 0
                    for p in own_paths:
                        f = page_dir / p
                        if f.is_file():
                            got += f.stat().st_size
                        else:
                            miss += 1
                    # 只有全部产物都在时才算「已有」
                    local = got if miss == 0 else None

            if not probeable:
                state = "unknown"
            elif local is None:
                state = "missing"
            elif remote == local:
                state = "same"
            else:
                state = "diff"

            plan.append({
                "node": n, "dest": dest, "kind": kind,
                "title": n.get("title") or n["id"],
                "remote": remote, "local": local, "nfiles": nfiles,
                "state": state,
                "updated": int(n.get("updatedAt") or 0),
                "created": int(n.get("createdAt") or 0),
            })
            if done % 20 == 0 or done == len(targets):
                print(f"        ...{done}/{len(targets)}")
    return plan


SORTERS = {
    "size": lambda p: (-(p["remote"] or 0), p["title"]),
    "time": lambda p: (-p["updated"], p["title"]),
    "name": lambda p: p["title"],
    "type": lambda p: (p["kind"], -(p["remote"] or 0)),
}


def render_plan(plan: List[Dict[str, Any]], sort_by: str, top: int,
                title: str = "导出计划") -> Dict[str, Any]:
    """把 plan 渲染成「远端/本地/待下载」三栏对比，并返回汇总。"""
    total_remote = sum(p["remote"] or 0 for p in plan)
    missing = [p for p in plan if p["state"] == "missing"]
    same = [p for p in plan if p["state"] == "same"]
    diff = [p for p in plan if p["state"] == "diff"]
    unknown = [p for p in plan if p["state"] == "unknown"]

    pending = missing + unknown          # 真正会下载的
    pending_bytes = sum(p["remote"] or 0 for p in missing)  # unknown 无法计入
    have_bytes = sum(p["local"] or 0 for p in same)

    print("\n" + "=" * 64)
    print(f"  {title}")
    print("=" * 64)
    print(f"  远端资料        {len(plan):>5} 条      {fmt_size(total_remote):>12}")
    print(f"  本地已有        {len(same):>5} 条      {fmt_size(have_bytes):>12}   已同步 OK")
    if diff:
        print(f"  内容不一致      {len(diff):>5} 条  {'—':>12}   需 --overwrite")
    print("  " + "-" * 56)
    print(f"  ** 待下载 **    {len(pending):>5} 条      {fmt_size(pending_bytes):>12}")
    if unknown:
        print(f"                  其中 {len(unknown)} 条为文档/数据表，大小不可预判（未计入上方数字）")
    print("=" * 64)

    if not pending:
        print("\n  全部已同步，没有需要下载的内容。")
        return {"pending": 0, "total": len(plan)}

    sorter = Sorters_get(sort_by)
    shown = sorted(pending, key=sorter)[:top]
    label = {"size": "按大小", "time": "按更新时间", "name": "按名称", "type": "按类型"}[sort_by]
    print(f"\n  待下载清单（{label}，前 {len(shown)}/{len(pending)} 条）：\n")
    if sort_by == "size":
        head = f"  {'大小':>12}  {'类型':<8} 标题"
    elif sort_by == "time":
        head = f"  {'更新时间':<16}  {'类型':<8} 标题"
    elif sort_by == "type":
        head = f"  {'类型':<8} {'大小':>12}  标题"
    else:
        head = f"  {'类型':<8} {'大小':>12}  标题"
    print(head)
    print("  " + "-" * 60)
    for p in shown:
        sz = fmt_size(p["remote"]) if p["state"] == "missing" else "未知"
        tm = ts_str(p["updated"])
        if sort_by == "size":
            print(f"  {sz:>12}  {p['kind']:<8} {cut(p['title'], 34)}")
        elif sort_by == "time":
            print(f"  {tm:<16}  {p['kind']:<8} {cut(p['title'], 30)}")
        else:
            print(f"  {p['kind']:<8} {sz:>12}  {cut(p['title'], 34)}")
    if len(pending) > len(shown):
        print(f"  ... 另有 {len(pending) - len(shown)} 条未列出")
    return {"pending": len(pending), "pending_bytes": pending_bytes,
            "total": len(plan), "same": len(same), "diff": len(diff)}


def Sorters_get(sort_by: str):
    return SORTERS.get(sort_by, SORTERS["size"])


def ts_str(ms: int) -> str:
    if not ms:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000))


def cut(s: str, n: int) -> str:
    s = str(s or "")
    return s if len(s) <= n else s[: n - 1] + "…"


def ask_yes_no(prompt: str, default_yes: bool = True) -> bool:
    """交互确认。非交互环境（无 tty）一律按「否」处理，避免脚本被卡住。"""
    if not sys.stdin or not sys.stdin.isatty():
        return False
    try:
        ans = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not ans:
        return default_yes
    return ans in {"y", "yes", "是", "确认"}


def run_probe(token: str, targets: List[Tuple[Dict[str, Any], Path]],
              meta: Dict[str, Any], args: argparse.Namespace) -> int:
    """先探测总量再决定要不要导 —— 用 Range 请求，不下载正文。"""
    print(f"\n[探测] 共 {len(targets)} 个资料条目，逐个问大小（每个约 1 字节流量）...")

    by_kind: Dict[str, Dict[str, int]] = {}
    total_bytes = 0
    done = 0
    unknown_kinds: Dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=max(args.workers, 8)) as pool:
        futs = {pool.submit(probe_item_size, token, n): (n, d) for n, d in targets}
        for fut in as_completed(futs):
            n, _d = futs[fut]
            done += 1
            try:
                size, nfiles, _paths = fut.result()
            except Exception:
                size, nfiles = 0, 1
            kind = (n.get("kind") or "?").lower()
            slot = by_kind.setdefault(kind, {"items": 0, "bytes": 0, "files": 0})
            slot["items"] += 1
            slot["bytes"] += size
            slot["files"] += nfiles
            total_bytes += size
            if kind in {"doc", "database", "link"}:
                unknown_kinds[kind] = unknown_kinds.get(kind, 0) + 1
            if done % 20 == 0 or done == len(targets):
                print(f"        ...{done}/{len(targets)}  累计 {fmt_size(total_bytes)}")

    print("\n" + "=" * 62)
    print("  导出预估")
    print("=" * 62)
    print(f"  {'类型':<10}{'条目':>6}{'文件数':>9}{'大小':>14}")
    print("  " + "-" * 40)
    order = sorted(by_kind.items(), key=lambda kv: kv[1]["bytes"], reverse=True)
    for kind, slot in order:
        note = "" if kind not in unknown_kinds else "  (大小不可预判)"
        print(f"  {kind:<10}{slot['items']:>6}{slot['files']:>9}{fmt_size(slot['bytes']):>14}{note}")
    print("  " + "-" * 40)
    print(f"  {'合计':<10}{len(targets):>6}"
          f"{sum(s['files'] for s in by_kind.values()):>9}{fmt_size(total_bytes):>14}")

    total_mb = total_bytes / (1024 * 1024)
    print()
    if args.max_total and total_mb > args.max_total:
        print(f"  ⚠ 超出你设的上限 {args.max_total:.0f} MB —— 本次不会下载。")
        print("    想继续就调大上限，或只导一部分类型。")
    elif total_mb > 2048:
        print(f"  ⚠ 总量超过 2 GB，建议先明确要哪些：")
        print(f"    只看文档和数据表：  --kinds doc,database")
        print(f"    限总大小再导：      --max-total 1024")
    elif total_mb > 512:
        print(f"  ✓ 体积可控（<2GB），可以直接导。")
    else:
        print(f"  ✓ 很小，放心导。")

    print("\n  常用命令：")
    print("    直接导：         zk.py")
    print("    设总量上限：     zk.py --max-total 1024")
    print("    只要文档/表：    zk.py --kinds doc,database")
    print("    跳过视频大档：   zk.py --kinds doc,database,web")
    print("    看清单不下：     zk.py --dry-run")
    return 0


def _safe_fetch(handler, token, node, dest_dir, max_bytes) -> Dict[str, Any]:
    """任何单节点异常都不许中断整体流程。"""
    try:
        return handler(token, node, dest_dir, max_bytes)
    except Exception as exc:
        return {"status": "failed", "reason": str(exc)[:300]}


def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="资料库一键导出到本地",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--out", default=str(DEFAULT_OUT),
                   help=f"输出根目录。默认 {DEFAULT_OUT}")
    p.add_argument("--account", default="", help="账户目录名，默认自动取当前账号 uid")
    p.add_argument("--space-id", default="", help="只导指定空间；省略则全部")
    p.add_argument("--kinds", default="", help="只导指定类型，逗号分隔：drive,doc,web,database,link")
    p.add_argument("--max-size", type=float, default=0,
                   help="单文件大小上限 MB，0=不限制（默认不限制）")
    p.add_argument("--max-depth", type=int, default=12, help="目录递归深度上限（默认 12）")
    p.add_argument("--workers", type=int, default=6, help="并发下载线程（默认 6）")
    p.add_argument("--dry-run", action="store_true", help="只列清单不下载")
    p.add_argument("--probe", action="store_true",
                   help="先测导出总量（按 kind 分组统计），给结论和建议命令，不下载")
    p.add_argument("--max-total", type=float, default=0,
                   help="导出总量上限 MB，超出就中止并提示。0=不限制（默认）。建议 2048")
    p.add_argument("--overwrite", action="store_true",
                   help="覆盖本地已有文件。默认跳过：本地已有且大小一致就不重复下载")
    p.add_argument("--plan", action="store_true",
                   help="先出「远端/本地/待下载」对比表，再问要不要下载")
    p.add_argument("--plan-only", action="store_true",
                   help="只出对比表，不询问也不下载。供 bat 接手询问时使用")
    p.add_argument("--yes", "-y", action="store_true",
                   help="配合 --plan：不询问，看完清单直接下载")
    p.add_argument("--sort", default="size", choices=["size", "time", "name", "type"],
                   help="待下载清单排序：size 大小 / time 更新时间 / name 名称 / type 类型（默认 size）")
    p.add_argument("--top", type=int, default=15,
                   help="清单显示前几条（默认 15）")
    p.add_argument("--include-team", action="store_true",
                   help="连团队空间一起导。默认只导你自己创建的（owner）空间，"
                        "因为别人的团队空间往往有几百个用不上的示例资源文件")
    p.add_argument("--no-manifest", action="store_true", help="不写 _manifest.json")
    p.add_argument("--library-path", default="", help="显式指定 library skill 目录")
    p.add_argument("--token-stdin", action="store_true", default=True,
                   help=argparse.SUPPRESS)
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.library_path:
        sys.path.insert(0, args.library_path)

    CNF.overwrite = args.overwrite
    CNF.skip_existing = not args.overwrite

    c = cm()
    strip_stdin_bom()
    token = clean_token(c.acquire_token())
    if not token and not c.is_sandbox():
        print("[错误] 未拿到 token。请由上层 agent 先调 connect_open_platform，"
              "再把 token 从 stdin 首行传入。", file=sys.stderr)
        return 2

    account = (args.account or detect_account()).strip() or "default"
    out_root = Path(args.out).expanduser().resolve() / safe_name(account)
    out_root.mkdir(parents=True, exist_ok=True)
    print(f"[输出] {out_root}")
    print(f"[账户] {account}")

    try:
        spaces = list_spaces(token)
    except Exception as exc:
        print(f"[错误] 列空间失败: {exc}", file=sys.stderr)
        return 3

    if args.space_id:
        spaces = [s for s in spaces if s.get("spaceId") == args.space_id]
        if not spaces:
            print(f"[错误] 找不到空间 {args.space_id}", file=sys.stderr)
            return 4

    # 默认只导自己拥有（owner）的空间。团队空间多为别人创建的示例素材，
    # 动辄几百个文件，容易把备份撑爆且大多用不上。
    if not args.include_team:
        own = [s for s in spaces if s.get("role") == "owner"]
        skipped = [s for s in spaces if s.get("role") != "owner"]
        if skipped:
            print(f"[默认] 跳过 {len(skipped)} 个团队空间（非 owner）：")
            for s in skipped:
                print(f"       - {s.get('title') or '(未命名)'}  role={s.get('role')}")
            print("       要连同导出请加 --include-team")
        spaces = own

    if not spaces:
        print("[提示] 没有属于你自己的空间可用。若只想导团队空间，加 --include-team。")
        return 0

    if args.include_team:
        print("[范围] 全部空间（含团队）")
    else:
        print("[范围] 仅自己创建的空间（要包含团队空间加 --include-team）")

    # --probe：先报到再决定，不下载
    targets, meta = collect_targets(token, spaces, args)
    if args.probe:
        return run_probe(token, targets, meta, args)

    # --plan / --plan-only：先对账出「远端/本地/待下载」对比表，再决定要不要下
    if args.plan or args.plan_only:
        plan = build_plan(token, targets, args.workers)
        summary = render_plan(plan, args.sort, args.top,
                              title="导出计划" + ("（含团队空间）" if args.include_team else ""))
        if args.plan_only:
            # bat 场景：报表交给上层脚本，由它用 set /p 询问后决定是否再调一次下载
            print("\n  [报表结束，未下载任何文件]")
            if summary.get("pending"):
                print(f"  待下载：{summary['pending']} 条")
            return 0
        if not summary.get("pending"):
            print("\n  无需下载，已退出。")
            return 0
        if args.yes:
            print("\n  --yes 已指定，直接下载。")
        else:
            print(f"\n  即将下载 {summary['pending']} 条"
                  f"（约 {fmt_size(summary.get('pending_bytes', 0))}）"
                  "，其余本地已有会自动跳过。")
            if not ask_yes_no("  要下载吗？[Y/n] "):
                print("\n  已取消，没有下载任何文件。")
                print("    换个排序再看：  --plan --sort time / --sort name / --sort type")
                print("    少看几条：      --plan --top 30")
                print("    自动确认：      --plan --yes")
                return 0
            print()

    # --max-total：先估总量，超限就中止（同样不下载）
    if args.max_total:
        print(f"[门槛] 总量上限 {args.max_total:.0f} MB，先估算...")
        est_bytes = 0
        with ThreadPoolExecutor(max_workers=max(args.workers, 8)) as pool:
            futs = [pool.submit(probe_item_size, token, n) for n, _d in targets]
            for f in as_completed(futs):
                try:
                    b, _c, _p = f.result()
                    est_bytes += b
                except Exception:
                    pass
        est_mb = est_bytes / (1024 * 1024)
        print(f"[门槛] 预估总量 {fmt_size(est_bytes)}")
        if est_mb > args.max_total:
            print(f"\n  ✗ 预估 {fmt_size(est_bytes)} 超过上限 {args.max_total:.0f} MB，已中止，未下载任何文件。")
            print("    可选操作：")
            print(f"      · 调大上限    --max-total {int(est_mb * 1.2) + 100}")
            print(f"      · 只导轻量    --kinds doc,database")
            print(f"      · 排除大视频  --kinds doc,database,web")
            print(f"      · 只看明细    --probe")
            return 5
        print(f"[门槛] 未超限，开始下载。")

    started = time.time()
    by_space: Dict[str, List[Tuple[Dict[str, Any], Path]]] = {}
    for n, d in targets:
        by_space.setdefault(n.get("spaceId") or "", []).append((n, d))
    reports = [
        export_space(token, s, args, by_space.get(s["spaceId"], []),
                     (meta["spaces"].get(s["spaceId"]) or {}).get("nodes", 0))
        for s in spaces
    ]
    elapsed = time.time() - started

    total_ok = sum(r.get("ok", 0) for r in reports)
    total_fail = sum(r.get("failed", 0) for r in reports)
    total_skip = sum(r.get("skipped", 0) for r in reports)
    total_cached = sum(r.get("cached", 0) for r in reports)

    print("\n" + "=" * 60)
    parts = [f"新下载 {total_ok}"]
    if total_cached:
        parts.append(f"本地已有跳过 {total_cached}")
    if total_skip:
        parts.append(f"上限跳过 {total_skip}")
    if total_fail:
        parts.append(f"失败 {total_fail}")
    print("完成：" + "，".join(parts) + f"，用时 {elapsed:.1f}s")
    if total_cached and total_ok == 0:
        print("      全部文件本地已存在且大小一致，没有重复下载。")

    if not args.no_manifest:
        manifest = out_root / "_manifest.json"
        manifest.write_text(
            json.dumps(
                {"exportedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "outRoot": str(out_root), "spaces": reports},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
        print(f"索引已写入 {manifest}")

    if total_fail:
        print("\n失败明细：")
        for r in reports:
            for item in r.get("results", []):
                if item.get("status") == "failed":
                    print(f"  - [{item.get('kind')}] {item.get('title')}: {item.get('reason')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
