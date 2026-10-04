"""M4 验收：写链路的**保存**路径 ``POST /endpoints``（M4.1 / M4.10；铁律 1 与铁律 2 的落盘面）。

跑法（``tasks/progress.md`` §五 / ``prompt.md`` §5.1，逐字照抄；venv 里没有 pytest 也不许装）：

    cd "H:/application/hermesnew/home/plugins/supplier-models"
    V="H:/application/hermesnew/hermes-agent/venv/Scripts/python.exe"
    T="$LOCALAPPDATA/Temp/providerchange-harness"
    rm -rf "$T" && mkdir -p "$T"
    cp H:/application/hermesnew/home/config.yaml "$T/config.yaml"
    cp H:/application/hermesnew/home/.env         "$T/.env"
    HERMES_HOME="$(cygpath -w "$T")" "$V" -m unittest discover -s tests -v
    rm -rf "$T"      # 副本里含明文密钥，跑完必须删

⚠️ **v0.0.3 系列的跑法改用主 Agent 钉住的 harness 变体**（``tasksv0.0.3/progress.md`` §八，
2026-09-25 用户裁决）：真配置已结构性漂移（两条 legacy 条目被并进 ``providers:``），
副本源改成 **09-24 基线备份对**（``providerchange-backups/config.yaml.bak-20260924-140439``
与 ``env.bak-20260924-140439``，**只读源**），其余命令逐字不变。
「只写副本 / 跑完 ``rm -rf`` / 真 ``config.yaml`` 与 ``.env`` 全程只读」三条纪律照旧。

本文件是本项目的**第一处写路径测试**，四条纪律：

* **只写副本**：每个写动作第一件事就是 ``self._guard_before_write()`` —— 断言
  ``hermes_cli.config.get_config_path()`` 落在 ``HERMES_HOME``（临时副本）之下、且该目录
  在系统临时目录里（prompt §5.2 的硬保险）。``setUp`` / ``tearDown`` 各自按字节还原副本，
  所以用例顺序无关、破坏性写在用例内闭环。真 ``config.yaml`` / ``.env`` 全程只读。
* **零真密钥**：断言用的密钥值一律是本文件造的形状（``sk-FAKE-TEST-*``）；真实副本里的密钥
  只被**哈希后**用来比对「有没有被动过」，内容不进期望值、不进断言消息。
* **零网络**：保存链路上没有任何探测（官方 ``upsert_custom_endpoint`` 只写 config）。
  ``self._save`` 把 M3 的 ``validate_custom_endpoint`` 换成「一被调用就失败」的桩，钉住这层含义。
* **计数不硬编码**（prompt §2.7）：条目数 / 白名单条数 / 「还剩几条」全部**先从副本读**，
  再断言运算关系；``2026-09-22`` 快照（3 条 ``providers:`` + 2 条 legacy）只出现在注释里。

payload 形状 = 前端 ``plugin.js`` 的 ``buildSavePayload``（§7.7 ``FormState`` → snake_case，
M3↔M4 共享契约）；:func:`_payload` 是它的 Python 镜像，字段增减必须两边同步。
「查看 → 编辑」的回填种子（``formFromRow`` 的「已添加」栏）由 :func:`_form_added_seed` 镜像：
契约补丁（``allowlist_models`` = 磁盘白名单）之后它**优先吃新字段**，两边也必须同步。
"""

from __future__ import annotations

import copy
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

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli.config import (
    custom_endpoint_key_env,
    get_config_path,
    get_env_path,
    invalidate_env_cache,
    read_raw_config,
)
from hermes_cli.web_models import CustomEndpointUpdate

PRODUCT_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = PRODUCT_ROOT / "dashboard"
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

import plugin_api  # noqa: E402  （prompt §5.2：先把 dashboard 塞进 sys.path 再 import）

MOUNT_PREFIX = "/api/plugins/supplier-models"
SAVE_PATH = f"{MOUNT_PREFIX}/endpoints"        # 插件自己的保存路由（M4.1）
LIST_PATH = f"{MOUNT_PREFIX}/endpoints"        # M1 的只读双读路由（编辑回填的数据源）
# M5 的清空路由：本文件只把它当**前置状态**用（「先清空、再按编辑器种子保存」的回归）
CLEAR_PATH_TEMPLATE = f"{MOUNT_PREFIX}/endpoints/{{endpoint_id}}/clear-models"

# 新建用例用的 id / 端点：``zz-`` 前缀确保不与本机真实条目撞车；``127.0.0.1`` 保证即使
# 有人误接网络也连不到任何外部主机（保存链路本身一个请求都不发）。
NEW_ID = "zz-m4-save-copy"
NEW_ENV_VAR = custom_endpoint_key_env(NEW_ID)
NEW_BASE_URL = "http://127.0.0.1:55999/v1"
FAKE_KEY_PREFIX = "sk-FAKE-TEST-"
FAKE_KEY = f"{FAKE_KEY_PREFIX}m4-save-0123456789abcdef"
FAKE_MODEL_A = "fake/m4-model-a"
FAKE_MODEL_B = "fake/m4-model-b"
# 清空 → 保存回归的**优先**取材对象（本机 2026-09-22 快照里那条被灌进 22 个模型的条目）。
# ⚠️ 只是优先级，**不是**硬编码期望值：拿不到就退到任一满足条件的 providers: 条目（prompt §2.7）
PREFERRED_CLEAR_ID = "token_rhythm"

# M9 新增用例（设计 §1.5 a–g）的**合成条目**取材：``zz-`` 前缀确保不与本机真实条目撞车，
# 且 id 经官方 ``_custom_endpoint_id`` slug 化后**不变**（否则等于换了一条条目，判据失去落点
# —— 与 :meth:`SaveRouteWriteTest._clearable_sample` 的稳定性判据同一口径）。
# 模型 id 一律是本文件造的假串，期望值再从中**运行时**算，绝不硬编码条数（prompt §2.7）。
SYNTH_ID = "zz-m9-direct-branch"
SYNTH_NAME = "M9 保存即所得用例"
SYNTH_MODELS = ["fake/m9-first", "fake/m9-second", "fake/m9-third", "fake/m9-fourth"]
SYNTH_CONTEXT = 4096


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


def _set_env_value(text: str, name: str, value: str) -> str:
    """把副本 ``.env`` 里的 ``NAME=`` 换成 ``value``（没有这一行就追加）。"""
    lines = text.splitlines(keepends=True)
    pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(name)}=")
    for index, line in enumerate(lines):
        body, eol = _split_eol(line)
        if pattern.match(body):
            lines[index] = f"{name}={value}{eol}"
            return "".join(lines)
    joiner = "" if (not lines or lines[-1].endswith(("\n", "\r"))) else "\n"
    return text + f"{joiner}{name}={value}\n"


def _env_value(text: str, name: str) -> Optional[str]:
    """副本 ``.env`` 文本里 ``NAME`` 的值（去引号）；没有这个变量 → ``None``。"""
    for line in text.splitlines():
        body, _eol = _split_eol(line)
        match = re.match(rf"^\s*(?:export\s+)?{re.escape(name)}=(.*)$", body)
        if match:
            return match.group(1).strip().strip('"').strip("'")
    return None


