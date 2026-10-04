"""M4 验收：写链路的**钉住**路径 ``POST /endpoints/{id}/pin``（M4.2 / M4.9；决策 11 的两条路径）。

跑法（``tasks/progress.md`` §五 / ``prompt.md` §5.1，逐字照抄；venv 里没有 pytest 也不许装）：

    cd "H:/application/hermesnew/home/plugins/supplier-models"
    V="H:/application/hermesnew/hermes-agent/venv/Scripts/python.exe"
    T="$LOCALAPPDATA/Temp/providerchange-harness"
    rm -rf "$T" && mkdir -p "$T"
    cp H:/application/hermesnew/home/config.yaml "$T/config.yaml"
    cp H:/application/hermesnew/home/.env         "$T/.env"
    HERMES_HOME="$(cygpath -w "$T")" "$V" -m unittest discover -s tests -v
    rm -rf "$T"

两条路径**必须分开**（§6.3 决策 11），本文件按路径各测一遍：

* ``providers:`` 条目 → 官方 ``upsert_custom_endpoint``：回传完整字段（读自磁盘）、
  ``api_key`` 省略。判据（T18）：``models`` 内容**完全不变**（条数取运行时读到的值，
  **不硬编码**「22」）、``key_env`` 不变、``discover_models`` 变 ``False``。
  ⚠️ 这条路**不声称无损**（官方会 ``rstrip("/")`` / ``strip()`` / 重建 dict —— §6.3 v0.4）。
* legacy（``custom_providers:``，cc-switch 写的形态）→ 插件就地改一个字段。
  判据（T22，**无损性只绑这条路**）：**全文件逐行 diff 只有 ``discover_models`` 那一行**——
  键原本不存在时「仅 1 行新增、零删除」（本机快照 ``bailian`` / ``sensenova`` 就是这个形态），
  键原本为 ``true`` 时「1 增 1 删且两行都是 discover_models」；两种情况下**其它行必须逐行相同**。
  再叠加：``api_key`` 的 **sha256** 不变、``models:`` 的 ``name:`` 子字段不变、
  条目在序列里的**位置**不变（T21）、**不多出** ``providers:`` 条目（钉住 ≠ 迁移）。

其它纪律与 ``test_save.py`` 一致：写之前先过 ``_guard_before_write()`` 红线（只允许写
``HERMES_HOME`` 临时副本）、真实密钥只做哈希比对、零网络（钉住是纯 config 操作）、
计数一律「先从副本读、再断言不变」（prompt §2.7；``2026-09-22`` 快照 3+2 条只当注释）。
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import os
import re
import sys
import tempfile
import typing
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli.config import (
    get_config_path,
    get_env_path,
    invalidate_env_cache,
    load_config,
    read_raw_config,
    save_config,
)
from hermes_cli.web_server_profiles import _config_profile_scope

PRODUCT_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = PRODUCT_ROOT / "dashboard"
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

import plugin_api  # noqa: E402  （prompt §5.2：先把 dashboard 塞进 sys.path 再 import）

MOUNT_PREFIX = "/api/plugins/supplier-models"
LIST_PATH = f"{MOUNT_PREFIX}/endpoints"
PIN_PATH_TEMPLATE = f"{MOUNT_PREFIX}/endpoints/{{endpoint_id}}/pin"
# 钉住响应里不许出现的字段（密钥铁律，与 M1 的 KEY_MATERIAL_KEYS 同口径）
FORBIDDEN_RESPONSE_KEYS = ("api_key", "api_key_preview")
MISSING_PIN_ID = "zz-m4-pin-no-such-entry"


# --------------------------------------------------------------- 副本读写原语

def _home() -> Path:
    value = os.environ.get("HERMES_HOME")
    if not value:
        raise AssertionError("HERMES_HOME 未设置 —— 请用 progress §五 的测试骨架命令跑")
    return Path(value).resolve()


def _read_copy_bytes(name: str = "config.yaml") -> bytes:
    return (_home() / name).read_bytes()


def _write_copy_bytes(data: bytes, name: str = "config.yaml") -> None:
    (_home() / name).write_bytes(data)


def _raw_providers(raw: Dict[str, Any]) -> Dict[str, Any]:
    providers = (raw or {}).get("providers")
    return providers if isinstance(providers, dict) else {}


def _raw_legacy(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [entry for entry in ((raw or {}).get("custom_providers") or []) if isinstance(entry, dict)]


def _sha(value: Any) -> str:
    marker = "\x00<absent>" if value is None else value
    return hashlib.sha256(str(marker).encode("utf-8")).hexdigest()


def _split_eol(line: str) -> Tuple[str, str]:
    for ending in ("\r\n", "\n", "\r"):
        if line.endswith(ending):
            return line[:-len(ending)], ending
    return line, ""


def _entry_key_indent(lines: List[str], name: str) -> Optional[str]:
    """``custom_providers:`` 里 ``name: <name>`` 所属条目的**兄弟键缩进**（校验插入行形状）。"""
    header = re.compile(rf"^(\s*)(-\s+)?name:\s*{re.escape(name)}\s*$")
    for index, line in enumerate(lines):
        body, _eol = _split_eol(line)
        match = header.match(body)
        if not match:
            continue
        own = " " * len((match.group(1) or "") + (match.group(2) or ""))
        for follow in lines[index + 1:index + 12]:
            fbody, _feol = _split_eol(follow)
            if not fbody.strip() or fbody.strip().startswith("#"):
                continue
            key_match = re.match(r"^(\s*)[A-Za-z_][A-Za-z0-9_]*:", fbody)
            if key_match and len(key_match.group(1)) >= len(own):
                return key_match.group(1)
            break
        return "  "
    return None


def _diff_added_removed(before: List[str], after: List[str]) -> Tuple[List[str], List[str]]:
    """逐行 diff 的新增行 / 删除行（T22 判据形状；``@@`` 与 ``+++``/``---`` 头不计）。"""
    added: List[str] = []
    removed: List[str] = []
    for line in difflib.unified_diff(before, after, lineterm="", n=0):
        if line.startswith(("+++", "---", "@@")):
            continue
        if line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            removed.append(line[1:])
    return added, removed


def _content_lines(lines: List[str]) -> List[str]:
    """滤掉空行与纯注释行，只留 YAML 内容行。

    ⚠️ 为什么需要这一步（实测发现，写进决策日志）：官方 ``save_config`` 会把
    ``_commented_sections_for_save``（``config.py:2301``）生成的**注释示例块**
    （``security.redact_secrets`` 未设 → ``_SECURITY_COMMENT``；未配 fallback →
    ``_FALLBACK_COMMENT``）追加到文件末尾。本机 ``config.yaml`` 原本**一行注释都没有**
    （实测 ``grep -c '^#'`` = 0），所以**第一次**任何 ``save_config`` 都会多出这一坨
    注释——它是平台的既有行为、值层面无影响、且幂等（不会每次保存累加）。
    因此 T22 的「仅 1 行」判据只能落在**内容行**上；除此之外本文件还用
    :func:`_changed_paths` 做值级零改动断言，两道一起才算「无损」。
    """
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


def _changed_paths(before: Any, after: Any, path: str = "") -> List[str]:
    """两份解析后的 config 的**值级**差异路径（无损性判据的硬核版）。

    只报「路径 + 变化种类」，**绝不回显值** —— 副本里带着明文密钥，断言消息会进测试输出。
    变化种类：``+added`` / ``-removed`` / ``!changed``。
    """
    if isinstance(before, dict) and isinstance(after, dict):
        changes: List[str] = []
        for key in sorted(set(before) | set(after), key=str):
            child = f"{path}.{key}" if path else str(key)
            if key not in before:
                changes.append(f"{child} +added")
            elif key not in after:
                changes.append(f"{child} -removed")
            else:
                changes.extend(_changed_paths(before[key], after[key], child))
        return changes
    if isinstance(before, list) and isinstance(after, list):
        if len(before) != len(after):
            return [f"{path} -removed" if not after else f"{path} +added"
                    if not before else f"{path}[len] !changed"]
        merged: List[str] = []
        for index, (left, right) in enumerate(zip(before, after)):
            merged.extend(_changed_paths(left, right, f"{path}[{index}]"))
        return merged
    return [] if before == after else [f"{path} !changed"]


class PinRouteTest(unittest.TestCase):
    """M4.2 / M4.6 / M4.9：两条钉住路径、批量钉住、报错不动手。"""

    @classmethod
    def setUpClass(cls):
        app = FastAPI()
        app.include_router(plugin_api.router, prefix=MOUNT_PREFIX)
        cls.client = TestClient(app)
        cls.pristine_config = _read_copy_bytes("config.yaml")
        cls.env_exists = (_home() / ".env").exists()
        cls.pristine_env = _read_copy_bytes(".env") if cls.env_exists else None

    # ---------------------------------------------------------------- 基础设施

    def setUp(self):
        self._guard_before_write()
        self.assertEqual(str((_home() / ".env").resolve()).lower(),
                         str(Path(get_env_path()).resolve()).lower(),
                         "get_env_path() 不在副本上，写测试会碰真配置")
        self._restore_copy()
        self._refresh()

    def tearDown(self):
        self._restore_copy()

    def _guard_before_write(self) -> None:
        home = _home()
        config_path = Path(get_config_path()).resolve()
        self.assertTrue(home.is_relative_to(Path(tempfile.gettempdir()).resolve()),
                        f"写入前守卫失败：HERMES_HOME={home} 不在系统临时目录下")
        self.assertTrue(str(config_path).lower().startswith(str(home).lower()),
                        f"写入前守卫失败：get_config_path()={config_path} 不在副本 {home} 之下")
        self.assertEqual(str(home / "config.yaml").lower(), str(config_path).lower())

    def _restore_copy(self):
        if _read_copy_bytes("config.yaml") != self.pristine_config:
            _write_copy_bytes(self.pristine_config, "config.yaml")
        self.assertEqual(self.pristine_config, _read_copy_bytes("config.yaml"),
                         "副本 config.yaml 没能还原成字节级原样")
        if self.pristine_env is not None:
            if _read_copy_bytes(".env") != self.pristine_env:
                _write_copy_bytes(self.pristine_env, ".env")
            invalidate_env_cache()
            self.assertEqual(self.pristine_env, _read_copy_bytes(".env"), "副本 .env 被写坏了")

    def _refresh(self) -> None:
        """重读副本（造过前置状态之后必须调，否则取材快照是旧的）。"""
        self.raw = read_raw_config()
        self.providers = _raw_providers(self.raw)
        self.legacy = _raw_legacy(self.raw)
        self.assertTrue(self.providers and self.legacy,
                        "副本里没有同时存在 providers: 与 custom_providers: 条目 —— "
                        "两条钉住路径无从验证，先按 progress §五 重建 harness 副本")

    # ------------------------------------------------------------------ 取材

    def _providers_pin_sample(self) -> str:
        """一个「未钉住 + 默认模型已在白名单里 + **id 已稳定**」的 ``providers:`` 条目 id（T18 样本）。

        id 稳定是前提：官方 ``_write_custom_endpoint:439`` 会再跑一遍 ``_custom_endpoint_id``，
        key 里带非法字符时它会**换名**（pop 旧 key 写新 key）—— 那正是 §6.3 说
        「官方路径不承诺无损」的原因之一，不该混进本用例的判据里。

        本机若全部已钉住，就用共享口径把其中一条改回 ``discover_models: true``
        —— 那正是 T06（官方页保存一次把钉住摘掉）的模拟，钉住要能把它修回去（T07 / F9）。
        """
        id_stable = re.compile(r"^[a-z0-9_-]+$")
        candidates = [(pid, entry) for pid, entry in sorted(self.providers.items())
                      if isinstance(entry, dict)
                      and id_stable.match(pid) and pid == pid.strip("-_")
                      and str(entry.get("model") or "") in (entry.get("models") or {})]
        self.assertTrue(candidates,
                        "副本里没有「id 稳定 + 默认模型已在白名单里」的 providers: 条目，"
                        "T18 的 models 不变判据无从验证")
        unpinned = [pid for pid, entry in candidates if entry.get("discover_models") is True]
        if unpinned:
            return unpinned[0]
        victim = candidates[0][0]
        self._set_providers_discover_models(victim, True)     # 模拟 T06
        return victim

    def _legacy_pin_sample(self) -> Tuple[str, bool]:
        """一个未钉住的 legacy 条目 → ``(name, 原本有没有 discover_models 键)``（T21/T22 样本）。

        本机快照里 ``bailian`` / ``sensenova`` 都**没有**这个键（cc-switch 不写它），
        所以钉住的正是一行新增；键已在且为 ``true`` 时退化成「1 增 1 删」，
        两种都算「除该字段外零改动」。
        """
        without = [str(e.get("name") or "").strip() for e in self.legacy
                   if e.get("discover_models") is None and str(e.get("name") or "").strip()]
        if without:
            return without[0], False
        turned_on = [str(e.get("name") or "").strip() for e in self.legacy
                     if e.get("discover_models") is True and str(e.get("name") or "").strip()]
        if turned_on:
            return turned_on[0], True
        victim = str(self.legacy[0].get("name") or "").strip()
        self.assertTrue(victim, "副本里的 legacy 条目没有 name，钉住路径无从验证")
        raise AssertionError("副本里的 legacy 条目全都已钉住且带显式 false —— 无法构造未钉住前置状态")

    def _set_providers_discover_models(self, endpoint_id: str, value: bool) -> None:
        """共享写口径（M4↔M5↔M6）：``load_config()`` → 定点改一个字段 → **一次** ``save_config()``。

        只用来在副本上**造前置状态**（模拟 T06）；被测对象仍是插件的钉住路由。
        """
        self._guard_before_write()
        with _config_profile_scope(None):
            cfg = load_config()
            entry = _raw_providers(cfg).get(endpoint_id)
            self.assertIsInstance(entry, dict, f"副本里没有 providers: 条目 {endpoint_id}")
            entry["discover_models"] = value
            save_config(cfg)
        self._refresh()
        self.assertIs(value, self.providers.get(endpoint_id, {}).get("discover_models"),
                      "前置状态没写进副本，后续断言失去意义")

    def _rows(self) -> List[Dict[str, Any]]:
        response = self.client.get(LIST_PATH)
        self.assertEqual(200, response.status_code, response.text)
        return response.json().get("endpoints") or []

    def _row_of(self, endpoint_id: str) -> Optional[Dict[str, Any]]:
        rows = [row for row in self._rows() if str(row.get("id") or "") == endpoint_id]
        self.assertLessEqual(len(rows), 1, f"GET /endpoints 里 {endpoint_id} 出现多行")
        return rows[0] if rows else None

    def _pin(self, endpoint_id: str, base_url: Optional[str] = None):
        """``POST /endpoints/{id}/pin``；``base_url=None`` → **不发请求体**（可选 body 契约）。"""
        self._guard_before_write()
        url = PIN_PATH_TEMPLATE.format(endpoint_id=endpoint_id)
        with mock.patch.object(plugin_api, "validate_custom_endpoint",
                               side_effect=AssertionError("钉住链路不许发探测请求（纯 config 操作）")):
            if base_url is None:
                return self.client.post(url)
            return self.client.post(url, json={"base_url": base_url})

    # ------------------------------------------------ M4.2 路径一：providers: → 官方 upsert

    def test_pin_providers_entry_keeps_models_identical(self):
        """T18 / T07 / F9：``providers:`` 条目钉住后 ``models`` **完全不变**、``key_env`` 不变、
        ``discover_models`` 为 ``False``。

        「钉住只阻止未来的覆盖，不会清理已经灌进去的模型」（UC-11 / 决议 3）：本机快照里
        ``token_rhythm`` 那 22 条就是被灌进来的，钉住后**仍**是那个条数（运行时读，不硬编码）。
        """
        endpoint_id = self._providers_pin_sample()
        raw_before = read_raw_config()
        before = copy.deepcopy(self.providers[endpoint_id])
        models_before = copy.deepcopy(before.get("models") or {})
        self.assertTrue(models_before, "样本条目的白名单为空，models 不变判据没有意义")
        self.assertIs(True, before.get("discover_models"), "样本必须未钉住，否则 T07 没有修复语义")

        response = self._pin(endpoint_id)
        self.assertEqual(200, response.status_code, response.text)
        payload = response.json()
        self.assertTrue(payload.get("ok"), payload)
        self.assertEqual(plugin_api.PIN_PATH_PROVIDERS, payload.get("pin_path"),
                         "providers: 条目必须走官方 upsert 那条路（决策 11）")
        self.assertIs(False, payload.get("discover_models"))

        raw_after = read_raw_config()
        # 本机样本实测：官方路径这一次也恰好只改了 discover_models 一个字段。
        # ⚠️ 这是**实测事实**、不是官方路径的承诺（§6.3 v0.4：它会 rstrip/strip/重建 dict，
        # 还会把条目里的明文 api_key 搬进 .env）；无损性判据（T22）绑的是 legacy 就地那条路。
        self.assertEqual([f"providers.{endpoint_id}.discover_models !changed"],
                         _changed_paths(raw_before, raw_after),
                         "官方钉住路径在本机样本上改到了 discover_models 以外的字段")
        after = copy.deepcopy(_raw_providers(raw_after).get(endpoint_id) or {})
        self.assertIs(False, after.get("discover_models"), "钉住没落盘")
        self.assertEqual(models_before, after.get("models"),
                         "钉住改到了 models 内容（应当只改 discover_models）")
        self.assertEqual(len(models_before), len(after.get("models") or {}),
                         "白名单条数变了（先读再断言，不硬编码条数）")
        self.assertEqual(before.get("key_env"), after.get("key_env"), "钉住不许动 key_env")
        self.assertNotIn("api_key", after, "钉住不许把密钥写成明文")
        self.assertEqual(before.get("model"), after.get("model"))
        self.assertEqual(before.get("name"), after.get("name"))
        self.assertEqual(before.get("base_url"), after.get("base_url"))
        # 前端徽章的判据：M2 渲染、M4 写入，共享同一个 discover_models 字段
        self.assertIs(False, self._row_of(endpoint_id).get("discover_models"))

    def test_pin_providers_entry_is_idempotent_and_leaves_others_alone(self):
        """连点两次「钉住」结果一致；期间**其它条目一个字段都不动**。"""
        endpoint_id = self._providers_pin_sample()
        others_before = {pid: copy.deepcopy(entry)
                         for pid, entry in self.providers.items() if pid != endpoint_id}
        legacy_before = copy.deepcopy(self.legacy)

        first = self._pin(endpoint_id)
        snapshot = copy.deepcopy(_raw_providers(read_raw_config()).get(endpoint_id) or {})
        second = self._pin(endpoint_id)
        self.assertEqual(200, first.status_code, first.text)
        self.assertEqual(200, second.status_code, second.text)
        self.assertEqual(snapshot, _raw_providers(read_raw_config()).get(endpoint_id),
                         "第二次钉住改了东西（钉住应当是幂等的）")

        providers_after = _raw_providers(read_raw_config())
        for pid, entry in others_before.items():
            self.assertEqual(entry, providers_after.get(pid),
                             f"钉住 {endpoint_id} 时改到了无关条目 {pid}")
        self.assertEqual(legacy_before, _raw_legacy(read_raw_config()),
                         "钉住 providers: 条目不该碰 custom_providers:")

    def test_pin_providers_entry_missing_is_404_and_writes_nothing(self):
        """条目不存在 → 404「不动手」（列表过期时不能凭空造条目）。"""
        config_before = _read_copy_bytes("config.yaml")
        env_before = _read_copy_bytes(".env")
        response = self._pin(MISSING_PIN_ID)
        self.assertEqual(404, response.status_code, response.text)
        self.assertIn(MISSING_PIN_ID, (response.json() or {}).get("detail") or "",
                      "错误原文要带上定位不到的 id（前端 toast 的判据）")
        self.assertEqual(config_before, _read_copy_bytes("config.yaml"), "404 之后副本被改了")
        self.assertEqual(env_before, _read_copy_bytes(".env"), "404 之后 .env 被改了")

    def test_pin_does_not_touch_top_level_model_mirror(self):
        """钉住不切换当前供应商：两条路径都不许动顶层 ``model:``（``make_default`` 恒 False）。"""
        providers_id = self._providers_pin_sample()
        legacy_name, _had_key = self._legacy_pin_sample()
        model_before = copy.deepcopy(read_raw_config().get("model"))
        self.assertEqual(200, self._pin(providers_id).status_code)
        self.assertEqual(200, self._pin(f"{plugin_api.CC_ID_PREFIX}{legacy_name}").status_code)
        self.assertEqual(model_before, copy.deepcopy(read_raw_config().get("model")),
                         "钉住写到了顶层 model:（那等于切换当前供应商）")

    # --------------------------------------- M4.2 路径二：legacy → 插件就地改一个字段（T22）

    def test_pin_legacy_entry_diff_is_only_the_discover_line(self):
        """T22 / T21（**无损性只绑这条路径**）：全文件逐行 diff 只涉及 ``discover_models`` 那一行；
        ``api_key`` 的 sha256 不变；``models:`` 的 ``name:`` 子字段不变；条目位置不变；
        不多出 ``providers:`` 条目；``.env`` 一字节都不动。
        """
        name, had_key = self._legacy_pin_sample()
        before_text = _read_copy_bytes("config.yaml").decode("utf-8")
        before_lines = before_text.splitlines(keepends=True)
        raw_before = read_raw_config()
        entry_before = plugin_api._locate_legacy_entries(raw_before.get("custom_providers"), name)[0]
        names_before = [str(e.get("name") or "") for e in _raw_legacy(raw_before)]
        index_before = names_before.index(name)
        key_sha_before = _sha(entry_before.get("api_key"))
        models_before = {mid: dict(meta or {})
                         for mid, meta in (entry_before.get("models") or {}).items()}
        providers_before = copy.deepcopy(_raw_providers(raw_before))
        env_before = _read_copy_bytes(".env")

        response = self._pin(f"{plugin_api.CC_ID_PREFIX}{name}",
                             base_url=plugin_api._entry_base_url(entry_before))
        self.assertEqual(200, response.status_code, response.text)
        payload = response.json()
        self.assertEqual(plugin_api.PIN_PATH_LEGACY, payload.get("pin_path"),
                         "legacy 条目必须走插件就地改字段那条路，不能走官方 upsert（决策 11）")
        self.assertIs(False, payload.get("discover_models"))

        after_lines = _read_copy_bytes("config.yaml").decode("utf-8").splitlines(keepends=True)
        added, removed = _diff_added_removed(before_lines, after_lines)
        content_added = _content_lines(added)
        content_removed = _content_lines(removed)
        if had_key:
            self.assertEqual(["discover_models: false"], content_added,
                             f"键已存在时内容行应当只换这一行：{content_added}")
            self.assertEqual(["discover_models: true"], content_removed,
                             f"应当只少一行 true：{content_removed}")
        else:
            self.assertEqual([], content_removed,
                             f"T22 要求内容行零删除（键原本不存在）：{content_removed[:5]}")
            self.assertEqual(["discover_models: false"], content_added,
                             f"T22：内容行 diff 应当只有 1 行新增，实到 {content_added[:5]}")
            indent = _entry_key_indent(before_lines, name)
            self.assertIsNotNone(indent, "取材失败：副本里找不到该 legacy 条目的键缩进")
            inserted = next(line for line in added if line.strip() == "discover_models: false")
            self.assertEqual(indent, inserted[:len(indent)],
                             "插入行的缩进必须与同条目的兄弟键一致（YAML 结构没被重排）")
        # 唯一的例外：平台在文件尾追加**纯注释**的示例块（见 _content_lines 的说明）。
        # 逐行核对：内容行以外的新增必须全是注释/空行，删除行同理。
        extra_added = [line for line in added if line.strip() and line.strip() not in content_added]
        self.assertTrue(all(line.strip().startswith("#") for line in extra_added),
                        f"内容行以外的新增必须全是注释：{[l[:40] for l in extra_added[:4]]}")
        self.assertTrue(all(not line.strip() or line.strip().startswith("#") for line in removed),
                        f"内容行以外的删除不该有：{[l[:40] for l in removed[:4]]}")

        raw_after = read_raw_config()
        # 值级零改动（比逐行更硬）：整份 config 里**只有**被钉住那条的 discover_models 变了
        self.assertEqual([f"custom_providers[{index_before}].discover_models"
                          + (" !changed" if had_key else " +added")],
                         _changed_paths(raw_before, raw_after),
                         "就地钉住改到了 discover_models 以外的字段（T22 无损性判据）")
        entry_after = plugin_api._locate_legacy_entries(raw_after.get("custom_providers"), name)[0]
        self.assertIs(False, entry_after.get("discover_models"))
        self.assertEqual(key_sha_before, _sha(entry_after.get("api_key")),
                         "明文密钥的 sha256 变了（就地钉住不许碰 api_key）")
        self.assertEqual(
            models_before,
            {mid: dict(meta or {}) for mid, meta in (entry_after.get("models") or {}).items()},
            "models: 内容或 name: 子字段变了（钉住只阻止未来覆盖）")
        self.assertEqual(index_before,
                         [str(e.get("name") or "") for e in _raw_legacy(raw_after)].index(name),
                         "条目在 custom_providers: 里换了位置")
        self.assertEqual(providers_before, _raw_providers(raw_after),
                         "钉住 legacy 条目多出了 providers: 条目（那是迁移，不是钉住）")
        self.assertEqual(env_before, _read_copy_bytes(".env"), "就地钉住不该写 .env")

        # 幂等：再钉一次应当**一个字节都不动**（顺带证明平台的注释块不会每次保存累加）
        second = self._pin(f"{plugin_api.CC_ID_PREFIX}{name}",
                           base_url=plugin_api._entry_base_url(entry_after))
        self.assertEqual(200, second.status_code, second.text)
        self.assertEqual(after_lines,
                         _read_copy_bytes("config.yaml").decode("utf-8").splitlines(keepends=True),
                         "第二次钉住又写了文件（钉住不幂等 / 注释块在累加）")

    def test_pin_legacy_entry_without_body_locates_by_name(self):
        """可选 body 契约：不发请求体时按 name 唯一定位也能钉住（name 唯一是前提）。"""
        name, _had_key = self._legacy_pin_sample()
        response = self._pin(f"{plugin_api.CC_ID_PREFIX}{name}")
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(plugin_api.PIN_PATH_LEGACY, response.json().get("pin_path"))
        entry = plugin_api._locate_legacy_entries(read_raw_config().get("custom_providers"), name)[0]
        self.assertIs(False, entry.get("discover_models"))
        row = self._row_of(f"{plugin_api.CC_ID_PREFIX}{name}")
        self.assertIs(False, (row or {}).get("discover_models"), "M1 的行没如实反映钉住状态（徽章不会消失）")

    def test_pin_legacy_entry_wrong_base_url_is_404_and_writes_nothing(self):
        """``(name, base_url)`` 定位不到 → 404，**不动手**（不猜目标）。"""
        name, _had_key = self._legacy_pin_sample()
        config_before = _read_copy_bytes("config.yaml")
        response = self._pin(f"{plugin_api.CC_ID_PREFIX}{name}",
                             base_url="http://127.0.0.1:59999/definitely-not-this-endpoint")
        self.assertEqual(404, response.status_code, response.text)
        self.assertIn(name, (response.json() or {}).get("detail") or "")
        self.assertEqual(config_before, _read_copy_bytes("config.yaml"),
                         "定位失败却写了副本（「报错不动手」破防）")

    def test_pin_legacy_entry_duplicate_is_409_and_writes_nothing(self):
        """同名同端点多条 → 409，**不动手**（§6.3「匹配到多条 → 报错」）。"""
        name, _had_key = self._legacy_pin_sample()
        self._guard_before_write()
        with _config_profile_scope(None):
            cfg = load_config()
            sequence = cfg.get("custom_providers")
            original = plugin_api._locate_legacy_entries(sequence, name)[0]
            clone = copy.deepcopy(original)
            clone["model"] = str(clone.get("model") or "") + "-duplicate-probe"
            sequence.append(clone)
            save_config(cfg)
        self._refresh()
        self.assertEqual(2, len(plugin_api._locate_legacy_entries(
            read_raw_config().get("custom_providers"), name)), "重名前置状态没造出来")

        config_before = _read_copy_bytes("config.yaml")
        response = self._pin(f"{plugin_api.CC_ID_PREFIX}{name}",
                             base_url=plugin_api._entry_base_url(original))
        self.assertEqual(409, response.status_code, response.text)
        self.assertIn(str(name), (response.json() or {}).get("detail") or "")
        self.assertEqual(config_before, _read_copy_bytes("config.yaml"),
                         "多条匹配却写了副本（「报错不动手」破防）")

    def test_pin_legacy_id_without_name_is_400(self):
        """只有 ``cc:`` 前缀、没有条目名 → 400（不拿空前缀去扫全表）。"""
        config_before = _read_copy_bytes("config.yaml")
        response = self._pin(plugin_api.CC_ID_PREFIX)
        self.assertEqual(400, response.status_code, response.text)
        self.assertEqual(config_before, _read_copy_bytes("config.yaml"))

    # ------------------------------------------------------------- M4.6 / UC-11 批量钉住

    def test_batch_pin_sequentially_pins_every_unpinned_row(self):
        """UC-11 的后端半边：逐条调 pin 能把**全部**未钉住行修好，且每条的白名单内容不变。

        前端 ``runBatchPin`` 就是这个循环（顺序执行、失败逐条列出、成功保留），
        所以这里用真实路由逐条走一遍，不 mock 写入。
        """
        self._providers_pin_sample()          # 保证至少有 1 条未钉住的 providers: 条目（快照口径）
        self._legacy_pin_sample()             # 保证至少有 1 条未钉住的 legacy 条目
        rows = self._rows()
        self.assertTrue(rows, "副本里一个供应商都没有，批量钉住无从验证")
        models_by_id = {str(row.get("id")): copy.deepcopy(row.get("models") or []) for row in rows}
        unpinned = [str(row.get("id")) for row in rows if row.get("discover_models") is not False]
        self.assertTrue(unpinned, "批量目标为空 —— 前置状态没造出来，本用例无从验证")

        failures: List[Tuple[str, Any]] = []
        for endpoint_id in unpinned:                       # 顺序执行，与前端一致
            response = self._pin(endpoint_id)
            if response.status_code != 200:
                failures.append((endpoint_id, (response.json() or {}).get("detail")))
        self.assertEqual([], failures,
                         "批量钉住里有条目失败（前端会把失败逐条列出并保留成功项，这里等价于用例失败）")
        self.assertLessEqual(len(unpinned), len(rows))

        after = {str(row.get("id")): row for row in self._rows()}
        self.assertEqual(set(models_by_id), set(after), "钉住后条目集变了（不许新增/丢失供应商）")
        for endpoint_id, models in models_by_id.items():
            self.assertIs(False, after[endpoint_id].get("discover_models"),
                          f"{endpoint_id} 钉不住（行里仍是 discover_models != False）")
            self.assertEqual(models, after[endpoint_id].get("models") or [],
                             f"{endpoint_id} 的白名单内容被钉住改动了（钉住不清理已灌入的模型）")

    # --------------------------------------------------------------- 路由契约（M5+ 参照）

    def test_pin_route_contract_optional_body_profile_and_no_key_material(self):
        """契约：``(endpoint_id, body: Optional[EndpointPinRequest]=None, profile=None)``，响应无密钥材料。"""
        import inspect

        signature = inspect.signature(plugin_api.pin_endpoint)
        self.assertEqual(["endpoint_id", "body", "profile"], list(signature.parameters))
        self.assertIsNone(signature.parameters["profile"].default)
        self.assertIsNone(signature.parameters["body"].default)
        hints = typing.get_type_hints(plugin_api.pin_endpoint)
        self.assertEqual(Optional[plugin_api.EndpointPinRequest], hints["body"])
        self.assertEqual("", plugin_api.EndpointPinRequest.model_fields["base_url"].default)
        self.assertEqual("/endpoints/{endpoint_id}/pin", plugin_api.PIN_PATH)
        self.assertEqual("cc:", plugin_api.CC_ID_PREFIX)

        endpoint_id = self._providers_pin_sample()
        response = self._pin(endpoint_id)
        self.assertEqual(200, response.status_code, response.text)
        self.assertNotIn("api_key_preview", response.text)
        for row in (response.json().get("endpoints") or []):
            for key in FORBIDDEN_RESPONSE_KEYS:
                self.assertNotIn(key, row, f"钉住响应里出现了密钥材料字段 {key}")

    def test_legacy_locator_helper_contract_for_m5_m6(self):
        """共享 helper 口径（M4↔M5↔M6）：``_locate_legacy_entries`` 返回候选列表，不猜、不写。"""
        name, _had_key = self._legacy_pin_sample()
        sequence = read_raw_config().get("custom_providers")
        entry = plugin_api._locate_legacy_entries(sequence, name)[0]
        self.assertEqual([entry], plugin_api._locate_legacy_entries(
            sequence, name.upper(), plugin_api._entry_base_url(entry)))
        self.assertEqual([], plugin_api._locate_legacy_entries(sequence, "   "))
        self.assertEqual([], plugin_api._locate_legacy_entries(sequence, name, "http://nope.invalid/v1"))
        self.assertEqual([], plugin_api._locate_legacy_entries(None, name))
        # 只读 helper 不许改副本
        self.assertEqual(self.pristine_config, _read_copy_bytes("config.yaml"))


if __name__ == "__main__":
    unittest.main()
