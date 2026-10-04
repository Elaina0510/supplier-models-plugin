"""M7 验收：危险动作 ``POST /endpoints/{id}/activate`` + ``DELETE /endpoints/{id}``
（M7.1 / M7.2 / M7.7；锚点 F8 / T14 的自动化半边 + M7.3–M7.6 的前端静态半边）。

跑法（``tasks/progress.md`` §五 / ``prompt.md`` §5.1，逐字照抄；venv 里没有 pytest 也不许装）：

    cd "H:/application/hermesnew/home/plugins/supplier-models"
    V="H:/application/hermesnew/hermes-agent/venv/Scripts/python.exe"
    T="$LOCALAPPDATA/Temp/providerchange-harness"
    rm -rf "$T" && mkdir -p "$T"
    cp H:/application/hermesnew/home/config.yaml "$T/config.yaml"
    cp H:/application/hermesnew/home/.env         "$T/.env"
    HERMES_HOME="$(cygpath -w "$T")" "$V" -m unittest discover -s tests -v
    rm -rf "$T"

本文件测的是**两条薄包装**（设计 §6.3），所以判据全部对着「官方那两条 route 的既有语义」写，
不对着插件的想象写（逐条读源码所得，另见 ``plugin_api.py`` 的 M7 区块头注）：

* **activate**（``config_env.py:557-594``）：在 ``providers:`` 里定位条目 → 把条目的
  ``model`` / ``base_url`` / ``key_env``（没有 ``key_env`` 时才退到 ``api_key``）经
  ``_validated_main_model_selection`` + ``_apply_main_model_assignment`` 写进**顶层**
  ``model:`` → 一次 ``save_config`` → 返回 ``{ok, provider, model}``。
  ⇒ 磁盘上的正向判据：顶层 ``model.provider`` / ``default`` / ``base_url`` / ``key_env``
  与该条目自己的字段**逐值一致**（这就是 F8「主模型切换生效」在无人值守下可证的那一半）。
* **delete**（``config_env.py:598-618``）：一次 ``save_config`` 里做三件事 ——
  ① ``providers.pop(stored_key)``；② ``remove_env_value(custom_endpoint_key_env(slug))``
  即**清掉 ``.env`` 里那份密钥**；③ :func:`_detach_main_model_from_provider`
  （``:418-435``）**仅当**顶层 ``model.provider``（strip + lower）恰等于该条目的 slug 时，
  才摘掉 ``provider`` / ``base_url`` / ``api_key`` / ``key_env`` 四个键。
  ⇒ T14 的**两支都钉**：指向被删条目 → 镜像摘净；指向别处 → 顶层 ``model:`` 一个键都不动。
* **决议 12 的拒绝面**：``cc:`` 前缀（= 住在 ``custom_providers:`` 的 cc-switch 条目）在
  **进官方之前**就被拒（400 + 中文 detail），并逐字节证明副本 ``config.yaml`` / ``.env``
  没被动过一个字节 —— 官方自己只会给一句英文 ``custom endpoint not found``。

三条红线与 M4 / M5 / M6 一致：任何写入之前先过 :meth:`ActivateRouteTest._guard_before_write`
（只允许写 ``HERMES_HOME`` 临时副本）；每个破坏性用例在 ``setUp`` / ``tearDown`` 各把副本
还原成 ``setUpClass`` 的原始字节（**fresh copy 口径**）；真实密钥只做哈希比对；零网络
（把 ``validate_custom_endpoint`` 换成「一调用就炸」的桩，证明这两条链路不发探测请求）。
计数与 id 一律「先从副本读、再断言」（prompt §2.7 —— 2026-09-22 快照的 3 条 ``providers:``
+ 2 条 cc-switch 只当注释，永不当期望值）。
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import inspect
import os
import re
import sys
import tempfile
import typing
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from hermes_cli.config import (
    custom_endpoint_key_env,
    get_config_path,
    get_env_path,
    invalidate_env_cache,
    read_raw_config,
)

PRODUCT_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = PRODUCT_ROOT / "dashboard"
DESKTOP_DIR = PRODUCT_ROOT / "desktop"
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

import plugin_api  # noqa: E402  （prompt §5.2：先把 dashboard 塞进 sys.path 再 import）

MOUNT_PREFIX = "/api/plugins/supplier-models"
LIST_PATH = f"{MOUNT_PREFIX}/endpoints"                     # M1 的只读双读路由
SAVE_PATH = f"{MOUNT_PREFIX}/endpoints"                     # M4 的保存路由（造前置状态用）
ACTIVATE_PATH_TEMPLATE = f"{MOUNT_PREFIX}/endpoints/{{endpoint_id}}/activate"
DELETE_PATH_TEMPLATE = f"{MOUNT_PREFIX}/endpoints/{{endpoint_id}}"
# 路由表里**不带**挂载前缀的原样路径（设计 §6.3 冻结的形状）
ACTIVATE_ROUTE_PATH = "/endpoints/{endpoint_id}/activate"
DELETE_ROUTE_PATH = "/endpoints/{endpoint_id}"

# 密钥铁律（M1.5 / progress §十五 决策 2）：响应任何字段里都不许出现这两个键
FORBIDDEN_RESPONSE_KEYS = ("api_key", "api_key_preview")
# 造前置状态用的合成条目：``zz-`` 前缀不与本机真实条目撞车；``127.0.0.1`` 即使误接网络
# 也连不到任何外部主机（这两条链路本身一个请求都不发，桩就是它的证明）
FAKE_BASE_URL = "http://127.0.0.1:55998/v1"
FAKE_KEY_PREFIX = "sk-FAKE-TEST-"
FAKE_MODEL = "fake/m7-model-a"
M7_SAMPLE_ID_A = "zz-m7-danger-a"
M7_SAMPLE_ID_B = "zz-m7-danger-b"
MISSING_ID = "zz-m7-no-such-entry"
BLANK_SEGMENT = "%20"           # 路径段里的「空白 id」（后端 strip 之后为空 → 400）
# 官方 ``_detach_main_model_from_provider`` 唯动的那四个顶层 ``model:`` 键（:433）。
# 本文件**自己另写一份**、不 import 后端的常量：期望值与实装值必须来自两处代码，
# 否则后端悄悄改了判据、这条测试还是绿的（prompt §2.7 的反漂移口径）。
MIRROR_FIELDS_EXPECTED = ("provider", "base_url", "api_key", "key_env")
# 任务书点名的优先取材对象（2026-09-22 快照里带 model + base_url 的 providers: 条目）；
# ⚠️ 只是优先级，拿不到就退到「任一满足条件的条目」
PREFERRED_PROVIDER_IDS = ("token_rhythm", "amd", "zhipu-glm")


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


def _split_eol(line: str) -> Tuple[str, str]:
    for ending in ("\r\n", "\n", "\r"):
        if line.endswith(ending):
            return line[:-len(ending)], ending
    return line, ""


def _env_value(text: str, name: str) -> Optional[str]:
    """副本 ``.env`` 文本里 ``NAME`` 的值（去引号）；没有这个变量 → ``None``。"""
    for line in text.splitlines():
        body, _eol = _split_eol(line)
        match = re.match(rf"^\s*(?:export\s+)?{re.escape(name)}=(.*)$", body)
        if match:
            return match.group(1).strip().strip('"').strip("'")
    return None


def _env_names(text: str) -> List[str]:
    """副本 ``.env`` 里出现过的变量名（**只要名字**，值一律不回传）。"""
    names = []
    for line in text.splitlines():
        body, _eol = _split_eol(line)
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=", body)
        if match:
            names.append(match.group(1))
    return names


def _raw() -> Dict[str, Any]:
    return read_raw_config()


def _raw_providers(raw: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    providers = (raw if raw is not None else _raw()).get("providers")
    return providers if isinstance(providers, dict) else {}


def _raw_legacy(raw: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    source = raw if raw is not None else _raw()
    return [entry for entry in (source.get("custom_providers") or []) if isinstance(entry, dict)]


def _raw_model(raw: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """顶层 ``model:`` 的**磁盘原值**（未展开）—— 镜像判据只认这一份。"""
    model_cfg = (raw if raw is not None else _raw()).get("model")
    return model_cfg if isinstance(model_cfg, dict) else {}


def _raw_entry(raw: Dict[str, Any], endpoint_id: str) -> Dict[str, Any]:
    """按**官方同一条**定位链取 ``providers:`` 条目（返回副本用于比对）。"""
    _stored, entry = plugin_api.find_provider_entry((raw or {}).get("providers"), endpoint_id)
    return copy.deepcopy(entry) if isinstance(entry, dict) else {}


def _entry_base_url(entry: Dict[str, Any]) -> str:
    return str((entry or {}).get("base_url") or (entry or {}).get("url") or "").strip()


def _sha(value: Any) -> str:
    """密钥材料只做哈希比对（断言消息里绝不落明文）。"""
    marker = "\x00<absent>" if value is None else str(value)
    return hashlib.sha256(marker.encode("utf-8")).hexdigest()


def _changed_paths(before: Any, after: Any, path: str = "") -> List[str]:
    """两份解析后 config 的**值级**差异路径（只报「路径 + 变化种类」，**绝不回显值** ——
    副本里带着明文密钥，断言消息会进测试输出）。判据口径与 M5 / M6 相同：按值级 +
    内容行比，不按「逐行 diff 只有一行」（批 2 决策 3）。"""
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
            return [f"{path}[len] !changed"]
        merged: List[str] = []
        for index, (left, right) in enumerate(zip(before, after)):
            merged.extend(_changed_paths(left, right, f"{path}[{index}]"))
        return merged
    return [] if before == after else [f"{path} !changed"]


def _save_payload(endpoint_id: str, name: str, model: str,
                  api_key: Optional[str] = None) -> Dict[str, Any]:
    """``buildSavePayload`` 的等价写法（M4 §7.7 的 payload 形状）。"""
    body: Dict[str, Any] = {
        "base_url": FAKE_BASE_URL,
        "discover_models": False,
        "id": endpoint_id,
        "make_default": False,
        "model": model,
        "models": [model],
        "name": name,
    }
    if api_key is not None:
        body["api_key"] = api_key
    return body


def _strip_python_comments(text: str) -> str:
    """粗粒度去注释：摘掉 ``#`` 行注释与 docstring，只留可执行代码。

    本文件只需要这个粒度（判「M7 区块里有没有把别人的 helper 再定义一遍」），
    注释里出现的字样不算数。
    """
    kept: List[str] = []
    in_doc = False
    for line in text.splitlines():
        stripped = line.strip()
        if in_doc:
            if '"""' in stripped:
                in_doc = False
            continue
        if stripped.startswith('#'):
            continue
        if stripped.startswith('"""'):
            if not (len(stripped) > 6 and stripped.endswith('"""')):
                in_doc = True
            continue
        kept.append(line.split(' #', 1)[0])
    return "\n".join(kept)