def _raw_providers(raw: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    providers = (raw or {}).get("providers")
    if not isinstance(providers, dict):
        return {}
    return {str(key): entry for key, entry in providers.items() if isinstance(entry, dict)}


def _sha(value: Any) -> str:
    return hashlib.sha256(str(value if value is not None else "").encode("utf-8")).hexdigest()


def _load_copy_yaml() -> Dict[str, Any]:
    """副本 YAML 的**原样**解析（造前置状态用；不走平台缓存）—— 与 ``test_migrate`` 同手法。"""
    data = yaml.safe_load(_read_copy_bytes("config.yaml").decode("utf-8"))
    return data if isinstance(data, dict) else {}


def _store_copy_yaml(data: Dict[str, Any]) -> None:
    """把改过的前置状态写回**副本**（``tearDown`` 会还原成 pristine 字节，破坏性写在用例内闭环）。"""
    _write_copy_bytes(yaml.safe_dump(data, allow_unicode=True, sort_keys=False).encode("utf-8"))
    invalidate_env_cache()


def _changed_paths(before: Any, after: Any, path: str = "") -> List[str]:
    """两份解析后 config 的**值级**差异路径（只报「路径 + 变化种类」，**绝不回显值** ——
    副本里带着明文密钥，断言消息会进测试输出）。判据口径与 M5 / M6 / M7 相同（批 2 决策 3），
    形状复用 ``test_activate_delete.py`` 的 surgical 判据（设计 §1.5 g）。
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
            return [f"{path}[len] !changed"]
        merged: List[str] = []
        for index, (left, right) in enumerate(zip(before, after)):
            merged.extend(_changed_paths(left, right, f"{path}[{index}]"))
        return merged
    return [] if before == after else [f"{path} !changed"]


def _payload(endpoint_id: str, name: str, base_url: str, model: str, models: List[str],
             api_key: Optional[str] = None, context_length: Optional[int] = None,
             discover_models: bool = True, make_default: bool = False) -> Dict[str, Any]:
    """前端 ``buildSavePayload`` 的镜像（M3↔M4 契约，§7.7 ``FormState`` → snake_case）。

    ``api_key=None`` → **整个字段不发**（铁律 2 的「缺省 = 保留」在 JSON 层的写法：
    ``form.apiKey.trim() || undefined`` 经 ``JSON.stringify`` 后字段消失）；
    ``api_key=""`` 才是「清空密钥」。
    ``discover_models`` 故意允许传 ``True``，用来证明后端无条件改写（铁律 1）。
    """
    body: Dict[str, Any] = {
        "base_url": base_url,
        "discover_models": discover_models,
        "id": endpoint_id,
        "make_default": make_default,
        "model": model,
        "models": list(models),
        "name": name,
    }
    if api_key is not None:
        body["api_key"] = api_key
    if context_length is not None:
        body["context_length"] = context_length
    return body


def _form_added_seed(row: Dict[str, Any]) -> List[str]:
    """``plugin.js`` 的 `formFromRow` 里「已添加」栏种子的 **Python 镜像**（同一处判据）。

    与 ``test_clear_models._form_added_seed`` 是同一份规则（两个文件各自独立写一遍，是为了
    让「期望值」不共享实现；prompt §2.7）。规则：`allowlist_models` 是数组 → 用它（磁盘真值），
    否则退回 `models`（现行行为）；去空去重保序；**只在非空时**把行上的 `model:` 折到第 0 位。
    本文件的 `_row_payload` 用它替代「直接吃 `row["models"]`」，这样保存侧的回归测的就是
    编辑器真正会发出去的那份 payload。改 JS 那句时必须同步改这里。
    """
    seed = row.get("allowlist_models")
    if not isinstance(seed, list):
        seed = row.get("models") if isinstance(row.get("models"), list) else []
    ids: List[str] = []
    for item in seed:
        text = str(item or "").strip()
        if text and text not in ids:
            ids.append(text)
    row_model = str(row.get("model") or "").strip()
    if row_model and ids and row_model not in ids:
        ids.insert(0, row_model)
    return ids


class SaveRouteWriteTest(unittest.TestCase):
    """M4.1 / M4.10：保存恒钉住、Key 三态、形状为 dict、失败原文透传。"""

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
        self.assertTrue(self.env_exists, "副本里没有 .env —— §5.1 的 harness 命令要求一并复制 .env")
        self.assertEqual(str((_home() / ".env").resolve()).lower(),
                         str(Path(get_env_path()).resolve()).lower(),
                         "get_env_path() 不在副本上，.env 写测试会碰真配置")
        self._restore_copy()
        self.raw = read_raw_config()
        self.providers = _raw_providers(self.raw)
        self.assertTrue(self.providers,
                        "副本里没有 providers: 条目 —— 保存断言失去意义，先按 progress §五 重建 harness 副本")

    def tearDown(self):
        self._restore_copy()

    def _guard_before_write(self) -> None:
        """每个写动作前的红线断言（M4 是第一处直接写 config 的模块，这道守卫不许省）。"""
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

    # ------------------------------------------------------------------ 取材

    def _entry_with_key_env(self) -> Tuple[str, Dict[str, Any]]:
        """副本里 ``providers:`` 中「有 ``key_env`` 且磁盘上没有明文 ``api_key``」的条目（T23 取材）。"""
        matches = [(pid, entry) for pid, entry in sorted(self.providers.items())
                   if str(entry.get("key_env") or "").strip()
                   and not str(entry.get("api_key") or "").strip()]
        self.assertTrue(matches, "副本里没有带 key_env 的 providers: 条目，Key 保留/清空用例无从验证")
        return matches[0]

    def _entry_with_context_length(self) -> Optional[Tuple[str, Dict[str, Any]]]:
        matches = [(pid, entry) for pid, entry in sorted(self.providers.items())
                   if isinstance(entry.get("context_length"), int) and entry["context_length"] > 0]
        return matches[0] if matches else None

    def _seed_env_key(self, env_name: str, fake: str) -> None:
        """把假密钥写进**副本**的 ``.env``（前提不成立就不许往下走）。"""
        self._guard_before_write()
        text = _read_copy_bytes(".env").decode("utf-8")
        _write_copy_bytes(_set_env_value(text, env_name, fake).encode("utf-8"), ".env")
        invalidate_env_cache()
        self.assertEqual(fake, _env_value(_read_copy_bytes(".env").decode("utf-8"), env_name),
                         f"假密钥没能写进副本 .env 的 {env_name}，前提不成立")

    def _row_of(self, endpoint_id: str) -> Dict[str, Any]:
        """从 M1 的 ``GET /endpoints`` 取行（前端编辑回填的数据源，不自己拼形状）。"""
        response = self.client.get(LIST_PATH)
        self.assertEqual(200, response.status_code, response.text)
        rows = [row for row in (response.json().get("endpoints") or [])
                if str(row.get("id") or "") == endpoint_id]
        self.assertEqual(1, len(rows),
                         f"GET /endpoints 里 {endpoint_id} 应当恰好一行，实到 {len(rows)} 行")
        return rows[0]

    def _row_payload(self, row: Dict[str, Any], api_key: Optional[str] = None,
                     models: Optional[List[str]] = None,
                     context_length: Optional[int] = None) -> Dict[str, Any]:
        """``formFromRow`` + ``buildSavePayload`` 的等价写法：回填行数据，**Key 不回填**。

        ``models`` 缺省走 :func:`_form_added_seed`（= 补丁后的种子：优先 ``allowlist_models``），
        所以本文件既有的「按行回填再保存」用例测的一直是页面真正会发出去的那份 payload。
        """
        return _payload(
            endpoint_id=str(row.get("id") or ""),
            name=str(row.get("name") or ""),
            base_url=str(row.get("base_url") or ""),
            model=str(row.get("model") or ""),
            models=list(models if models is not None else _form_added_seed(row)),
            api_key=api_key,
            context_length=(row.get("context_length") or None)
            if context_length is None else context_length,
            # 前端**永远**发 false（铁律 1）；后端还额外无条件改写一次，两道都在本文件覆盖
            discover_models=False,
            make_default=False)

    def _save(self, body: Dict[str, Any]):
        """``POST /endpoints``：先跑红线守卫，再把 validate 换成「一调用就失败」的桩。"""
        self._guard_before_write()
        with mock.patch.object(plugin_api, "validate_custom_endpoint",
                               side_effect=AssertionError("保存链路不许发探测请求（M4 只写 config）")):
            return self.client.post(SAVE_PATH, json=body)

    def _raw_entry(self, endpoint_id: str) -> Optional[Dict[str, Any]]:
        return _raw_providers(read_raw_config()).get(endpoint_id)

    # ------------------------------------------------------- M4.1 / 铁律 1：恒钉住

    def test_saved_entry_always_has_discover_models_false(self):
        """M4.10 第一条：请求里写 ``discover_models: true`` 也没用 —— 落盘恒 ``False``（铁律 1）。

        这正是 §2.3 真相 1 的坑：不强制的话下一次 live 探测会把白名单覆盖成端点全量目录。
        """
        before = set(self.providers)
        response = self._save(_payload(
            NEW_ID, "M4 保存用例", NEW_BASE_URL, FAKE_MODEL_A, [FAKE_MODEL_A, FAKE_MODEL_B],
            api_key=FAKE_KEY, discover_models=True))
        self.assertEqual(200, response.status_code, response.text)
        payload = response.json()
        self.assertTrue(payload.get("ok"), payload)
        self.assertEqual(NEW_ID, payload.get("id"))

        entry = self._raw_entry(NEW_ID)
        self.assertIsInstance(entry, dict, "新建条目没落进 providers:")
        self.assertIs(False, entry.get("discover_models"),
                      "铁律 1 破了：前端勾了「允许自动发现」就被写进 config")
        self.assertEqual(before | {NEW_ID}, set(_raw_providers(read_raw_config())),
                         "保存新建时动了别的条目（应当只多一条）")
        # 徽章依据：M1 的行也要如实反映钉住状态（前端 readPinBadge 就吃这个字段）
        self.assertIs(False, self._row_of(NEW_ID).get("discover_models"))

    def test_new_entry_shape_is_providers_dict_with_dict_models(self):
        """M4.10 附加 / F3：新建走 ``providers:`` 形态，``models:`` 是 **dict**（§7.2 / §7.3）。"""
        response = self._save(_payload(
            NEW_ID, "M4 形状用例", NEW_BASE_URL, FAKE_MODEL_A, [FAKE_MODEL_A, FAKE_MODEL_B],
            api_key=FAKE_KEY))
        self.assertEqual(200, response.status_code, response.text)
        entry = self._raw_entry(NEW_ID)
        self.assertIsInstance(entry, dict)
        models = entry.get("models")
        self.assertIsInstance(models, dict,
                              f"models 必须是 dict 形状（§7.3），实到 {type(models).__name__}")
        self.assertEqual([FAKE_MODEL_A, FAKE_MODEL_B], list(models))
        self.assertTrue(all(isinstance(meta, dict) for meta in models.values()),
                        "models 的子字段必须是 dict（可带 context_length 元数据）")
        self.assertEqual("M4 形状用例", entry.get("name"))
        self.assertEqual(NEW_BASE_URL, entry.get("base_url"))
        self.assertEqual(FAKE_MODEL_A, entry.get("model"))
        # 密钥只进 .env，config.yaml 里只有 key_env 引用
        self.assertEqual(NEW_ENV_VAR, entry.get("key_env"))
        self.assertNotIn("api_key", entry, "明文密钥不许落进 config.yaml（§6.3 密钥铁律）")
        self.assertEqual(FAKE_KEY, _env_value(_read_copy_bytes(".env").decode("utf-8"), NEW_ENV_VAR))

    def test_empty_models_saves_only_the_default_model(self):
        """不变式 3：**白名单可以为空**，但默认模型会被无条件折回池 → 落盘「只剩默认模型」。

        ``_write_custom_endpoint:481`` 遍历 ``(*body.models, model)``，所以 ``models: []`` 的
        保存结果恒为「只剩默认模型那一条」。**M9 的新口径**：保存分两条通道 ——
        **有删除差集时走插件直写支**（官方只增不减，删不掉），**无删除时走官方 upsert**
        （本用例是**新建**：磁盘还没有这条条目 ⇒ 差集为空 ⇒ 必然官方支）。
        UI 文案口径不变：「只剩默认模型」，**不许**写「不提供任何模型」。
        """
        response = self._save(_payload(NEW_ID, "M4 空白名单用例", NEW_BASE_URL, FAKE_MODEL_A, [],
                                       api_key=FAKE_KEY))
        self.assertEqual(200, response.status_code, response.text)
        models = (self._raw_entry(NEW_ID) or {}).get("models")
        self.assertEqual({FAKE_MODEL_A: {}}, models,
                         "models 传空时落盘应当只剩默认模型一条（不变式 3）")
        self.assertIs(False, (self._raw_entry(NEW_ID) or {}).get("discover_models"))
        # M9.10：新建**不误入直写支**（回执通道是测试钉链路选择的机器可读判据）
        receipt = response.json()
        self.assertEqual(plugin_api.SAVE_CHANNEL_OFFICIAL, receipt.get("write_channel"),
                         "新建（磁盘无差集）应当走官方 upsert，不该出现插件直写通道")
        self.assertEqual(0, receipt.get("removed_models"),
                         "新建没有删掉任何模型，回执却报了删除数（拍板 #12 的数字必须真实）")

    # -------------------------------- 契约补丁（编辑态）：清空 → 空种子 → 保存只该带默认模型回去

    def _clear(self, endpoint_id: str):
        """M5 的 ``clear-models``：本文件只借它造「刚清空」的**前置态**（判据仍归 test_clear_models）。

        与 :meth:`_save` 同样先过红线守卫，并把探测函数换成「一调用就失败」的桩。
        """
        self._guard_before_write()
        with mock.patch.object(plugin_api, "validate_custom_endpoint",
                               side_effect=AssertionError("清空链路不许发探测请求（纯 config 操作）")):
            return self.client.post(CLEAR_PATH_TEMPLATE.format(endpoint_id=endpoint_id))

    def _clearable_sample(self) -> Tuple[str, Dict[str, Any]]:
        """一个「``providers:`` + 白名单是非空 dict + 有默认模型 + 磁盘上没有**明文**密钥 +
        **id 经官方 slug 化后不变**」的条目。

        明文密钥要排除：官方 ``_write_custom_endpoint:507-512`` 会顺手把它迁进 ``.env``，
        那与本用例的判据（磁盘 ``models:`` 与密钥零改动）无关却会把红字写脏。
        ``${VAR}`` 模板不算明文（与后端 ``_raw_key_is_plaintext`` 同判据）。
        id 稳定性也要排除：官方 ``_custom_endpoint_id`` 会把键重写一遍（非 ``[A-Za-z0-9_-]`` →
        ``-``、剥首尾 ``-_``、小写），换个键就等于换了一条条目 —— 本用例全程盯**同一条目**的落盘。
        """
        def usable(endpoint_id: str, entry: Dict[str, Any]) -> bool:
            raw_key = str(entry.get("api_key") or "").strip()
            plaintext = bool(raw_key) and not raw_key.startswith("${")
            stable_id = endpoint_id == re.sub(r"[^A-Za-z0-9_-]+", "-", endpoint_id).strip("-_").lower()
            return (stable_id and isinstance(entry.get("models"), dict) and bool(entry.get("models"))
                    and bool(str(entry.get("model") or "").strip()) and not plaintext)

        matches = [(pid, entry) for pid, entry in sorted(self.providers.items())
                   if isinstance(entry, dict) and usable(str(pid), entry)]
        self.assertTrue(matches,
                        "副本里没有「providers: + 白名单非空 dict + 有默认模型 + 无明文密钥 + id 稳定」的条目，"
                        "清空 → 保存的回归判据无从验证（先按 progress §五 重建 harness 副本）")
        preferred = [item for item in matches if str(item[0]) == PREFERRED_CLEAR_ID]
        return preferred[0] if preferred else matches[0]

    def test_clear_then_seed_gives_the_editor_an_empty_allowlist(self):
        """回归（后端层）：清空后 ``allowlist_models == []`` ⇒ 种子为空 ⇒ 编辑器不再替用户
        「已添加」那一条默认模型（M4.7 / UC-12 的意图；旧种子会给出 ``[默认模型]``）。
        """
        endpoint_id, entry_before = self._clearable_sample()
        default_model = str(entry_before.get("model") or "")
        cleared_ids = {str(key) for key in (entry_before.get("models") or {})}
        self.assertTrue(cleared_ids - {default_model},
                        "样本清空前的白名单里除了默认模型没有别的 id，本用例的差分判据没意义")
        self.assertEqual(200, self._clear(endpoint_id).status_code)

        row = self._row_of(endpoint_id)
        self.assertEqual([], row["allowlist_models"],
                         "清空后的行没带回空的磁盘白名单（formFromRow 的新种子源）")
        self.assertEqual(0, row["allowlist_count"])
        self.assertEqual([default_model], row["models"],
                         "注入视图（既有字段）语义变了 —— 本补丁不该碰它")
        self.assertEqual([], _form_added_seed(row),
                         "编辑器仍把注入的默认模型当成用户已添加的那一条（formFromRow 缺口未修）")
        # 差分证据：老形状（没有该字段 → 退回现行行为）才会带回默认模型那一条
        self.assertEqual([default_model],
                         _form_added_seed(dict(row, allowlist_models=None)),
                         "字段缺失时的回退路径不再等价于旧行为")

    def test_save_after_clear_writes_back_only_the_default_model(self):
        """保存侧回归（**副本**上）：清空 → 按新种子发 ``models: []`` → 落盘只剩默认模型那一条，
        被清掉的 id **一个都不回来**；行上的两个派生字段与磁盘重新对齐。

        ⚠️ 实测口径（读 ``_write_custom_endpoint:479-487`` 得）：``models_map`` 先从 existing
        entry 拷出来，再遍历 ``(*body.models, model)`` —— 官方的 ``model:`` 是**无条件**折进去的
        （``model`` 还是必填字段，``:451-452`` 缺了就 400）。所以「payload 的 models 为空」**不会**
        让磁盘保持 ``{}``：清空态在官方 upsert 下做不到 —— M5 的 clear-models 因此自己直写，
        而**保存**这一侧自 M9 起也有了自己的直写支（有删除差集才走）。本例此刻磁盘是 ``{}``
        ⇒ **差集为空 ⇒ 仍必须走官方 upsert**，这枚通道断言就是「直写支误伤无删除保存」的回归钉
        （同一件事在**新建**路径由 :meth:`test_empty_models_saves_only_the_default_model` 钉着）。
        本用例断的是四件真话：
        ① 编辑器发出去的 payload 里 ``models`` 是空（补丁生效的直接证据，旧种子会发 ``[默认模型]``）；
        ② 落盘的 ``models`` 恰为 ``{默认模型: {}}`` —— 清空前那批 id 零回流（UC-12 / T24 的意图）；
        ③ 保存后的行上 ``allowlist_models`` / ``allowlist_count`` 等于新的磁盘真值；
        ④ 回执 ``write_channel == official`` 且 ``removed_models == 0``（M9.11 的通道同步）。
        """
        endpoint_id, entry_before = self._clearable_sample()
        default_model = str(entry_before.get("model") or "")
        cleared_ids = {str(key) for key in (entry_before.get("models") or {})}
        others_before = {pid: copy.deepcopy(entry)
                         for pid, entry in self.providers.items() if pid != endpoint_id}
        legacy_before = copy.deepcopy(read_raw_config().get("custom_providers") or [])
        self.assertEqual(200, self._clear(endpoint_id).status_code)
        env_before = _read_copy_bytes(".env")

        row = self._row_of(endpoint_id)
        payload = self._row_payload(row, models=_form_added_seed(row), context_length=0)
        self.assertEqual([], payload["models"],
                         "保存的 payload 没按磁盘真值种子（allowlist_models 没被 formFromRow 吃掉）")
        self.assertNotIn("api_key", payload, "本用例不改密钥：字段缺失 = 保留（铁律 2）")

        response = self._save(payload)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(endpoint_id, response.json().get("id"))
        # ④ 通道同步（M9.11 的防误伤回归钉）：磁盘 {} ⇒ 无差集 ⇒ 官方支、删除数 0
        self.assertEqual(plugin_api.SAVE_CHANNEL_OFFICIAL, response.json().get("write_channel"),
                         "清空后的保存（磁盘无差集）被误投到插件直写支")
        self.assertEqual(0, response.json().get("removed_models"),
                         "清空后的保存什么都没删，回执却报了删除数")

        entry = self._raw_entry(endpoint_id)
        self.assertIsInstance(entry, dict, "保存把该条目弄丢了")
        self.assertEqual({default_model: {}}, entry.get("models"),
                         f"落盘不是「只剩默认模型」那一条（实到 {sorted((entry.get('models') or {}))}）")
        self.assertEqual(set(), (cleared_ids - {default_model}) & set(entry.get("models") or {}),
                         "被清掉的白名单 id 随保存回流了（UC-12 / T24 的意图被破坏）")
        self.assertEqual(default_model, str(entry.get("model") or ""),
                         "保存改到了条目的默认模型（清空≠池里空，E7）")
        self.assertIs(False, entry.get("discover_models"), "铁律 1：保存恒钉住")
        # ③ 派生字段与磁盘对齐（保存响应自己也走 M1 的组装）
        after = self._row_of(endpoint_id)
        self.assertEqual([default_model], after["allowlist_models"],
                         "保存后的 allowlist_models 不等于新的磁盘真值")
        self.assertEqual(1, after["allowlist_count"])
        self.assertEqual(after["allowlist_count"], len(after["allowlist_models"]))
        saved = [r for r in (response.json().get("endpoints") or []) if r.get("id") == endpoint_id]
        self.assertEqual(1, len(saved), "写响应的行里该条目没了")
        self.assertEqual([default_model], saved[0]["allowlist_models"],
                         "写响应的行没带回保存后的磁盘真值")
        self.assertEqual(saved[0]["models"], saved[0]["allowlist_models"],
                         "默认模型已落盘 ⇒ 此刻视图与磁盘真值该重合，不重合说明两个字段错位")
        # 无损性：其它条目与 legacy 段一字节都不动，不改 Key 的保存也不重写 .env
        providers_after = _raw_providers(read_raw_config())
        self.assertEqual(set(others_before) | {endpoint_id}, set(providers_after))
        for pid, snapshot in others_before.items():
            self.assertEqual(snapshot, providers_after.get(pid),
                             f"保存 {endpoint_id} 时改到了无关条目 {pid}")
        self.assertEqual(legacy_before, read_raw_config().get("custom_providers"),
                         "保存 providers: 条目时碰了 custom_providers:")
        self.assertEqual(env_before, _read_copy_bytes(".env"), "不发 api_key 的保存动了 .env")

    def test_old_and_new_seed_payloads_land_on_the_same_disk_allowlist(self):
        """**能力边界**（实测记录，防误读）：清空后「旧种子」``models: [默认模型]`` 与
        「新种子」``models: []`` 两份 payload 的落盘结果**逐键相同**（都是 ``{默认模型: {}}``）——
        官方 upsert 无条件把必填的 ``model:`` 折进 ``models_map``（``_write_custom_endpoint:481``），
        所以「保存会把默认模型写回盘」这一半**不是** ``formFromRow`` 能修的（要它空着只能走 M5 的
        直写清空）。本补丁修的是**编辑器与请求体的真实性**（M4.7）。
        这条测试把边界钉住：日后拿「磁盘还是出现了 1 条」当本补丁失效的证据，这里先红。
        **M9.12 追加的半边**：两份 payload 的回执都必须是 ``write_channel == official``
        且**插件侧直写计数器 = 0**（monkeypatch 打在 ``plugin_api`` 自己的 ``save_config``
        绑定上）—— 无删除时插件一次直写都不许发生。
        """
        endpoint_id, entry_before = self._clearable_sample()
        default_model = str(entry_before.get("model") or "")

        # A) 新种子（磁盘真值 = 空）
        self.assertEqual(200, self._clear(endpoint_id).status_code)
        new_seed = _form_added_seed(self._row_of(endpoint_id))
        self.assertEqual([], new_seed)
        # 计数器打在 **plugin_api 自己 import 的 save_config 绑定**上（M6.7 同手法）：
        # 官方 upsert 内部落盘走的是 ``config_env`` 那份绑定，不经过这个口 ——
        # 所以「0 次」就是「无删除时插件绝不直写」的直接证据。
        with mock.patch.object(plugin_api, "save_config",
                               wraps=plugin_api.save_config) as counter_new:
            response_new = self._save(
                self._row_payload(self._row_of(endpoint_id), models=new_seed, context_length=0))
        self.assertEqual(200, response_new.status_code, response_new.text)
        self.assertEqual(plugin_api.SAVE_CHANNEL_OFFICIAL, response_new.json().get("write_channel"))
        self.assertEqual(0, response_new.json().get("removed_models"))
        self.assertEqual(0, counter_new.call_count,
                         "无删除的保存里插件自己动过盘（直写支误伤了官方支）")
        models_new = dict((self._raw_entry(endpoint_id) or {}).get("models") or {})

        # B) 旧种子（注入视图 = [默认模型]）—— 两份 payload 确实不同，落盘却必须相同
        self.assertEqual(200, self._clear(endpoint_id).status_code)
        old_seed = _form_added_seed(dict(self._row_of(endpoint_id), allowlist_models=None))
        self.assertEqual([default_model], old_seed)
        self.assertNotEqual(new_seed, old_seed, "两份 payload 一样 ⇒ 本用例的差分前提不成立")
        with mock.patch.object(plugin_api, "save_config",
                               wraps=plugin_api.save_config) as counter_old:
            response_old = self._save(
                self._row_payload(self._row_of(endpoint_id), models=old_seed, context_length=0))
        self.assertEqual(200, response_old.status_code, response_old.text)
        self.assertEqual(plugin_api.SAVE_CHANNEL_OFFICIAL, response_old.json().get("write_channel"))
        self.assertEqual(0, response_old.json().get("removed_models"))
        self.assertEqual(0, counter_old.call_count,
                         "无删除的保存里插件自己动过盘（直写支误伤了官方支）")
        models_old = dict((self._raw_entry(endpoint_id) or {}).get("models") or {})

        self.assertEqual(models_old, models_new,
                         "两种种子的落盘结果开始分叉：官方 upsert 的 model: 折叠语义变了，须复核")
        self.assertEqual({default_model: {}}, models_new)
        self.assertEqual(1, self._row_of(endpoint_id)["allowlist_count"],
                         "落盘 1 条却报别的数 —— 派生字段与磁盘脱节")

    # --------------------------------------------- M4.10 / 铁律 2：Key 缺省 = 保留

    def test_api_key_field_absent_keeps_key_env_and_env_value(self):
        """T23（防 E6）：编辑已有供应商**不改 Key**（字段缺失）→ ``key_env`` 与 ``.env`` 变量都在。"""
        endpoint_id, entry = self._entry_with_key_env()
        env_name = str(entry.get("key_env") or "").strip()
        fake = f"{FAKE_KEY_PREFIX}keep-0123456789abcdef"
        self._seed_env_key(env_name, fake)
        before_entry = copy.deepcopy(self._raw_entry(endpoint_id))
        self.assertEqual(fake, _env_value(_read_copy_bytes(".env").decode("utf-8"), env_name))

        response = self._save(self._row_payload(self._row_of(endpoint_id)))   # 不发 api_key 字段
        self.assertEqual(200, response.status_code, response.text)

        after_entry = self._raw_entry(endpoint_id)
        self.assertEqual(env_name, after_entry.get("key_env"),
                         "Key 字段缺失时 key_env 被摘掉了（E6 回归）")
        self.assertNotIn("api_key", after_entry, "缺省 Key 时不该把密钥写成明文")
        self.assertEqual(fake, _env_value(_read_copy_bytes(".env").decode("utf-8"), env_name),
                         "Key 字段缺失时 .env 变量被动过（E6 回归）")
        self.assertEqual(set(before_entry.get("models") or {}), set(after_entry.get("models") or {}),
                         "只回填行数据的保存不该增删白名单模型")
        self.assertEqual(before_entry.get("model"), after_entry.get("model"))
        self.assertIs(False, after_entry.get("discover_models"))

    def test_api_key_empty_string_clears_key_and_drops_key_env(self):
        """M4.10：``api_key: ""`` → 密钥被清空且 ``key_env`` 被摘。**这是预期行为，不是 bug**
        （§3.2 UC-02 实测 E6；UI 上显式清密钥是独立动作 + 二次确认，不混在普通保存里）。
        """
        endpoint_id, entry = self._entry_with_key_env()
        env_name = str(entry.get("key_env") or "").strip()
        self._seed_env_key(env_name, f"{FAKE_KEY_PREFIX}clear-0123456789abcdef")

        response = self._save(self._row_payload(self._row_of(endpoint_id), api_key=""))
        self.assertEqual(200, response.status_code, response.text)

        after = self._raw_entry(endpoint_id)
        self.assertNotIn("key_env", after, "传空串应当摘掉 key_env")
        self.assertNotIn("api_key", after, "传空串不该留下明文密钥")
        self.assertIsNone(_env_value(_read_copy_bytes(".env").decode("utf-8"), env_name),
                          "传空串应当删掉 .env 里的变量（官方 remove_env_value 的语义）")

    def test_save_is_surgical_other_entries_untouched(self):
        """无损性（保存侧）：改一条不该动其它条目的任何一个字段，也不该重写 ``.env``。"""
        endpoint_id, _entry = self._entry_with_key_env()
        others_before = {pid: copy.deepcopy(raw)
                         for pid, raw in self.providers.items() if pid != endpoint_id}
        legacy_before = copy.deepcopy(read_raw_config().get("custom_providers") or [])
        env_before = _read_copy_bytes(".env")

        response = self._save(self._row_payload(self._row_of(endpoint_id)))
        self.assertEqual(200, response.status_code, response.text)

        providers_after = _raw_providers(read_raw_config())
        self.assertEqual(set(others_before) | {endpoint_id}, set(providers_after))
        for pid, snapshot in others_before.items():
            self.assertEqual(snapshot, providers_after.get(pid),
                             f"保存 {endpoint_id} 时改到了无关条目 {pid}")
        self.assertEqual(legacy_before, read_raw_config().get("custom_providers"),
                         "保存 providers: 条目不该碰 custom_providers:")
        self.assertEqual(env_before, _read_copy_bytes(".env"), "不改 Key 的保存不该重写 .env")

    def test_context_length_is_preserved_or_written_explicitly(self):
        """``context_length``：不传 → 官方从 existing entry 原样带走；传 → 写进条目与默认模型子字段。

        这条是「钉住路径**故意不回填** ``context_length``」的理由（回填会给默认模型的子字段
        多加一个键），也是 M5/M6 复用同一口径时的参照。
        """
        picked = self._entry_with_context_length()
        self.assertTrue(picked, "副本里没有带 context_length 的 providers: 条目，本用例无从验证")
        endpoint_id, before = picked
        stored = before.get("context_length")

        row = self._row_of(endpoint_id)
        response = self._save(self._row_payload(row, context_length=0))   # 0 → 不写该字段
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(stored, self._raw_entry(endpoint_id).get("context_length"),
                         "context_length 传 0/None 时应当原样保留，不是被抹掉")

    # ---------------------------------------------------------- 错误原文透传（M4.8）

    def test_backend_validation_detail_passes_through_verbatim(self):
        """M4.8 的前提：官方 400 的 ``detail`` 原文出口，前端才能「透传后端 detail 原文」。

        同时证明「校验失败 = 一个字都不写」（400 抛在 ``_write_custom_endpoint:444-452``，
        早于 ``save_config``）。
        """
        config_before = _read_copy_bytes("config.yaml")
        for base_url, detail in [("","base_url required"), ("not-a-url",
                                  "base_url must include scheme and host")]:
            self._guard_before_write()
            response = self._save(_payload("zz-m4-invalid", "M4 失败用例", base_url,
                                           FAKE_MODEL_A, [FAKE_MODEL_A], api_key=FAKE_KEY))
            self.assertEqual(400, response.status_code, response.text)
            self.assertEqual(detail, (response.json() or {}).get("detail"),
                             "后端 detail 必须原文透传（前端 toast 的判据）")
        self.assertIsNone(self._raw_entry("zz-m4-invalid"), "校验失败的请求不该留下条目")
        self.assertEqual(config_before, _read_copy_bytes("config.yaml"), "校验失败不该写副本")

    def test_missing_name_returns_official_400(self):
        """缺 ``name`` → 官方 ``_write_custom_endpoint:444-445`` 抛 400（不是「用空值覆盖」）。"""
        response = self._save(_payload("zz-m4-nameless", "   ", NEW_BASE_URL, FAKE_MODEL_A,
                                       [FAKE_MODEL_A], api_key=FAKE_KEY))
        self.assertEqual(400, response.status_code, response.text)
        self.assertEqual("name required", (response.json() or {}).get("detail"))
        self.assertIsNone(self._raw_entry("zz-m4-nameless"))

    # ------------------------------------------------------------ 密钥不出网关（M1.5）

    def test_save_response_carries_no_key_material(self):
        """保存的响应沿用 M1 的归一化：任何字段里没有密钥内容（连 redact 预览都没有）。"""
        response = self._save(_payload(NEW_ID, "M4 出口用例", NEW_BASE_URL, FAKE_MODEL_A,
                                       [FAKE_MODEL_A], api_key=FAKE_KEY))
        self.assertEqual(200, response.status_code, response.text)
        text = response.text
        self.assertNotIn(FAKE_KEY, text, "响应里出现了明文密钥")
        self.assertNotIn("api_key_preview", text,
                         "响应里不该带 api_key_preview（progress §十五 决策 2：前端不得依赖它）")
        body = response.json()
        for row in body.get("endpoints") or []:
            self.assertNotIn("api_key", row, "行里不该有 api_key")
            self.assertNotIn("api_key_preview", row)
            self.assertIn("has_api_key", row)
        self.assertTrue(body.get("ok") and body.get("id"))

    # --------------------------------------------------------- 路由契约（R8 / M4 出口）

    def test_save_route_signature_and_registry_contract(self):
        """M4 的路由契约：``POST /endpoints`` 带 ``profile`` 形参（R8），且没顶掉 M1/M3 的路由。"""
        import inspect

        signature = inspect.signature(plugin_api.save_endpoint)
        self.assertIn("profile", signature.parameters)
        self.assertIsNone(signature.parameters["profile"].default,
                          "profile 形参默认值必须是 None（无覆盖 = 进程级默认 profile）")
        # plugin_api 用了 `from __future__ import annotations`，所以形参注解是字符串：解析后再比
        hints = typing.get_type_hints(plugin_api.save_endpoint)
        self.assertIs(CustomEndpointUpdate, hints["body"],
                      "保存路由必须吃官方 CustomEndpointUpdate（web_models.py:36），不自造模型")
        self.assertEqual("/endpoints", plugin_api.SAVE_PATH)

        routes = {(tuple(sorted(route.methods)), route.path) for route in plugin_api.router.routes}
        for methods, path in [(("POST",), "/endpoints"), (("GET",), "/endpoints"),
                              (("POST",), "/endpoints/validate"),
                              (("POST",), "/endpoints/{endpoint_id}/pin")]:
            self.assertIn((methods, path), routes, f"路由表缺 {list(methods)} {path}")

    def test_save_does_not_mutate_request_body_flag(self):
        """直接调函数也要强制钉住，且**就地不改**请求对象（``model_copy`` 出副本）。"""
        body = CustomEndpointUpdate(id=NEW_ID, name="M4 直调用例", base_url=NEW_BASE_URL,
                                    model=FAKE_MODEL_A, models=[FAKE_MODEL_A],
                                    api_key=FAKE_KEY, discover_models=True)
        self._guard_before_write()
        plugin_api.save_endpoint(body, None)
        self.assertIs(True, body.discover_models, "原 body 被就地改了：应当用 model_copy 出副本")
        self.assertIs(False, (self._raw_entry(NEW_ID) or {}).get("discover_models"),
                      "直调路径没强制钉住（铁律 1 只写在 HTTP 层不算落地）")

    def test_sha_helper_is_only_used_for_opaque_comparison(self):
        """自证测试纪律：本文件对真实密钥只做哈希比对，从不比较明文。"""
        sample = "sk-something-not-from-this-machine"
        self.assertNotEqual(sample, _sha(sample))
        self.assertEqual(_sha(sample), _sha(sample))
        self.assertTrue(self.providers)
        for _pid, entry in self.providers.items():
            self.assertNotIn(FAKE_KEY_PREFIX, _sha(entry.get("api_key")),
                             "哈希值里不该出现本文件的假密钥前缀（说明取材取到了测试数据）")

    # ═══════════════ M9 新增用例（设计 §1.5 a–g；副本路由，跑完 `rm -rf` 副本）═══════════
    #
    # 前置状态一律由**合成条目** ``SYNTH_ID`` 造：先跑保存路由新建（磁盘无差集 ⇒ 官方支），
    # 需要「留存项带子字段」时再把副本 YAML 的 ``models:`` 换形状（只写副本，``tearDown``
    # 还原成 pristine 字节，破坏性写在用例内闭环）。期望数**运行时读盘后再算**。

    def _seed_synth(self, models: List[str], api_key: Optional[str] = None) -> List[str]:
        """造一条合成 ``providers:`` 条目（默认模型 = ``models[0]``），返回磁盘 id 序列。"""
        response = self._save(_payload(SYNTH_ID, SYNTH_NAME, NEW_BASE_URL, models[0],
                                       list(models), api_key=api_key, discover_models=False))
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(plugin_api.SAVE_CHANNEL_OFFICIAL, response.json().get("write_channel"),
                         "前置状态的新建保存走错了通道（新建无差集，应当是官方支）")
        seeded = self._synth_ids()
        self.assertEqual(len(models), len(seeded), "前置状态没成立：合成条目的白名单条数不对")
        return seeded

    def _synth_models(self) -> Dict[str, Any]:
        """磁盘上合成条目的 ``models:``（**运行时读**，dict 形状）。"""
        models = (self._raw_entry(SYNTH_ID) or {}).get("models")
        self.assertIsInstance(models, dict,
                              f"合成条目的 models: 应当是 dict，实到 {type(models).__name__}")
        return models

    def _synth_ids(self) -> List[str]:
        return list(self._synth_models())

    def _patch_synth_models(self, models_map: Dict[str, Any]) -> None:
        """把合成条目的 ``models:`` 换成**带子字段**的形状（只写副本；先过红线守卫）。"""
        self._guard_before_write()
        data = _load_copy_yaml()
        entry = (data.get("providers") or {}).get(SYNTH_ID)
        self.assertIsInstance(entry, dict, "前置状态没成立：合成条目没落在 providers:")
        entry["models"] = models_map
        _store_copy_yaml(data)

    def _save_page_view(self, page_models: List[str], default_model: str) -> Dict[str, Any]:
        """按「页面所见」提交保存（**不发** ``api_key`` 字段 = 铁律 2 的「保留」），返回回执。"""
        response = self._save(_payload(SYNTH_ID, SYNTH_NAME, NEW_BASE_URL, default_model,
                                       list(page_models), discover_models=False))
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    def _cc_sample(self) -> Tuple[str, Dict[str, Any]]:
        """M1 列表里第一条 ``cc:`` 行（前端没拦住时，后端收到的就是这种请求）。"""
        response = self.client.get(LIST_PATH)
        self.assertEqual(200, response.status_code, response.text)
        rows = [row for row in (response.json().get("endpoints") or [])
                if str(row.get("id") or "").startswith(plugin_api.CC_ID_PREFIX)]
        self.assertTrue(rows, "副本里没有由 cc-switch 管理的供应商 —— cc 闸门用例无从验证"
                             "（先按 progress §八 重建 harness 副本）")
        return str(rows[0]["id"]), rows[0]

    # ------------------------------------------------------------------ 用例 a（M9.13）

    def test_removal_of_a_middle_model_lands_on_disk_through_the_direct_channel(self):
        """设计 §1.5 a：移除**中间项** → 磁盘该 id 消失、其余**顺序不变**、子字段逐键相等。

        这就是 R2 的缺陷本体（官方 ``models:`` 只增不减），所以本例**必须**落进插件直写支：
        回执 ``write_channel == direct``，``removed_models`` = 运行时算出的差集条数。
        两个通道常量的**值**在这里一并冻结（命名对测试冻结，M10/M12 都吃这两串）。
        """
        self.assertEqual("official", plugin_api.SAVE_CHANNEL_OFFICIAL)
        self.assertEqual("direct", plugin_api.SAVE_CHANNEL_DIRECT)

        seeded = self._seed_synth(SYNTH_MODELS)
        with_meta: Dict[str, Any] = {}
        for index, model_id in enumerate(seeded):
            meta: Dict[str, Any] = {}
            if index == 0:
                meta["context_length"] = SYNTH_CONTEXT        # 默认模型带一项元数据
            elif index == 1:
                meta["name"] = f"{model_id} 的显示名"          # 另一份形状，证明不只保 ``{}``
            with_meta[model_id] = meta
        self._patch_synth_models(with_meta)

        before = copy.deepcopy(self._synth_models())
        ids_before = list(before)
        self.assertGreaterEqual(len(ids_before), 3, "前置状态不足三条，「中间项」没有意义")
        removed_id = ids_before[len(ids_before) // 2]
        page = [model_id for model_id in ids_before if model_id != removed_id]
        env_before = _read_copy_bytes(".env")

        with mock.patch.object(plugin_api, "save_config",
                               wraps=plugin_api.save_config) as counter:
            receipt = self._save_page_view(page, ids_before[0])

        self.assertEqual(plugin_api.SAVE_CHANNEL_DIRECT, receipt.get("write_channel"),
                         "有删除的保存没走插件直写支（官方只增不减，删不掉）")
        self.assertEqual(len(set(ids_before) - set(page)), receipt.get("removed_models"),
                         "回执删除数不等于运行时算出的差集条数（拍板 #12 的数字必须真实）")
        self.assertEqual(1, counter.call_count,
                         "有删除的保存不是单次原子写（>1 次 save_config 就有「删一半」的窗口）")

        after = self._synth_models()
        self.assertNotIn(removed_id, after, "被移除的模型仍在磁盘上（R2 缺陷本体没修掉）")
        self.assertEqual(page, list(after), f"落盘顺序变了（实到 {list(after)}，应为 {page}）")
        for model_id in page:
            self.assertEqual(before[model_id], after.get(model_id),
                             f"留存项 {model_id} 的子字段没逐键保留（官方 dict(current) 那一半）")
        self.assertEqual(env_before, _read_copy_bytes(".env"), "不发 api_key 的移除保存动了 .env")
        # 写后重跑 M1 只读列表（proposal §3.4-2 里能自动化的那半句）
        self.assertNotIn(removed_id, self._row_of(SYNTH_ID)["allowlist_models"],
                         "列表行仍带回被移除的模型 —— 磁盘真值与页面分叉了")

    # ------------------------------------------------------------------ 用例 b（M9.14）

    def test_removing_the_default_model_promotes_the_new_first_one_and_leaves_the_mirror(self):
        """设计 §1.5 b：移除默认项（前端 ``formWithRemovedModel`` 已把新首项升为默认）。

        提交形状由前端保证（``model`` = 页面名单第一个），后端只管差集：
        旧默认消失、新默认**在池里**且条目 ``model:`` = 新默认；
        **没勾 make_default 时顶层 ``model:`` 零改动**（当前供应商绝不被保存顺手切换）。
        """
        seeded = self._seed_synth(SYNTH_MODELS)
        old_default, new_default = seeded[0], seeded[1]
        page = [model_id for model_id in seeded if model_id != old_default]
        main_before = copy.deepcopy(read_raw_config().get("model") or {})
        self.assertNotEqual(plugin_api._custom_endpoint_id(SYNTH_ID),
                            str(main_before.get("provider") or "").strip().lower(),
                            "前置状态：合成条目正是当前供应商，顶层镜像的判据会失真")

        receipt = self._save_page_view(page, new_default)
        self.assertEqual(plugin_api.SAVE_CHANNEL_DIRECT, receipt.get("write_channel"))
        self.assertEqual(len(set(seeded) - set(page)), receipt.get("removed_models"))

        entry = self._raw_entry(SYNTH_ID)
        self.assertEqual(new_default, str(entry.get("model") or ""),
                         "条目的默认模型没换成新首项（proposal §3.2-4 的约定）")
        after = self._synth_models()
        self.assertNotIn(old_default, after, "被移除的旧默认仍在池里")
        self.assertIn(new_default, after, "新默认不在池里（不变式 3：默认模型永远在池里）")
        self.assertEqual(page, list(after), f"升默认后的落盘顺序变了（实到 {list(after)}）")

        main_after = copy.deepcopy(read_raw_config().get("model") or {})
        self.assertEqual([], _changed_paths(main_before, main_after),
                         "没勾 make_default 却改了顶层 model:（当前供应商被顺手切换）")

    # ------------------------------------------------------------------ 用例 c（M9.15 两支）

    def test_reducing_the_allowlist_to_only_the_default_model_lands_as_only_the_default(self):
        """设计 §1.5 c 第一支：页面只留 [默认] → 落盘只剩默认那一条，通道 ``direct``。

        与既有「只剩默认模型」口径**逐字一致**（``models:`` 不是空 dict —— 默认豁免）。
        """
        seeded = self._seed_synth(SYNTH_MODELS)
        default = seeded[0]
        page = [default]
        expected_removed = len([m for m in seeded if m not in set(page)])
        self.assertGreater(expected_removed, 0, "前置状态：磁盘除默认模型外没有别的 id")

        receipt = self._save_page_view(page, default)
        self.assertEqual(plugin_api.SAVE_CHANNEL_DIRECT, receipt.get("write_channel"))
        self.assertEqual(expected_removed, receipt.get("removed_models"))
        self.assertEqual([default], self._synth_ids(),
                         "移除到只剩默认之后落盘却不是只剩默认（不变式 3 / E7 口径）")

        row = self._row_of(SYNTH_ID)
        self.assertEqual([default], row["allowlist_models"])
        self.assertEqual(1, row["allowlist_count"])

    def test_emptying_the_page_still_keeps_the_default_and_reports_the_non_default_diff(self):
        """设计 §1.5 c 第二支：页面留空 ``[]``（提交 ``model`` = 默认）→ keep 集**并入默认模型**。

        差集虽覆盖磁盘全部非默认 id，默认仍豁免 ⇒ 落盘同样只剩默认那一条，
        ``removed_models`` = 磁盘非默认条数（运行时读）。这正是「清空 ≠ 池里空」的既有口径。
        """
        seeded = self._seed_synth(SYNTH_MODELS)
        default = seeded[0]
        non_default = len([m for m in seeded if m != default])
        self.assertGreater(non_default, 0, "前置状态：磁盘没有非默认 id，本例的差分没有意义")

        receipt = self._save_page_view([], default)
        self.assertEqual(plugin_api.SAVE_CHANNEL_DIRECT, receipt.get("write_channel"),
                         "页面留空也要删东西（差集非空），不该走官方支")
        self.assertEqual(non_default, receipt.get("removed_models"),
                         "keep 集没并入默认模型，或删除数报得不实")
        self.assertEqual({default: {}}, self._synth_models(),
                         "「已添加栏为空」的保存落盘不是「只剩默认模型」那一条")

    # ------------------------------------------------------------------ 用例 d（M9.16）

    def test_saving_a_cc_switch_managed_supplier_is_refused_and_writes_zero_bytes(self):
        """设计 §1.5 d + §1.2.2 要点 5：``cc:`` 前缀 id 保存 → **400**，且**零写入**。

        前端 ``SAVE_DISABLED_LEGACY`` 已拦一层，本例证明**后端不信前端**（与
        :func:`clear_models_endpoint` 同口径）。闸门同时封掉现状那个真实口子：官方
        ``_custom_endpoint_id("cc:<name>")`` 会 slug 化成 ``cc-<name>`` 并**新建**一条
        ``providers:`` 影子条目 —— 被拒后键集合必须一模一样。
        """
        identity, row = self._cc_sample()
        bare = identity[len(plugin_api.CC_ID_PREFIX):].strip()
        config_before = _read_copy_bytes("config.yaml")
        env_before = _read_copy_bytes(".env")
        keys_before = sorted(_raw_providers(read_raw_config()))
        legacy_before = copy.deepcopy(read_raw_config().get("custom_providers") or [])
        shadow_id = plugin_api._custom_endpoint_id(identity)

        response = self._save(_payload(identity, str(row.get("name") or bare),
                                       str(row.get("base_url") or ""), str(row.get("model") or ""),
                                       _form_added_seed(row), discover_models=False))
        self.assertEqual(400, response.status_code, response.text)
        detail = str((response.json() or {}).get("detail") or "")

        # 四要素（M10 §2.4）：动作对象（条目名）+ 拒绝原因 + 正路指引 +「列表未改动」承诺
        self.assertIn(bare, detail, f"detail 没带上被拒的条目名 {bare!r}")
        self.assertIn("由 cc-switch 管理", detail, "detail 没说清这供应商归 cc-switch 所有")
        self.assertIn("保存", detail, "detail 的动词不是「保存」")
        self.assertTrue("收编进本插件" in detail or "回 cc-switch" in detail,
                        "拒绝必须给正路（收编 / 回 cc-switch），不能只说不行")
        self.assertIn("列表未改动", detail, "detail 没保证零写入")
        cjk = len(re.findall(r"[一-鿿]", detail))
        latin = len(re.findall(r"[A-Za-z]", detail))
        self.assertGreater(cjk, latin // 2, f"detail 不像中文说明（中文 {cjk} 字 / 拉丁 {latin} 字母）")
        # M10 §2.2 禁词表：M9 的新串第一天就按使用者语言写（设计 §0.3：M10 不回改）
        for jargon in ("providers:", "models:", "custom_providers:", "discover_models", "key_env",
                       "标准形态", "决议", "UC-", "§", "cc-switch 条目", "config_env.py"):
            self.assertNotIn(jargon, detail, f"保存闸门的 detail 带了黑话或编号：{jargon}")

        # 零写入：两个文件一字节都不动；providers: 键集合不变（没有影子条目）；legacy 段照旧
        self.assertEqual(config_before, _read_copy_bytes("config.yaml"), "被拒的保存写了 config")
        self.assertEqual(env_before, _read_copy_bytes(".env"), "被拒的保存写了 .env")
        keys_after = sorted(_raw_providers(read_raw_config()))
        self.assertEqual(keys_before, keys_after, "被拒的保存改动了 providers: 的键集合（静默新建）")
        self.assertNotIn(shadow_id, keys_after, f"影子条目 {shadow_id} 被建出来了")
        self.assertEqual(legacy_before, read_raw_config().get("custom_providers"),
                         "被拒的保存动了 custom_providers:")

    # ------------------------------------------------------------------ 用例 e（M9.17）

    def test_multi_model_removal_receipt_counts_exactly_the_popped_ids(self):
        """设计 §1.5 e：一次保存删**多条** → ``removed_models`` == 实际被 pop 的 id 数。"""
        seeded = self._seed_synth(SYNTH_MODELS)
        default = seeded[0]
        page = seeded[:2]                                   # 留前两条，其余一次删掉
        expected = [model_id for model_id in seeded if model_id not in set(page)]
        self.assertGreaterEqual(len(expected), 2, "前置状态：只删一条时本例与用例 a 没有差别")

        receipt = self._save_page_view(page, default)
        self.assertEqual(plugin_api.SAVE_CHANNEL_DIRECT, receipt.get("write_channel"))
        self.assertEqual(len(expected), receipt.get("removed_models"))
        self.assertEqual(len(expected), len(set(seeded) - set(self._synth_ids())),
                         "回执数与实际消失的 id 数不一致")
        self.assertEqual(sorted(page), sorted(self._synth_ids()),
                         "该留的没留下，或不该留的还在")
        self.assertEqual(len(page), self._row_of(SYNTH_ID)["allowlist_count"],
                         "派生字段与磁盘脱节（回执 N 就不是「本次删除 N 个模型」的可靠数字）")

    # ------------------------------------------------------------------ 用例 f（M9.18）

    def test_removal_save_never_touches_the_stored_key_or_the_env_file(self):
        """设计 §1.5 f：密钥零接触 —— 有删除的保存前后 ``key_env`` **逐字不变**、``.env`` **字节不变**。

        走的是铁律 2 的「字段缺失 = 保留」那一支：官方只在条目自己带明文 ``api_key`` 时
        才动 ``.env``，而合成条目从落盘起就只有 ``key_env`` 引用。
        ⚠️ 本例比的是**变量名**与**文件字节**，密钥值只用于「有没有出现在回执里」这一条
        反断言（与 :meth:`test_save_response_carries_no_key_material` 同一判据形状）。
        """
        seeded = self._seed_synth(SYNTH_MODELS, api_key=FAKE_KEY)
        before_entry = copy.deepcopy(self._raw_entry(SYNTH_ID) or {})
        env_name = str(before_entry.get("key_env") or "").strip()
        self.assertEqual(custom_endpoint_key_env(SYNTH_ID), env_name,
                         "前置状态没成立：合成条目没带上 key_env 引用")
        default = seeded[0]
        page = seeded[:len(seeded) - 1]
        env_before = _read_copy_bytes(".env")

        receipt = self._save_page_view(page, default)
        self.assertEqual(plugin_api.SAVE_CHANNEL_DIRECT, receipt.get("write_channel"))

        after_entry = self._raw_entry(SYNTH_ID) or {}
        self.assertEqual(env_name, str(after_entry.get("key_env") or ""),
                         "移除保存把 key_env 改掉了（直写支吃的是同一份官方实现，不该碰密钥）")
        self.assertNotIn("api_key", after_entry, "移除保存把明文密钥写进了 config.yaml")
        self.assertEqual(env_before, _read_copy_bytes(".env"), "移除保存重写了 .env 的字节")
        self.assertNotIn(FAKE_KEY, str(receipt), "回执里出现了密钥内容（M1.5 的密钥铁律）")

    # ------------------------------------------------------------------ 用例 g（M9.19）

    def test_removal_save_is_surgical_and_leaves_every_other_entry_untouched(self):
        """设计 §1.5 g：整份 config 的**值级 diff** 只落在目标条目（``test_activate_delete`` 的形状）。

        判据按值级而非「逐行 diff 只有一行」（批 2 决策 3）—— ``save_config`` 本来就会重排
        / 规整排版。``auxiliary.*``（副本里那份槽真带着密钥）与 ``custom_providers:``
        尤其不许被牵连（proposal §3.4-6「其余供应商卡片无意外变动」的自动化那半）。
        """
        seeded = self._seed_synth(SYNTH_MODELS)
        default = seeded[0]
        page = seeded[:len(seeded) - 1]                     # 删掉最后一条
        before = copy.deepcopy(read_raw_config())

        receipt = self._save_page_view(page, default)
        self.assertEqual(plugin_api.SAVE_CHANNEL_DIRECT, receipt.get("write_channel"))
        after = copy.deepcopy(read_raw_config())

        changes = _changed_paths(before, after)
        outside = [path for path in changes if not path.startswith(f"providers.{SYNTH_ID}")]
        self.assertEqual([], outside, f"移除保存动了预期之外的路径（值级）：{outside}")
        self.assertTrue(any(path.startswith(f"providers.{SYNTH_ID}") for path in changes),
                        "目标条目却没变化 ⇒ 本例的差集前提不成立")
        self.assertEqual(_sha(repr(before.get("auxiliary"))), _sha(repr(after.get("auxiliary"))),
                         "auxiliary.* 被牵连（副本那份槽里真带着密钥）")
        self.assertEqual(len(before.get("custom_providers") or []),
                         len(after.get("custom_providers") or []),
                         "custom_providers: 的条目数变了")
        self.assertEqual(sorted(key for key in (before.get("providers") or {}) if key != SYNTH_ID),
                         sorted(key for key in (after.get("providers") or {}) if key != SYNTH_ID),
                         "providers: 的键集合变了（既不该新建也不该删掉别的条目）")

    # ------------------------------------------------ 前端契约静态钉（M9.7 / M9.8 / M9.9）

    def test_plugin_js_save_toast_consumes_the_backend_receipt(self):
        """静态钉 ``plugin.js`` 保存回执的消费口径（M9.7–M9.9；GUI 实测归用户，见停止点 D）。

        * 两个新常量的**值**按设计 §1.3 逐字冻结（拼起来 = 「已保存，本次删除 N 个模型」）；
        * ``saveMutation.onSuccess`` 只剩 ``haptic('success')``，成功 toast 改在 ``runSave`` 里
          **吃后端回执** ``removed_models``（缺字段 = 0 兜底旧后端形状；前端不自己数）；
        * N=0 那支仍是光秃秃的 ``SAVED_TOAST_MESSAGE``（拍板 #12：纯新增不含半句）；
        * ``write_channel`` 是机器可读判据，**不进任何 UI 文案**（``runSave`` 里不许读它）。
        """
        source = (PRODUCT_ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")

        def slice_of(text: str, start: str, end: str) -> str:
            begin = text.index(start)
            return text[begin:text.index(end, begin)]

        self.assertIn("const SAVED_TOAST_REMOVED_INFIX = '，本次删除 '", source,
                      "删除回执那半句不再与设计 §1.3 逐字一致")
        self.assertIn("const SAVED_TOAST_REMOVED_SUFFIX = ' 个模型'", source,
                      "删除回执后半句不再与设计 §1.3 逐字一致")

        save_block = slice_of(source, "const saveMutation = useMutation({",
                              "const pinMutation = useMutation({")
        self.assertIn("haptic('success')", save_block, "onSuccess 丢了触觉反馈")
        self.assertNotIn("notifyWriteFeedback('success'", save_block,
                         "成功 toast 还留在 onSuccess —— 那里拿不到回执的 removed_models")
        self.assertIn("refreshEndpoints()", save_block,
                      "写后失效（§6.4）不该随 toast 挪位置一起丢")

        run_save = slice_of(source, "const runSave = async (editor, rows) => {", "    batchOpen,")
        self.assertIn("const result = await saveMutation.mutateAsync(payload)", run_save,
                      "runSave 不再消费 mutateAsync 的结果（回执 N 无从取数）")
        self.assertIn("Number(result && result.removed_models) || 0", run_save,
                      "删除数没按「缺字段 = 0」兜底旧后端形状")
        self.assertIn("${SAVED_TOAST_MESSAGE}${SAVED_TOAST_REMOVED_INFIX}${removed}"
                      "${SAVED_TOAST_REMOVED_SUFFIX}", run_save,
                      "removed > 0 那支没拼成「已保存，本次删除 N 个模型」")
        self.assertIn(": SAVED_TOAST_MESSAGE,", run_save,
                      "removed == 0 那支不该带后半句（拍板 #12）")
        self.assertNotIn("write_channel", run_save,
                         "write_channel 是机器可读判据，不该进任何 UI 文案组装")
        self.assertNotIn(".filter(", run_save,
                         "前端在 runSave 里自己数模型（N 只能来自后端回执，M9.9）")