def _strip_js_comments(text: str) -> str:
    """去掉 ``//`` 行注释与 ``/* */`` 块注释（前端静态判据按**代码**判，注释里的字样不算数）。"""
    out: List[str] = []
    index = 0
    length = len(text)
    while index < length:
        pair = text[index:index + 2]
        if pair == "//":
            end = text.find("\n", index)
            index = length if end == -1 else end
            continue
        if pair == "/*":
            end = text.find("*/", index + 2)
            index = length if end == -1 else end + 2
            continue
        out.append(text[index])
        index += 1
    return "".join(out)


def _assert_present(case: unittest.TestCase, needle: str, text: str, label: str) -> None:
    """detail / 文案里必须出现的那句话（不比对整句 —— 措辞日后可能整体微调）。"""
    case.assertIn(needle, text, f"{label}：少了「{needle}」")


class _CopyHarness:
    """副本骨架（M4/M5/M6 同口径）：写前守卫 + 每用例 fresh copy + 零网络桩。

    放在 mixin 里是因为 activate 与 delete 两个测试类都要同一套还原/守卫逻辑，
    而 ``unittest`` 的 ``setUpClass`` 在 mixin 上同样生效。
    """

    @classmethod
    def setUpClass(cls):
        app = FastAPI()
        app.include_router(plugin_api.router, prefix=MOUNT_PREFIX)
        cls.client = TestClient(app)
        cls.pristine_config = _read_copy_bytes("config.yaml")
        cls.pristine_env = _read_copy_bytes(".env") if (_home() / ".env").exists() else None

    def setUp(self):
        self._guard_before_write()
        self.assertIsNotNone(self.pristine_env,
                             "副本里没有 .env —— §5.1 的 harness 命令要求一并复制 .env")
        self.assertEqual(str((_home() / ".env").resolve()).lower(),
                         str(Path(get_env_path()).resolve()).lower(),
                         "get_env_path() 不在副本上，写测试会碰真 .env")
        self._restore_copy()
        self.raw = _raw()
        self.providers = _raw_providers(self.raw)
        self.assertTrue(self.providers,
                        "副本里没有 providers: 条目 —— 危险动作的正向判据无从验证，"
                        "先按 progress §五 重建 harness 副本")

    def tearDown(self):
        self._restore_copy()

    # ---------------------------------------------------------------- 基础设施

    def _guard_before_write(self) -> None:
        """红线（prompt §5.2）：任何写入之前先确认落在 ``HERMES_HOME`` 临时副本上。"""
        home = _home()
        config_path = Path(get_config_path()).resolve()
        self.assertTrue(home.is_relative_to(Path(tempfile.gettempdir()).resolve()),
                        f"写入前守卫失败：HERMES_HOME={home} 不在系统临时目录下")
        self.assertTrue(str(config_path).lower().startswith(str(home).lower()),
                        f"写入前守卫失败：get_config_path()={config_path} 不在副本 {home} 之下")
        self.assertEqual(str(home / "config.yaml").lower(), str(config_path).lower())

    def _restore_copy(self) -> None:
        """**每个破坏性用例都是 fresh copy**：``setUp`` 与 ``tearDown`` 各还原一次。"""
        if _read_copy_bytes("config.yaml") != self.pristine_config:
            _write_copy_bytes(self.pristine_config, "config.yaml")
        self.assertEqual(self.pristine_config, _read_copy_bytes("config.yaml"),
                         "副本 config.yaml 没能还原成字节级原样")
        if self.pristine_env is not None:
            if _read_copy_bytes(".env") != self.pristine_env:
                _write_copy_bytes(self.pristine_env, ".env")
            invalidate_env_cache()
            self.assertEqual(self.pristine_env, _read_copy_bytes(".env"), "副本 .env 被写坏了")

    def _assert_bytes_unchanged(self) -> None:
        self.assertEqual(self.pristine_config, _read_copy_bytes("config.yaml"),
                         "被拒的请求写了 config")
        self.assertEqual(self.pristine_env, _read_copy_bytes(".env"),
                         "被拒的请求写了 .env（密钥是最要命的那一份）")

    # -------------------------------------------------------------------- 取材

    def _activatable_sample(self) -> str:
        """一个「``providers:`` 里 + 有 ``model`` + 有 ``base_url`` + **不是当前供应商**」的条目 id。

        ⚠️ 排除当前条目不是锦上添花，是这条判据的前提：启用**本来就是当前**的那一条是
        官方语义里的真 no-op（顶层 ``model:`` 逐值早已一致），于是
        ``test_activate_is_the_only_writer...`` 的「启用什么都没写？」与
        ``test_unknown_profile...`` 的「provider 没落到样本上」都会凭空落空
        —— 2026-09-23 真机把 ``model.provider`` 启用成了副本里的条目，本文件立刻就是这个样子。

        「是不是当前」的比法与**被包装的那条 route 同源**（``_danger_gate`` / 官方
        ``_detach_main_model_from_provider``：``str(顶层 model.provider).strip().lower()``
        对 ``_custom_endpoint_id(条目)``），测试不自建第二套判据。
        """
        def usable(entry: Any) -> bool:
            return (isinstance(entry, dict)
                    and bool(str(entry.get("model") or "").strip())
                    and bool(_entry_base_url(entry)))

        current_key = str(_raw_model().get("provider") or "").strip().lower()
        candidates = [str(pid) for pid, entry in
                      sorted(self.providers.items(), key=lambda item: str(item[0])) if usable(entry)]
        eligible = [pid for pid in candidates
                    if plugin_api._custom_endpoint_id(pid) != current_key]
        self.assertTrue(eligible,
                        f"副本里没有「providers: + 有 model + 有 base_url + 不是当前供应商」的条目"
                        f"（可用的 {len(candidates)} 条全是当前那条）—— 启用会退化成 no-op，"
                        "本文件的正向判据失去意义，先按 progress §五 重建 harness 副本")
        preferred = [pid for pid in eligible if pid in PREFERRED_PROVIDER_IDS]
        return preferred[0] if preferred else eligible[0]

    def _legacy_sample(self) -> Tuple[str, str]:
        """一个 cc-switch 条目（返回 ``(cc:名字, 裸名字)``）—— 决议 12 的拒绝面对象。"""
        named = [str(entry.get("name") or "").strip() for entry in _raw_legacy(self.raw)]
        usable = [name for name in named if name]
        self.assertTrue(usable, "副本里没有带 name 的 custom_providers: 条目，拒绝面无从验证")
        return f"{plugin_api.CC_ID_PREFIX}{usable[0]}", usable[0]

    def _rows(self) -> List[Dict[str, Any]]:
        response = self.client.get(LIST_PATH)
        self.assertEqual(200, response.status_code, response.text)
        return response.json().get("endpoints") or []

    def _create_fake_entry(self, endpoint_id: str, name: str) -> str:
        """在副本上造一条**合成** ``providers:`` 条目（走 M4 的保存路由），返回它的 .env 变量名。

        密钥用明显的假串，所以「删完 .env 变量必须消失」这类判据不必拿用户的真密钥来做
        （真密钥在本文件里只做哈希比对）。
        """
        fake_key = f"{FAKE_KEY_PREFIX}{endpoint_id}-0123456789"
        response = self.client.post(SAVE_PATH, json=_save_payload(endpoint_id, name, FAKE_MODEL,
                                                                  api_key=fake_key))
        self.assertEqual(200, response.status_code, response.text)
        env_var = custom_endpoint_key_env(endpoint_id)
        text = _read_copy_bytes(".env").decode("utf-8")
        self.assertEqual(_sha(fake_key), _sha(_env_value(text, env_var)),
                         f"前置状态没成立：假密钥没写进副本 .env 的 {env_var}")
        return env_var

    def _no_network(self):
        """把官方探测换成「一调用就炸」的桩：启用/删除都是纯 config 操作，零网络。"""
        return mock.patch.object(
            plugin_api, "validate_custom_endpoint",
            side_effect=AssertionError("危险动作链路不许发探测请求（纯 config 操作）"))


# ============================================================== 启用（M7.1 / M7.3 / F8）

class ActivateRouteTest(_CopyHarness, unittest.TestCase):
    """``POST /endpoints/{id}/activate``：官方 ``activate_custom_endpoint`` 的薄包装。"""

    def _activate(self, endpoint_id: str, profile: Optional[str] = None):
        self._guard_before_write()
        url = ACTIVATE_PATH_TEMPLATE.format(endpoint_id=endpoint_id)
        with self._no_network():
            if profile is None:
                return self.client.post(url)
            return self.client.post(url, params={"profile": profile})

    # ------------------------------------------------------------- 正向（F8 的磁盘半边）

    def test_activate_writes_top_level_model_consistent_with_the_entry(self):
        """M7.1 / UC-04 / F8：顶层 ``model.provider`` / ``default`` / ``base_url`` / ``key_env``
        与该条目自己的字段**逐值一致**（官方 ``:570-592`` 的镜像语义）。"""
        endpoint_id = self._activatable_sample()
        entry = _raw_entry(_raw(), endpoint_id)
        entry_model = str(entry.get("model") or "").strip()
        entry_key_env = str(entry.get("key_env") or "").strip()
        provider_key = plugin_api._custom_endpoint_id(endpoint_id)

        response = self._activate(endpoint_id)
        self.assertEqual(200, response.status_code, response.text)
        body = response.json()
        self.assertTrue(body.get("ok"), "官方契约的 ok 必须是 True")
        self.assertEqual(provider_key, body.get("provider"),
                         "响应的 provider 不是官方那一份（规整后的 slug）")
        self.assertEqual(entry_model, body.get("model"), "响应的 model 与条目的 model 不一致")

        model_cfg = _raw_model()
        self.assertEqual(provider_key, str(model_cfg.get("provider") or "").strip().lower(),
                         "顶层 model.provider 没落到被启用的条目上")
        self.assertEqual(entry_model, str(model_cfg.get("default") or "").strip(),
                         "顶层 model.default 不是条目的默认模型（F8 的磁盘半边）")
        self.assertEqual(_entry_base_url(entry), str(model_cfg.get("base_url") or "").strip(),
                         "顶层 model.base_url 与条目的 base_url 不一致")
        if entry_key_env:
            self.assertEqual(entry_key_env, str(model_cfg.get("key_env") or "").strip(),
                             "条目有 key_env 时官方会把它镜像到顶层 model.key_env（:578-580）")
            self.assertNotIn("api_key", model_cfg,
                             "走 key_env 镜像时官方明确 pop 掉 model.api_key（:580）")

    def test_activate_is_the_only_writer_and_moves_the_current_flag(self):
        """启用的写入范围（F8 的无损半边）：**只**改顶层 ``model:`` —— ``providers:`` 的键集合
        与所有条目**值级零改动**；同时 ``is_current`` 恰好落在被启用那一行。

        渲染级的「●使用中 转移」属 M7.8 人工项，这里钉的是它在只读契约上的等价事实。
        """
        endpoint_id = self._activatable_sample()
        before = copy.deepcopy(_raw())
        keys_before = sorted(str(key) for key in _raw_providers(before))

        response = self._activate(endpoint_id)
        self.assertEqual(200, response.status_code, response.text)
        after = copy.deepcopy(_raw())

        self.assertEqual(keys_before, sorted(str(key) for key in _raw_providers(after)),
                         "启用改动了 providers: 的键集合（官方 activate 只写顶层 model:）")
        changes = _changed_paths(before, after)
        self.assertTrue(changes, "启用什么都没写？判据落空了")
        for path in changes:
            self.assertTrue(path.startswith("model."),
                            f"启用动了顶层 model: 之外的路径：{path}")
        rows = response.json().get("endpoints") or []
        self.assertEqual(len(self._rows()), len(rows), "响应里的刷新行数与运行时列表不一致")
        current = [str(row.get("id") or "") for row in rows if row.get("is_current") is True]
        self.assertEqual([endpoint_id], current, "●使用中 没落到（或不止落在）被启用的那条上")

    def test_activate_response_shape_carries_refreshed_rows_and_no_key_material(self):
        """契约（M7.1 + §6.4）：``{ok, provider, model}`` + ``_write_response`` 的
        ``endpoints`` / ``current``；行里**没有任何密钥材料**。"""
        endpoint_id = self._activatable_sample()
        response = self._activate(endpoint_id)
        self.assertEqual(200, response.status_code, response.text)
        body = response.json()
        for key in ("ok", "id", "provider", "model", "endpoints", "current"):
            self.assertIn(key, body, f"启用响应缺字段 {key}")
        self.assertEqual(str(endpoint_id), str(body.get("id")), "响应的 id 不是被启用的条目")
        for row in (body.get("endpoints") or []):
            for key in FORBIDDEN_RESPONSE_KEYS:
                self.assertNotIn(key, row, f"启用响应的行里出现了密钥材料字段 {key}")
        self.assertNotIn("api_key_preview", response.text, "启用响应里出现了密钥预览字段")
        self.assertEqual(str(body.get("model") or ""),
                         str((body.get("current") or {}).get("model") or ""),
                         "current.model 与启用的模型不是同一个")

    def test_activate_profile_is_passed_through_to_official_and_scope(self):
        """R8 的形参半边：``profile`` 原样透传给官方函数与本插件自己的作用域。

        官方 ``activate_custom_endpoint`` 换成桩 ⇒ **一个字节都不写**（末尾逐字节证明）。
        ``list_custom_endpoints`` 也要桩化：``_write_response`` 会真的再跑一遍 M1 的列表，
        而 ``acme-team`` 这个 profile 在本机不存在 —— 官方对它**如实 404**（M1 已钉过），
        那会把「透传没透传」这件事混进「profile 存不存在」里。基线从**真副本**取，
        桩只负责把它原样交回来。
        """
        endpoint_id = self._activatable_sample()
        baseline = plugin_api.list_custom_endpoints(None)
        seen: Dict[str, List[Any]] = {"activate": [], "scope": []}

        def fake_activate(raw_id: str, profile: Optional[str] = None):
            seen["activate"].append((raw_id, profile))
            return {"ok": True, "provider": raw_id, "model": "stub/model"}

        with mock.patch.object(plugin_api, "activate_custom_endpoint", side_effect=fake_activate), \
                mock.patch.object(plugin_api, "list_custom_endpoints", return_value=baseline), \
                mock.patch.object(plugin_api, "_config_profile_scope",
                                  side_effect=lambda profile=None: seen["scope"].append(profile)
                                  or contextlib.nullcontext()):
            self.assertEqual(200, self._activate(endpoint_id, profile="acme-team").status_code)
            self.assertEqual(200, self._activate(endpoint_id).status_code)

        self.assertEqual([(endpoint_id, "acme-team"), (endpoint_id, None)], seen["activate"],
                         "profile 没被原样透传给官方 activate_custom_endpoint")
        # 每次请求开两次作用域（闸门读 config + M1 的 endpoints() 刷新），两次的 profile 必须一致
        self.assertEqual(["acme-team", "acme-team", None, None], seen["scope"],
                         "_config_profile_scope 收到的 profile 与请求里的不是一一对应")
        self._assert_bytes_unchanged()

    def test_route_contract_paths_signatures_and_official_shape(self):
        """契约（设计 §6.3）：两条路由的路径 / 方法 / 形参形状，并与官方两条**同形**。

        ⚠️ DELETE 没有请求体 ⇒ ``profile`` 与官方 :598 一样是 **query 参数**。
        """
        routes = {(tuple(sorted(route.methods)), route.path) for route in plugin_api.router.routes}
        self.assertIn((("POST",), ACTIVATE_ROUTE_PATH), routes, f"路由表没挂上 activate：{sorted(routes)}")
        self.assertIn((("DELETE",), DELETE_ROUTE_PATH), routes,
                      f"路由表没挂上 DELETE：{sorted(routes)}")

        for function in (plugin_api.activate_endpoint, plugin_api.delete_endpoint,
                         plugin_api.activate_custom_endpoint, plugin_api.delete_custom_endpoint):
            with self.subTest(function=function.__name__):
                parameters = inspect.signature(function).parameters
                self.assertEqual(["endpoint_id", "profile"], list(parameters),
                                 "官方与本插件的形参形状不再一致（薄包装漂移）")
                self.assertIsNone(parameters["profile"].default)
                self.assertEqual(Optional[str], typing.get_type_hints(function)["profile"])

    # --------------------------------------------------- 决议 12 / 三条拒绝面：零写入

    def test_activate_cc_switch_id_is_refused_with_chinese_detail_and_zero_bytes(self):
        """``cc:`` 前缀 → **400 + 中文 detail + 副本字节级未动**（决议 12）。

        官方对 legacy 条目只会给一句英文 ``custom endpoint not found``（:568），
        本插件在**进官方之前**就换成说得清的中文 + 指出正路（任务书 M7 的 detail 口径）。
        """
        identity, name = self._legacy_sample()
        response = self._activate(identity)
        self.assertEqual(400, response.status_code, response.text)
        detail = str(response.json().get("detail") or "")
        # 改动记录：2026-09-25 R3（M10.14，设计 §2.5 + §2.4 表 B · B5）：detail 已改成使用者
        # 语言，字面针脚同步换成新措辞的关键句（「由 cc-switch 管理」「收编进本插件」）；
        # 下面每条的**意图**（归属 / 官方行为 / 拒绝依据 / 拒绝必须给正路 / 零写入承诺）一条没少。
        _assert_present(self, "cc-switch", detail, "拒绝 cc: 的 detail 没点明条目归属")
        _assert_present(self, "由 cc-switch 管理", detail, "detail 没按新措辞点明这供应商归谁")
        _assert_present(self, "必然失败", detail, "detail 没说明官方对 legacy 的行为")
        _assert_present(self, "互相覆盖", detail, "detail 没给出「为什么不自建第二套」的依据")
        _assert_present(self, "收编进本插件", detail, "detail 没指出正路")
        _assert_present(self, "列表未改动", detail, "detail 没保证零写入")
        cjk = len(re.findall(r"[一-鿿]", detail))
        latin = len(re.findall(r"[A-Za-z]", detail))
        self.assertGreater(cjk, latin // 2, f"detail 不像中文说明（中文 {cjk} 字 / 拉丁 {latin} 字母）")
        self._assert_bytes_unchanged()
        self.assertIn(name, [str(entry.get("name") or "").strip() for entry in _raw_legacy()],
                      "被拒的启用请求把 legacy 条目动了")

    def test_activate_blank_and_unknown_id_write_nothing(self):
        """另两条拒绝面：空 id → 400、``providers:`` 里查无此条目 → 404（**不静默新建**）。"""
        keys_before = sorted(str(key) for key in _raw_providers())

        blank = self.client.post(f"{LIST_PATH}/{BLANK_SEGMENT}/activate")
        self.assertEqual(400, blank.status_code, blank.text)
        _assert_present(self, "缺少供应商标识", str(blank.json().get("detail") or ""),
                        "空 id 的 detail 没说清缺什么")

        missing = self._activate(MISSING_ID)
        self.assertEqual(404, missing.status_code, missing.text)
        detail = str(missing.json().get("detail") or "")
        # 改动记录：2026-09-25 R3（M10.14，表 B · B2 分支②）：该分支的 detail 不再出现存储段名，
        # 第一针换成新措辞的关键句；意图不变（说清「这是真找不到」+ 保证不静默新建）。
        _assert_present(self, "找不到该供应商", detail, "查无此条目的 detail 没说是「真找不到」")
        _assert_present(self, "不会新建", detail, "查无此条目的 detail 没保证不静默新建")

        self._assert_bytes_unchanged()
        self.assertEqual(keys_before, sorted(str(key) for key in _raw_providers()),
                         "拒绝面改动了 providers: 的键集合（静默新建）")

    def test_unknown_profile_is_refused_and_writes_nothing(self):
        """R8 的作用域半边：未知 profile 必须**如实失败**，不能悄悄改默认 profile 的顶层 ``model:``。

        「没动」这件事只有在一个前提下才不是废话：样本**当时不是**当前供应商
        （``_activatable_sample()`` 已保证，下面把前置状态再钉一次）。所以本用例按真正的
        不变量收口：① 未知 profile ⇒ 逐字节没动、``model.provider`` 还在原处；
        ② 随后**正常启用同一条目** ⇒ 顶层 ``model.provider`` 落到它身上
        —— ①的「没动」因此不可能是「本来就在那儿」。
        """
        endpoint_id = self._activatable_sample()
        provider_key = plugin_api._custom_endpoint_id(endpoint_id)
        mirror_before = copy.deepcopy(_raw_model())
        current_before = str(_raw_model().get("provider") or "").strip().lower()
        self.assertNotEqual(provider_key, current_before,
                            "前置状态没成立：样本就是当前供应商，启用本来就是 no-op")

        response = self._activate(endpoint_id, profile="definitely-not-a-profile")
        self.assertGreaterEqual(response.status_code, 400, response.text)
        self.assertLess(response.status_code, 500, f"未知 profile 不该打成 5xx：{response.text}")
        self._assert_bytes_unchanged()
        self.assertEqual(mirror_before, _raw_model(), "未知 profile 改了顶层 model:（R8 破防）")
        self.assertEqual(current_before, str(_raw_model().get("provider") or "").strip().lower(),
                         "未知 profile 动了顶层 model.provider（R8 破防）")

        confirmed = self._activate(endpoint_id)
        self.assertEqual(200, confirmed.status_code, confirmed.text)
        self.assertEqual(provider_key, str(_raw_model().get("provider") or "").strip().lower(),
                         "启用样本后 model.provider 没落到它身上：上面那条「没动」判据落空")


# ============================================================== 删除（M7.2 / M7.4 / T14）

class DeleteRouteTest(_CopyHarness, unittest.TestCase):
    """``DELETE /endpoints/{id}``：官方 ``delete_custom_endpoint`` 的三件事。"""

    def _delete(self, endpoint_id: str, profile: Optional[str] = None):
        self._guard_before_write()
        url = DELETE_PATH_TEMPLATE.format(endpoint_id=endpoint_id)
        with self._no_network():
            if profile is None:
                return self.client.delete(url)
            return self.client.delete(url, params={"profile": profile})

    def _activate(self, endpoint_id: str):
        return self.client.post(ACTIVATE_PATH_TEMPLATE.format(endpoint_id=endpoint_id))

    # ------------------------------------------------------------- T14 的正向三件事

    def test_delete_removes_entry_and_clears_the_env_key_only_for_itself(self):
        """M7.2 / T14 前两件：``providers:`` 条目消失 + ``.env`` 里那个变量被清；
        **其它**变量一个都不动（``remove_env_value`` 只该清它自己那一个）。"""
        env_var = self._create_fake_entry(M7_SAMPLE_ID_A, "M7 删除用例 A")
        env_text_before = _read_copy_bytes(".env").decode("utf-8")
        names_before = sorted(set(_env_names(env_text_before)))
        keys_before = sorted(str(key) for key in _raw_providers())

        response = self._delete(M7_SAMPLE_ID_A)
        self.assertEqual(200, response.status_code, response.text)
        self.assertTrue(response.json().get("ok"))
        self.assertNotIn(M7_SAMPLE_ID_A, _raw_providers(), "条目还在 providers: 里")
        self.assertEqual([key for key in keys_before if key != M7_SAMPLE_ID_A],
                         sorted(str(key) for key in _raw_providers()),
                         "providers: 的键集合改动不止被删那一条")

        env_text_after = _read_copy_bytes(".env").decode("utf-8")
        self.assertIsNone(_env_value(env_text_after, env_var), ".env 里的对应变量还在（没清）")
        self.assertEqual([name for name in names_before if name != env_var],
                         sorted(set(_env_names(env_text_after))),
                         "删除改动了别的 .env 变量")
        # 真配置仍然只读：跑完这一套之后副本还能逐字节还原（tearDown 会再证明一次）
        invalidate_env_cache()

    def test_delete_detaches_mirror_only_when_it_pointed_at_the_deleted_entry(self):
        """T14 第三件 + M7.4 文案的判据：镜像**指向被删条目** ⇒ 四个键全摘；
        指向别处 ⇒ 顶层 ``model:`` 一个键都不动（``:428-435`` 的既有语义）。"""
        # ① 指向被删条目：启用 A ⇒ 镜像指向 A ⇒ 删 A ⇒ 镜像摘净
        self._create_fake_entry(M7_SAMPLE_ID_A, "M7 当前条目 A")
        self.assertEqual(200, self._activate(M7_SAMPLE_ID_A).status_code)
        provider_key = plugin_api._custom_endpoint_id(M7_SAMPLE_ID_A)
        self.assertEqual(provider_key, str(_raw_model().get("provider") or "").strip().lower(),
                         "前置状态没成立：顶层 model.provider 没指向 A")

        first = self._delete(M7_SAMPLE_ID_A)
        self.assertEqual(200, first.status_code, first.text)
        receipt = first.json().get("deleted") or {}
        self.assertTrue(receipt.get("main_model_detached"),
                        "回执声称镜像没摘 —— 但它当时正是当前供应商")
        model_cfg = _raw_model()
        for field in MIRROR_FIELDS_EXPECTED:
            self.assertNotIn(field, model_cfg,
                             f"镜像指向被删条目时官方要摘掉 model.{field}（:433），实际还留着")

        # ② 指向别处：启用 B 之后删 A ⇒ B 的镜像一个键都不动
        self._create_fake_entry(M7_SAMPLE_ID_A, "M7 无关条目 A")
        self._create_fake_entry(M7_SAMPLE_ID_B, "M7 当前条目 B")
        self.assertEqual(200, self._activate(M7_SAMPLE_ID_B).status_code)
        mirror_before = copy.deepcopy(_raw_model())
        self.assertEqual(plugin_api._custom_endpoint_id(M7_SAMPLE_ID_B),
                         str(mirror_before.get("provider") or "").strip().lower())

        second = self._delete(M7_SAMPLE_ID_A)
        self.assertEqual(200, second.status_code, second.text)
        second_receipt = second.json().get("deleted") or {}
        self.assertFalse(second_receipt.get("main_model_detached"),
                         "镜像指向 B 时回执却声称摘掉了 A 的镜像")
        self.assertEqual(mirror_before, _raw_model(),
                         "删 A 却动了指向 B 的顶层 model:（官方只在指向被删条目时才摘）")
        self.assertEqual(plugin_api._custom_endpoint_id(M7_SAMPLE_ID_B),
                         str(_raw_model().get("provider") or "").strip().lower())
        self.assertIn(M7_SAMPLE_ID_B, _raw_providers(), "删 A 把 B 也带走了")

    def test_delete_receipt_names_the_env_var_and_carries_no_key_material(self):
        """契约（M7.2 / M7.4 的前端半边）：``deleted{...}`` 段在位、``env_var`` 只是**变量名**、
        整份响应里都不出现密钥材料（连本次用的假密钥也不回显）。"""
        fake_key = f"{FAKE_KEY_PREFIX}{M7_SAMPLE_ID_A}-0123456789"
        created = self.client.post(SAVE_PATH, json=_save_payload(M7_SAMPLE_ID_A, "M7 出口用例",
                                                                FAKE_MODEL, api_key=fake_key))
        self.assertEqual(200, created.status_code, created.text)

        deleted = self._delete(M7_SAMPLE_ID_A)
        self.assertEqual(200, deleted.status_code, deleted.text)
        body = deleted.json()
        for key in ("ok", "id", "endpoints", "current", "deleted"):
            self.assertIn(key, body, f"删除响应缺字段 {key}")
        receipt = body["deleted"]
        for key in ("id", "name", "model", "provider", "env_var", "main_model_detached"):
            self.assertIn(key, receipt, f"deleted 段缺字段 {key}")
        self.assertEqual(custom_endpoint_key_env(str(receipt["provider"])), receipt["env_var"],
                         "deleted.env_var 不是官方那个变量名（custom_endpoint_key_env）")
        self.assertTrue(str(receipt["env_var"]).startswith("HERMES_CUSTOM_"),
                        "env_var 看着不像变量名（密钥铁律：这一栏只许是名字）")
        self.assertNotIn(fake_key, deleted.text, "删除响应里出现了明文密钥")
        self.assertNotIn("api_key_preview", deleted.text, "删除响应里出现了密钥预览字段")
        for row in (body.get("endpoints") or []):
            for key in FORBIDDEN_RESPONSE_KEYS:
                self.assertNotIn(key, row, f"删除响应的行里出现了密钥材料字段 {key}")
        self.assertNotIn(M7_SAMPLE_ID_A, [str(row.get("id") or "") for row in body["endpoints"]],
                         "刷新后的列表里那条还在（_write_response 没读到删完的状态）")

    def test_delete_is_surgical_for_everything_else(self):
        """无损性（T22 口径的 M7 版）：除了被删条目本身，整份 config **值级零改动** ——
        尤其 ``auxiliary.*``（本机那一份真的带着密钥）与 ``custom_providers:`` 不许被牵连。"""
        self._create_fake_entry(M7_SAMPLE_ID_A, "M7 无损用例 A")
        self._create_fake_entry(M7_SAMPLE_ID_B, "M7 无损用例 B")
        before = copy.deepcopy(_raw())
        self.assertNotEqual(plugin_api._custom_endpoint_id(M7_SAMPLE_ID_A),
                            str(_raw_model().get("provider") or "").strip().lower(),
                            "前置状态：镜像不该指向被删条目（本用例要的是「只动条目」那一支）")

        response = self._delete(M7_SAMPLE_ID_A)
        self.assertEqual(200, response.status_code, response.text)
        after = copy.deepcopy(_raw())

        changes = [path for path in _changed_paths(before, after)
                   if not path.startswith(f"providers.{M7_SAMPLE_ID_A}")]
        self.assertEqual([], changes, f"删除动了预期之外的路径（值级）：{changes}")
        self.assertEqual(_sha(repr(before.get("auxiliary"))), _sha(repr(after.get("auxiliary"))),
                         "auxiliary.* 被牵连了（本机那份槽里真带着密钥）")
        self.assertEqual(len(_raw_legacy(before)), len(_raw_legacy(after)),
                         "custom_providers: 的条目数变了")
        self.assertIn(M7_SAMPLE_ID_B, _raw_providers(after), "同批造的 B 条目被一起删了")

    def test_delete_cc_switch_id_is_refused_with_chinese_detail_and_zero_bytes(self):
        """决议 12 的删除半边：``cc:`` → 400 + 中文 detail + 副本字节级未动。"""
        identity, name = self._legacy_sample()
        response = self._delete(identity)
        self.assertEqual(400, response.status_code, response.text)
        detail = str(response.json().get("detail") or "")
        # 改动记录：2026-09-25 R3（M10.14，设计 §2.5 + 表 B · B5）：与启用半边同款针脚同步——
        # 换成新措辞的关键句，「拒绝必须给正路」等意图逐条保留。
        _assert_present(self, "cc-switch", detail, "删除被拒的 detail 没点明条目归属")
        _assert_present(self, "由 cc-switch 管理", detail, "删除被拒的 detail 没按新措辞点明归属")
        _assert_present(self, "互相覆盖", detail, "删除被拒的 detail 没给出依据")
        _assert_present(self, "收编进本插件", detail, "删除被拒的 detail 没指出正路")
        _assert_present(self, "列表未改动", detail, "删除被拒的 detail 没保证零写入")
        self._assert_bytes_unchanged()
        self.assertIn(name, [str(entry.get("name") or "").strip() for entry in _raw_legacy()],
                      "被拒的删除请求把 legacy 条目摘掉了")

    def test_delete_blank_and_unknown_id_write_nothing(self):
        """另两条拒绝面（与启用共用同一道闸门）：空 id → 400、查无此条目 → 404，且**不**新建。"""
        keys_before = sorted(str(key) for key in _raw_providers())

        blank = self.client.delete(f"{LIST_PATH}/{BLANK_SEGMENT}")
        self.assertEqual(400, blank.status_code, blank.text)
        missing = self._delete(MISSING_ID)
        self.assertEqual(404, missing.status_code, missing.text)
        _assert_present(self, "不会新建", str(missing.json().get("detail") or ""),
                        "查无此条目的 detail 没保证不静默新建")
        self._assert_bytes_unchanged()
        self.assertEqual(keys_before, sorted(str(key) for key in _raw_providers()),
                         "拒绝面改动了 providers: 的键集合")

    def test_delete_takes_no_body_and_profile_arrives_as_query_param(self):
        """M7.2 的签名口径：DELETE 无请求体 ⇒ ``profile`` 走 query（与官方 :598 同形）。

        官方函数换成桩 ⇒ 零写入；``params`` 而不是 ``json`` 传 profile 就是这条契约的证据。
        """
        seen: Dict[str, List[Any]] = {"delete": [], "scope": []}
        # 条目必须真实存在：写前闸门（查无此条目 → 404）跑的是**真**逻辑，
        # 桩只包住官方那次写。响应的 endpoints 同「启用」那个用例的理由一并桩化。
        self._create_fake_entry(M7_SAMPLE_ID_A, "M7 签名用例")
        baseline = plugin_api.list_custom_endpoints(None)

        def fake_delete(raw_id: str, profile: Optional[str] = None):
            seen["delete"].append((raw_id, profile))
            return {"ok": True}

        with mock.patch.object(plugin_api, "delete_custom_endpoint", side_effect=fake_delete), \
                mock.patch.object(plugin_api, "list_custom_endpoints", return_value=baseline), \
                mock.patch.object(plugin_api, "_config_profile_scope",
                                  side_effect=lambda profile=None: seen["scope"].append(profile)
                                  or contextlib.nullcontext()):
            self._guard_before_write()
            response = self.client.delete(
                DELETE_PATH_TEMPLATE.format(endpoint_id=M7_SAMPLE_ID_A),
                params={"profile": "acme-team"})
            self.assertEqual(200, response.status_code, response.text)

        self.assertEqual([(M7_SAMPLE_ID_A, "acme-team")], seen["delete"],
                         "profile 没被透传给官方 delete_custom_endpoint")
        self.assertEqual(["acme-team", "acme-team"], seen["scope"],
                         "写前闸门与列表刷新两次作用域的 profile 不是一一对应")
        # 官方写被桩住 ⇒ 前置状态之外不该再有新的落盘（条目仍在、.env 里那份假密钥仍在）
        self.assertIn(M7_SAMPLE_ID_A, _raw_providers(), "桩化的删除请求把条目删了")

    def test_danger_gate_is_shared_and_official_functions_are_not_reimplemented(self):
        """纪律（progress §四 / §六）：两条路由共用**同一道**闸门，且复用 M5 的定位 helper、
        M1 的 ``CC_ID_PREFIX``，不重写官方逻辑。"""
        source = Path(plugin_api.__file__).read_text(encoding="utf-8")
        m7_code = _strip_python_comments(source[source.index("区块 M7"):])
        self.assertEqual(2, m7_code.count("= _danger_gate("),
                         "两条危险路由没共用同一道闸门（拒绝面会漂）")
        self.assertEqual(1, m7_code.count("def _danger_gate("),
                         "_danger_gate 被定义了不止一份（一份闸门才是重点）")
        self.assertIn("_locate_providers_entry(", m7_code,
                      "M7 没复用 M5 的定位 helper（第二套判据会漂移）")
        self.assertIn("CC_ID_PREFIX", m7_code, "M7 的 cc: 拒绝面没吃 M1 的常量")
        self.assertIn("activate_custom_endpoint(", m7_code, "启用没走官方函数（重写了业务）")
        self.assertIn("delete_custom_endpoint(", m7_code, "删除没走官方函数（重写了业务）")
        for frozen in ("def _locate_providers_entry(", "def _write_response(",
                       "def _cc_switch_rows("):
            self.assertEqual(1, source.count(frozen), f"共享 helper 被复制出第二份：{frozen}")
        with self.assertRaises(HTTPException) as caught:
            plugin_api._danger_gate(MISSING_ID, "启用", None)
        self.assertEqual(404, caught.exception.status_code)
        self._assert_bytes_unchanged()

    def test_m7_backend_constants_and_official_mirror_field_list_agree(self):
        """常量口径：本插件记录的「官方镜像四键」与官方实现**实际**摘的键一致（防上游改名）。

        读 ``config_env.py`` 的源码而不是读注释：那四键的字面量出现在
        ``_detach_main_model_from_provider`` 的 ``for field in (...)`` 一行里。
        """
        from hermes_cli.web_routers import config_env

        official = inspect.getsource(config_env._detach_main_model_from_provider)
        declared = re.search(r"for field in \(([^)]*)\)", official)
        self.assertIsNotNone(declared, "官方 _detach_main_model_from_provider 的形状变了，判据要重写")
        fields = tuple(item.strip().strip('"\'') for item in declared.group(1).split(","))
        self.assertEqual(MIRROR_FIELDS_EXPECTED, fields,
                         "官方摘的镜像键与本文件/后端记录的清单不再一致")
        self.assertEqual(fields, tuple(plugin_api.MAIN_MODEL_MIRROR_FIELDS))


# ========================================================= 前端静态判据（M7.3–M7.6）

class PluginJsM7LintTest(unittest.TestCase):
    """M7.3 / M7.4 / M7.5 / M7.6 在 ``plugin.js`` 里的**静态**半边。

    ⚠️ 这不是渲染证据（prompt §5.4）：磁盘插件没有测试基建，渲染级验收是 M7.8 的人工项。
    这里钉的是「功能面的形状 + 文案归属 + 三条硬边界」，因为它一旦漂了，
    人工验收就是在验另一套东西。
    """

    @classmethod
    def setUpClass(cls):
        cls.source = (DESKTOP_DIR / "plugin.js").read_text(encoding="utf-8")
        # 「不许再出现」类判据一律按**代码**判：M7 自己的注释里会**记录**被删掉的占位常量名
        # 与旧措辞（那是改动记录，不是活文案），拿原文判就会把记录本身判成违规。
        cls.code = _strip_js_comments(cls.source)
        marker = "区块 M7：危险动作"
        index = cls.source.index(marker)
        cls.tail = cls.source[index:]
        cls.tail_code = _strip_js_comments(cls.source[cls.source.index("*/", index) + 2:])
        cls.card_body = cls._slice(cls.source, "function ProviderCardActions",
                                   "function ProviderCard({")
        cls.card_code = _strip_js_comments(cls.card_body)

    @staticmethod
    def _slice(text: str, start: str, end: str) -> str:
        begin = text.index(start)
        return text[begin:text.index(end, begin)]

    # ------------------------------------------------------------------ 追加纪律

    def test_m7_block_is_appended_after_m6_and_mounted_once(self):
        order = [self.source.index(anchor) for anchor in (
            "区块 M2：供应商列表与状态徽章", "区块 M3：表单区 + 两栏模型区", "区块 M4：写链路",
            "区块 M5：清空白名单", "区块 M6：cc-switch 条目迁移", "区块 M7：危险动作",
            "function SupplierModelsPage")]
        self.assertEqual(sorted(order), order, "区块顺序被改了（只许追加、不许重排）")
        self.assertEqual(1, self.source.count("function DangerZone"), "DangerZone 定义不唯一")
        self.assertEqual(1, self.source.count("jsx(DangerZone,"), "DangerZone 的挂载点不是恰好一处")

    def test_danger_actions_are_gated_to_providers_rows_only(self):
        """动作闸门（决议 12 + progress §六「M6 ↔ M7」）：启用 / 删除 / 清空 = ``providers:``
        独有，迁移 = cc-switch 独有；卡片里各只有一处调用点。"""
        for call in ("onClick: () => requestActivateSupplier(row),",
                     "onClick: () => requestDeleteSupplier(row),"):
            self.assertEqual(1, self.card_body.count(call), f"卡片里的危险入口调用点不唯一：{call}")
        self.assertLess(self.card_body.index("isProvidersSource(row)"),
                        self.card_body.index("onClick: () => requestActivateSupplier(row),"),
                        "启用按钮不在 isProvidersSource 闸门之内 ⇒ cc-switch 卡片也会拿到它")
        self.assertLess(self.card_body.index("onClick: () => requestActivateSupplier(row),"),
                        self.card_body.index("onClick: () => requestDeleteSupplier(row),"),
                        "删除跑到了启用之前（他人动作项的顺序不许改）")
        self.assertEqual(2, self.card_body.count("isProvidersSource(row)"),
                         "providers: 闸门份数变了（清空与危险动作各一处，不许合并或散落）")
        self.assertEqual(1, self.card_body.count("isCcSwitchRow(row)"),
                         "cc-switch 闸门份数变了（迁移入口必须只有一处）")
        self.assertIn("query.rows.filter(isProvidersSource)", self.tail_code)
        self.assertIn("ctx.rest(activatePathFor(target.id), { method: 'POST' })", self.tail_code)
        self.assertIn("ctx.rest(deletePathFor(target.id), { method: 'DELETE' })", self.tail_code)
        # 路径都复用 M4 的 /endpoints 常量，且本区不新建 mutation（收尾 A 复用 M4 的写链路）
        self.assertEqual(2, self.tail_code.count("${SAVE_ENDPOINTS_PATH}/"),
                         "启用/删除的路径没复用 M4 的 SAVE_ENDPOINTS_PATH 常量")
        self.assertEqual(0, self.tail_code.count("useMutation({"),
                         "M7 区块自己新建了 mutation（钉住/保存必须复用 M4 那条）")

    def test_activate_refreshes_list_and_notifies_the_host(self):
        """M7.3 / UC-04：成功之后 ① 失效列表（``●使用中`` 转移）② 通知宿主主模型已变
        ③ haptic + toast；失败透传后端 detail 原文。"""
        self.assertIn("queryClient.invalidateQueries({ queryKey: ENDPOINTS_QUERY_KEY })",
                      self.tail_code, "启用/删除后没走 §6.4 那条统一失效路径")
        self.assertIn("queryClient.invalidateQueries({ queryKey: MODEL_OPTIONS_QUERY_KEY })",
                      self.tail_code,
                      "没通知宿主主模型已变（app 自己在主模型变更后也是失效这个键）")
        self.assertIn("const MODEL_OPTIONS_QUERY_KEY = ['model-options']", self.tail_code)
        self.assertIn("notifyHostMainModelChanged(queryClient)", self.tail_code)
        self.assertIn("haptic('success')", self.tail_code)
        self.assertIn("backendErrorDetail(error)", self.tail_code, "失败没透传后端 detail 原文")
        # 通知门的依据写在注释里（M8 / 用户要能核到源码行）
        for evidence in ("settings-tile-view.tsx", "sdk/index.ts:1706", "readonly"):
            _assert_present(self, evidence, self.tail, "UC-04「通知宿主」的实现依据没写进注释")

    def test_delete_confirm_copy_states_key_and_model_mirror(self):
        """M7.4 必说话案：``.env`` 里的密钥 + 顶层 ``model`` 镜像（UC-05），且镜像那一句按
        ``is_current`` 分两支；确认弹窗走 SDK 自己的 destructive 形状。"""
        _assert_present(self, "会同时清除 .env 里的密钥", self.tail, "删除的二次确认少了密钥那一句")
        _assert_present(self, "顶层 model", self.tail, "删除的二次确认少了顶层 model 镜像那一句")
        _assert_present(self, "provider / base_url / api_key / key_env", self.tail,
                        "镜像那一句没列出官方实际会摘的四个键（与后端 detail 同一套事实）")
        self.assertIn("target.isCurrent ? DELETE_CONFIRM_LINE_MIRROR_DETACH "
                      ": DELETE_CONFIRM_LINE_MIRROR_KEPT", self.tail_code,
                      "镜像文案没按 is_current 分叉（「只有指向被删条目才摘」这个语义丢了）")
        self.assertIn("DELETE_CONFIRM_TITLE_TEMPLATE = '删除 {name}？'", self.tail,
                      "确认标题不再与官方页同形状（zh.ts:511，M7.5 的可声称那一档）")
        self.assertIn("destructive: true", self.tail_code, "删除弹窗没走 SDK 的 destructive 形状")
        self.assertEqual(1, self.tail_code.count("ConfirmDialog,"), "M7 区块里的确认弹窗不止一处")
        # 卡片按钮的删除入口只开确认，不直接写（决议 3 的「不做自动猜测式删除」同口径）
        self.assertIn("askDelete: row => danger.askDelete(row)", self.tail_code)

    def test_danger_colour_is_only_the_red_token(self):
        """M7.4 / M7.6：危险动作的强调色只有 ``var(--ui-red)`` 一个来源。"""
        colours = re.findall(r"color:\s*'([^']+)'", self.tail_code)
        self.assertTrue(colours, "M7 区块一个颜色声明都没有？判据落空了")
        for colour in colours:
            self.assertTrue(colour.startswith("var(--ui-"), f"M7 区块出现非主题色：{colour}")
        non_text = {colour for colour in colours if "var(--ui-text" not in colour}
        self.assertEqual({"var(--ui-red)"}, non_text, f"危险色出现了第二个来源：{non_text}")

    def test_no_hardcoded_colour_jsx_or_manual_profile_anywhere(self):
        """M7.6 终检（**全站**，不只本区块）：硬编码颜色 / JSX / 手工拼 ``?profile=`` 零命中。

        与任务书那条命令逐字对应（``whiteSpace`` 之所以不命中，是 ``\\bwhite\\b`` 的
        词边界在 ``whiteSpace`` 里不成立）：
          ``grep -nE "#[0-9a-fA-F]{3,8}|rgb\\(|rgba\\(|\\bblack\\b|\\bwhite\\b" desktop/plugin.js``
        """
        pattern = re.compile(r"#[0-9a-fA-F]{3,8}|rgb\(|rgba\(|\bblack\b|\bwhite\b")
        hits = [line for line in self.source.splitlines() if pattern.search(line)]
        self.assertEqual([], hits, f"命中硬编码颜色：{hits[:3]}")
        code = _strip_js_comments(self.source)
        self.assertIsNone(re.search(r"<[A-Za-z][A-Za-z0-9]*[\s/>]", code), "出现了 JSX 标签（无编译）")
        self.assertNotIn("?profile=", code, "前端手工拼了 profile 参数（§2.6 C2 已结案）")
        specifiers = sorted(set(re.findall(r"from '([^']+)'", self.source)))
        self.assertEqual(["@hermes/plugin-sdk", "react", "react/jsx-runtime"], specifiers,
                         "import 的 specifier 超出三个（磁盘插件硬边界）")

    def test_dead_placeholders_are_wired_and_stale_hints_gone(self):
        """收尾 A + 三合一（布局优化 2026-09-24）：卡片「钉住」仍接 M4 的 `runPin`；
        保存写链路在合并卡底部唯一一处（原表单区那颗重复「保存」与 `requestFormSaveFromForm`
        转发链已退役），且**不**新建第二份写链路。

        「清零」那几条按代码判（``self.code`` / ``card_code``）：M7 的注释里**记录**了被删掉
        的常量名与旧话术（改动记录要给 M8 复核），拿原文判会把记录本身判成违规。
        """
        self.assertNotIn("PLACEHOLDER_ACTION_TIP", self.code, "过期占位提示还在文件里")
        self.assertNotIn("该动作由后续模块接通", self.code, "占位 Tip 的原文还在别处")
        self.assertNotIn("保存链路由后续模块接通", self.code, "FORM_WRITE_NOTE 的过期话术还在")
        self.assertNotIn("disabled: true", self.card_code, "卡片动作行里还有死占位按钮")
        form_body = self._slice(self.source, "function FormBody", "const FIELD_CONTEXT_HINT")
        self.assertNotIn("disabled: true", _strip_js_comments(form_body),
                         "表单主体里还有死占位按钮")
        # 三合一后表单区不再自带「保存」：那颗按钮与整条转发链应已从代码里移除
        self.assertNotIn("requestFormSaveFromForm", self.code,
                         "表单区重复「保存」的转发链没随三合一退役")
        self.assertIn("onClick: () => requestPinFromCard(row)", self.card_body,
                      "卡片「钉住」没接到 M4 的钉住")
        # 唯一写入口：合并卡底部保存行的 runSave 恰好一处（判据不复制，第三份链路不许长出来）
        self.assertEqual(1, self.code.count("onClick: () => actions.runSave(editor, query.rows)"),
                         "保存写链路不再是唯一一处")
        self.assertIn("cardPinRequestHandler = row => actions.runPin(row)", self.tail_code)
        for single in ("function buildSavePayload(", "function saveBlockReason(",
                       "function pinPathFor("):
            self.assertEqual(1, self.source.count(single), f"M4 的写链路被复制出第二份：{single}")

    def test_copy_unified_and_source_attribution_recorded(self):
        """收尾 B + C（M7.5）：措辞统一 + 文案**来源**分档写进文件
        （不许把 cc-switch / 设计口径的话说成官方 zh.ts 的）。"""
        # 改动记录：2026-09-25 R3（M10.15，设计 §2.5 第二行）：M10 重写的是区块层文案，本方法
        # 钉着的来源表 needle（`zh.ts:503` 等，写在 plugin.js 的注释里）**一条都没删**，下面
        # `CLEAR_ROW_VIEW_TEMPLATE` 的逐字断言也原样保持（表 A 的「不改」行）。断言代码零改动。
        # 措辞统一：模板常量里不再有「视图」二字（注释**提到**旧措辞是记录改动，允许）
        self.assertNotIn("白名单视图", self.code, "「白名单视图 N 个」的旧措辞还留在代码里")
        self.assertIn("CLEAR_ROW_VIEW_TEMPLATE = '白名单 {count} 个 · {note}'", self.source,
                      "清空区的措辞没统一成「白名单 N 个」")
        self.assertNotIn("动作集恒为", self.card_body,
                         "M2 动作行头注还写着「动作集恒为查看+钉住」（批 3 已过期）")
        for needle in ("zh.ts:503", "addModelPlaceholder", "HermesFormFields.tsx:425",
                       "不得声称出自官方 zh.ts"):
            _assert_present(self, needle, self.source, "M7.5 的文案来源表不完整")
        # `添加模型` 的自查结论（任务书标「未实测」那条）必须写在文件里，别只活在汇报里
        _assert_present(self, "CANDIDATE_ADD_LABEL", self.source, "来源表没覆盖 `添加模型` 这一条")
        for forbidden in ("不提供" + "任何模型", "解除钉住", "撤销迁移", "un-pin", "unmigrate"):
            self.assertNotIn(forbidden, self.source, f"禁语出现在文件里：{forbidden}")

    def test_probe_failure_copy_mapping_is_the_four_verbatim_strings(self):
        """收尾 C：M3.9 那四条逐字失败文案 + ``error_kind`` → 文案的映射仍然一一对应。

        官方 ``validate_custom_endpoint`` 只给英文 message，M3 因此加了 ``error_kind``；
        文案职责是**照 kind 映射**，绝不去嗅探英文原文（prompt §2.6 / 任务书 C 条）。
        """
        for verbatim in ("请先填写 API 端点和 API Key", "API Key 无效或无权限",
                         "无法连接到 ", "未找到可用模型"):
            self.assertIn(verbatim, self.source, f"M3.9 的逐字文案少了：{verbatim}")
        mapping = self._slice(self.source, "function copyForProbeFailure", "\nconst PROBE_BASE_URL")
        for kind, copy_needle in (("'auth'", "ERROR_INVALID_KEY"),
                                  ("'unreachable'", "ERROR_UNREACHABLE_PREFIX"),
                                  ("'no_endpoint'", "ERROR_NEED_ENDPOINT_AND_KEY"),
                                  ("'empty'", "ERROR_NO_MODELS")):
            self.assertIn(f"case {kind}:", mapping, f"error_kind 映射少了 {kind} 这一支")
            self.assertIn(copy_needle, mapping, f"{kind} 没映射到那条逐字文案")
        after = self.source[self.source.index("function copyForProbeFailure"):]
        self.assertNotIn("message.includes", after, "前端又开始嗅探官方英文原文")
        self.assertNotIn("PROBE_MESSAGE", after, "前端开始读后端的英文原文常量（判据必须是 error_kind）")

    def test_page_ground_uses_the_chat_window_token(self):
        """收尾 D：页面底色改成聊天窗自己那层玻璃（``--ui-chat-window-background``），
        不再是壳底 ``--ui-bg-chrome``；调查判据写在 PAGE_STYLE 的注释里给 M8 复核。"""
        page_style = self._slice(self.source, "const PAGE_STYLE = {", "\n}")
        code_part = page_style[page_style.rindex("*/") + 2:]
        self.assertIn("background: 'var(--ui-chat-window-background)'", code_part,
                      "页面底色不再是聊天窗那一层的 token")
        self.assertNotIn("--ui-bg-chrome", code_part, "PAGE_STYLE 的代码部分又涂回了壳底")
        for evidence in ("styles.css:428-433", "app/chat/index.tsx:687", "route-tile.tsx:67",
                         "surfaces.tsx"):
            _assert_present(self, evidence, page_style, "D 项的调查判据没写进注释")


if __name__ == "__main__":
    unittest.main()
