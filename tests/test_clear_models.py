"""M5 验收：清空白名单 ``POST /endpoints/{id}/clear-models``（M5.6 / M5.7 / M5.8；T19 / T24 的自动化半边）。

跑法（``tasks/progress.md`` §五 / ``prompt.md`` §5.1，填好占位后照抄；venv 里没有 pytest 也不许装）：

    cd "<本仓根目录>"                        # 必须 cd 到含 tests/ 的目录，discover 依赖 cwd
    V="<装有 hermes_cli 的 venv 里的 python>"
    T="<临时目录>/supplier-models-harness"    # 副本务必建在仓外，本仓 .gitignore 已排除 config/.env
    rm -rf "$T" && mkdir -p "$T"
    cp "<副本源 config.yaml>" "$T/config.yaml"   # 副本源须仍留有 legacy custom_providers: 条目
    cp "<副本源 .env>"        "$T/.env"
    HERMES_HOME="$(cygpath -w "$T")" "$V" -m unittest discover -s tests -v
    rm -rf "$T"

判据口径（⚠️ 批 2 决策 3）：**值级比对**（解析后的 YAML / ``_changed_paths``），
不用「逐行 diff 只有 1 行」当无损判据 —— 官方 ``save_config`` 首次写入会在文件末尾追加
约 38 行**纯注释**示例块（``config.py:2301`` 的 ``_commented_sections_for_save``），
值层面无影响、幂等，但逐行断言在首次写入时必假红。本文件因此两道一起上：

* **值级**：把 ``providers.<id>.models`` 整键摘掉后，前后两份 config 必须**完全相等**
  （``_changed_paths`` 为空）—— 这就是 T24 要的「只改了 models 一处」。
* **内容行级**：把该条目的 ``models:`` 子块（两种形态都摘）从前后文本里剔掉后，
  剩余**内容行逐行相同** —— 值级比对看不见的「键被重排」由这一道兜住。
  注释行与空行一律不参与比对（理由见上）。

三条红线与 M4 一致：写之前先过 ``_guard_before_write()``（只允许写 ``HERMES_HOME``
临时副本）、真实密钥只做哈希比对、零网络（清空是纯 config 操作，用 mock 把探测函数
换成「一调就炸」来证明）。计数一律「先从副本读、再断言」（prompt §2.7；
``2026-09-22`` 快照 ``token_rhythm`` 22 条只当注释，不当期望值）。
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

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from hermes_cli.config import (
    get_config_path,
    get_env_path,
    invalidate_env_cache,
    load_config,
    read_raw_config,
)
from hermes_cli.web_server_profiles import _config_profile_scope

PRODUCT_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = PRODUCT_ROOT / "dashboard"
DESKTOP_DIR = PRODUCT_ROOT / "desktop"
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

import plugin_api  # noqa: E402  （prompt §5.2：先把 dashboard 塞进 sys.path 再 import）

MOUNT_PREFIX = "/api/plugins/supplier-models"
LIST_PATH = f"{MOUNT_PREFIX}/endpoints"
CLEAR_PATH_TEMPLATE = f"{MOUNT_PREFIX}/endpoints/{{endpoint_id}}/clear-models"
# 密钥铁律（与 M1 的 KEY_MATERIAL_KEYS 同口径）：响应里不许出现任何密钥材料
FORBIDDEN_RESPONSE_KEYS = ("api_key", "api_key_preview")
MISSING_CLEAR_ID = "zz-m5-clear-no-such-entry"
# 任务书点名的 T24 样本（本机 2026-09-22 快照：22 条被灌进来的白名单 + discover_models: true）
# ⚠️ 只是**优先**取材对象，不是硬编码期望值：拿不到就退到「任一 providers: 条目」（prompt §2.7）
PREFERRED_SAMPLE_ID = "token_rhythm"
# 清空动作**不许**碰的字段（M5.2 决议 13 派生口径），逐字段比对
PROTECTED_ENTRY_FIELDS = (
    "model", "default_model", "discover_models", "key_env", "api_key",
    "name", "base_url", "url", "api_mode", "context_length", "api_key_env", "key_cmd",
)
# 语义铁律 3（E7 / §7.7 不变式 3 的 v0.4 改写）里**永远不许出现**的说法。
# ⚠️ 拆成两段拼起来：这样「全文 grep 该字面量 = 0 命中」这条静态断言连本测试文件也一起过，
# 而 lint 判据本身不变（拼出来的仍是那句原文）。
FORBIDDEN_POOL_COPY = "不提供" + "任何模型"
# 二次确认弹窗的 `open` 在 M5 / M6 区块里的取法（`:2967` / `:3399`）。
# 用作「hook 必须导出 confirmOpen state」那条护栏的**前提标记**（缺陷修复 round 1）。
CONFIRM_DIALOG_OPEN_EXPR = "open: actions.confirmOpen"


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


def _provider_entry(raw: Dict[str, Any], endpoint_id: str) -> Dict[str, Any]:
    """按**官方同一条**定位链（``find_provider_entry``）取条目，返回副本用于比对。"""
    _stored, entry = plugin_api.find_provider_entry((raw or {}).get("providers"), endpoint_id)
    return copy.deepcopy(entry) if isinstance(entry, dict) else {}


def _entry_models(raw: Dict[str, Any], endpoint_id: str) -> Any:
    return _provider_entry(raw, endpoint_id).get("models")


def _disk_model_ids(entry: Any) -> List[str]:
    """条目 ``models:`` 的**磁盘** id 列表（dict 数键、list 数元素、其它形状 → ``[]``）。

    故意在测试里另写一遍口径、**不**调后端的 ``_entry_model_ids``：期望值与实装值必须来自
    两处代码，否则 ``allowlist_count`` 的断言永远为真、抓不到实现漂移（prompt §2.7）。
    """
    models = entry.get("models") if isinstance(entry, dict) else None
    if isinstance(models, dict):
        return [str(key).strip() for key in models if str(key).strip()]
    if isinstance(models, (list, tuple)):
        return [str(item).strip() for item in models if str(item).strip()]
    return []


def _disk_models_count(raw: Dict[str, Any], endpoint_id: str) -> int:
    """副本里该 ``providers:`` 条目的磁盘白名单条数。"""
    return len(_disk_model_ids(_provider_entry(raw, endpoint_id)))


def _legacy_disk_models_count(raw: Dict[str, Any], name: str) -> int:
    """副本里该 ``custom_providers:`` 条目（cc-switch 写的）的磁盘白名单条数。"""
    entry = next((item for item in _raw_legacy(raw)
                  if str(item.get("name") or "").strip() == name), None)
    return len(_disk_model_ids(entry))


def _injected_view_len(disk_ids: List[str], default_model: str) -> int:
    """官方 ``_models_from_custom_endpoint_entry``（``config_env.py:317-328``）注入默认模型后的**视图长度**（保序去重）。"""
    ordered = ([default_model] + list(disk_ids)) if default_model else list(disk_ids)
    return len({item for item in ordered if item})


def _form_added_seed(row: Dict[str, Any]) -> List[str]:
    """``plugin.js`` 的 `formFromRow` 里「已添加」栏种子的 **Python 镜像**（同一处判据）。

    规则逐字对齐 JS：`allowlist_models` 是数组就用它（磁盘真值），否则退回 `models`；
    去空去重保序；只在列表**非空**时把行上的 `model:` 并进第 0 位（§7.7 不变式只约束
    「非空 ⇒ model ∈ models」，空栏本身合法 —— 清空后的条目就该回填成空栏）。

    没有浏览器可跑（prompt §5.4：M5 不做渲染证据），所以「编辑器不会把默认模型当成用户
    已添加的那一条」这句话在**后端层**由本函数钉住，另由
    :meth:`ClearModelsRouteTest.test_plugin_js_copy_and_gate_lint` 的静态 lint 盯住
    「`row.allowlist_models` 在 `plugin.js` 里只被读一处」。改 JS 那句时必须同步改这里。
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


def _excise_models(raw: Dict[str, Any], endpoint_id: str) -> Dict[str, Any]:
    """深拷贝整份 config 后**只摘掉** ``providers.<id>.models`` 一个键（值级判据用）。

    剩下的一切（含同条目的其它字段、其它条目、顶层 ``model:``、``custom_providers:``、
    ``auxiliary.*``…）都必须与清空前逐值相等。
    """
    cut = copy.deepcopy(raw)
    _stored, entry = plugin_api.find_provider_entry((cut or {}).get("providers"), endpoint_id)
    if isinstance(entry, dict):
        entry.pop("models", None)
    return cut


def _sha(value: Any) -> str:
    marker = "\x00<absent>" if value is None else value
    return hashlib.sha256(str(marker).encode("utf-8")).hexdigest()


def _changed_paths(before: Any, after: Any, path: str = "") -> List[str]:
    """两份解析后的 config 的**值级**差异路径（只报「路径 + 变化种类」，**绝不回显值**——
    副本里带着明文密钥，断言消息会进测试输出）。种类：``+added`` / ``-removed`` / ``!changed``。
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


def _content_lines(lines: List[str]) -> List[str]:
    """滤掉空行与纯注释行，只留 YAML 内容行（``strip`` 后）。

    注释行不参与逐行比对的理由见模块 docstring（批 2 决策 3：``save_config`` 首次写入
    会追加纯注释示例块）。
    """
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


def _lines_outside_models_block(text: str, endpoint_id: str) -> List[str]:
    """把 ``providers:`` 下该条目的 ``models:`` **整块**（含 ``models:`` 那一行）剔掉。

    两种形态都要摘：清空前是 ``models:`` + 若干更深缩进的子行；清空后缩成一行
    ``models: {}``。摘完之后剩下的内容行必须逐行相同 —— 这是「只改了 models 一处」的
    文本级证据（值级比对看不见键序变化）。
    """
    lines = text.splitlines()
    entry_re = re.compile(rf"^\s+{re.escape(endpoint_id)}:\s*$")
    kept: List[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if entry_re.match(line):
            entry_indent = len(line) - len(line.lstrip())
            models_indent = entry_indent + 2
            index += 1
            while index < len(lines):
                child = lines[index]
                child_stripped = child.strip()
                child_indent = len(child) - len(child.lstrip())
                real_content = bool(child_stripped) and not child_stripped.startswith("#")
                if real_content and child_indent <= entry_indent:
                    break                                   # 条目到此结束
                is_models_header = real_content and child_indent == models_indent and (
                    child_stripped == "models:" or child_stripped.startswith("models: "))
                if is_models_header:
                    index += 1
                    while index < len(lines):
                        grand = lines[index]
                        grand_stripped = grand.strip()
                        grand_indent = len(grand) - len(grand.lstrip())
                        if not grand_stripped or grand_stripped.startswith("#"):
                            index += 1                      # 空行/注释行跟着一起吞
                            continue
                        if grand_indent > models_indent:
                            index += 1                      # models 的子项
                            continue
                        break
                    continue
                kept.append(child)
                index += 1
            continue
        kept.append(line)
        index += 1
    return kept


class ClearModelsRouteTest(unittest.TestCase):
    """M5.1 / M5.2 / M5.3 / M5.6 / M5.7 / M5.8：直接写路径的清空 + 三条拒绝面。"""

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
        """红线（prompt §5.2）：任何写入之前先确认落在 ``HERMES_HOME`` 临时副本上。"""
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
                        "清空的开放对象与拒绝面无从验证，先按 progress §五 重建 harness 副本")

    # ------------------------------------------------------------------ 取材

    def _clear_sample(self) -> str:
        """一个「``providers:`` 里 + 白名单非空 + 有默认模型」的条目 id（T24 样本）。

        优先任务书点名的 ``token_rhythm``（本机快照 22 条被灌进来的模型），拿不到就退到
        「任一满足条件的 ``providers:`` 条目」—— **条数一律运行时读**（prompt §2.7）。
        """
        def usable(entry: Any) -> bool:
            return (isinstance(entry, dict)
                    and isinstance(entry.get("models"), dict)
                    and bool(entry.get("models"))
                    and bool(str(entry.get("model") or "").strip()))

        candidates = [(pid, entry) for pid, entry in sorted(self.providers.items(),
                                                            key=lambda item: str(item[0]))
                      if isinstance(entry, dict) and usable(entry)]
        self.assertTrue(candidates,
                        "副本里没有「providers: + 白名单非空 + 有默认模型」的条目，"
                        "T24 的 models 判据无从验证")
        preferred = [pid for pid, _entry in candidates if pid == PREFERRED_SAMPLE_ID]
        return preferred[0] if preferred else candidates[0][0]

    def _legacy_sample(self) -> str:
        """一个 cc-switch 条目名（M5.7 的拒绝面对象；本机快照是 ``sensenova`` / ``bailian``）。"""
        names = [str(entry.get("name") or "").strip() for entry in self.legacy]
        named = [name for name in names if name]
        self.assertTrue(named, "副本里没有带 name 的 custom_providers: 条目，M5.7 无从验证")
        return named[0]

    def _rows(self) -> List[Dict[str, Any]]:
        response = self.client.get(LIST_PATH)
        self.assertEqual(200, response.status_code, response.text)
        return response.json().get("endpoints") or []

    def _row_of(self, endpoint_id: str) -> Optional[Dict[str, Any]]:
        rows = [row for row in self._rows() if str(row.get("id") or "") == endpoint_id]
        self.assertLessEqual(len(rows), 1, f"GET /endpoints 里 {endpoint_id} 出现多行")
        return rows[0] if rows else None

    def _clear(self, endpoint_id: str):
        """``POST /endpoints/{id}/clear-models``（**无请求体**）；顺手证明这条链路零网络。"""
        self._guard_before_write()
        url = CLEAR_PATH_TEMPLATE.format(endpoint_id=endpoint_id)
        with mock.patch.object(plugin_api, "validate_custom_endpoint",
                               side_effect=AssertionError("清空链路不许发探测请求（纯 config 操作）")):
            return self.client.post(url)

    # ------------------------------------------- M5.6（T24 自动化半边）：只写 models 一个字段

    def test_clear_providers_entry_empties_models_and_keeps_every_other_field(self):
        """M5.6 / T24：清空 ``token_rhythm`` 后 ``entry["models"] == {}``；``model:`` /
        ``discover_models`` / ``key_env`` 与写入前**逐字段相等**；值级整份 config diff
        **只有 models 一处**变化。
        """
        endpoint_id = self._clear_sample()
        raw_before = read_raw_config()
        before = _provider_entry(raw_before, endpoint_id)
        models_before = copy.deepcopy(before.get("models"))
        self.assertTrue(models_before, "样本白名单为空，清空动作无从验证")
        models_count = len(models_before)
        text_before = _read_copy_bytes("config.yaml").decode("utf-8")
        env_before = _read_copy_bytes(".env")

        response = self._clear(endpoint_id)
        self.assertEqual(200, response.status_code, response.text)
        payload = response.json()
        self.assertTrue(payload.get("ok"), payload)
        self.assertEqual(endpoint_id, payload.get("id"))
        self.assertEqual(models_count, payload.get("cleared_models"),
                         "响应回执的清除条数与运行时读到的条数不一致")
        self.assertEqual(str(before.get("model") or ""), payload.get("default_model"),
                         "响应里的默认模型不是条目自己的 model:（E7 的判据）")
        self.assertIn("endpoints", payload)
        self.assertIn("current", payload)

        raw_after = read_raw_config()
        after = _provider_entry(raw_after, endpoint_id)
        # ① 期望结果本身
        self.assertEqual({}, after.get("models"), "models 没被清成空 dict（T24）")
        self.assertIsInstance(after.get("models"), dict, "models 必须是 dict，不是被整个删掉")
        # ② 写入范围：只写 models:（M5.2），逐字段比对
        for field in PROTECTED_ENTRY_FIELDS:
            self.assertEqual(before.get(field), after.get(field),
                             f"清空改到了 {field}（决议 13：只许写 models:）")
        self.assertEqual(_sha(before.get("api_key")), _sha(after.get("api_key")),
                         "api_key 的 sha256 变了（清空不许碰密钥）")
        # ③ 值级无损：摘掉 models 之后整份 config 逐值相等
        self.assertEqual([], _changed_paths(_excise_models(raw_before, endpoint_id),
                                            _excise_models(raw_after, endpoint_id)),
                         "除 models 之外还有别的字段变了（T24 的无损判据）")
        # ④ 文本级：摘掉该条目的 models 子块后剩余内容行逐行相同（值级看不见键序）
        text_after = _read_copy_bytes("config.yaml").decode("utf-8")
        self.assertEqual(_content_lines(_lines_outside_models_block(text_before, endpoint_id)),
                         _content_lines(_lines_outside_models_block(text_after, endpoint_id)),
                         "models 子块之外的内容行变了（键序 / 缩进 / 其它条目被动过）")
        # ⑤ .env 一字节都不动（清空是纯 config 操作）
        self.assertEqual(env_before, _read_copy_bytes(".env"), "清空不该写 .env")
        # ⑥ 前端消费的行：⚠️ 实测事实（写进决策日志）——官方读路径
        # `_models_from_custom_endpoint_entry`（`config_env.py:317-328`）会把条目的 `model:`
        # **无条件 insert 到 models 列表第 0 位**（与 `_absorb_entry_models` 同一族行为），
        # 所以行里的 `models` 是「白名单 ∪ {默认模型}」：清空后磁盘是 `{}`，**行里仍是那 1 条默认模型**。
        # 这条语义**已被编排裁定为冻结项**（只加字段、不改既有字段），所以卡片要显「已添加 0 个模型」
        # 吃的是新增的磁盘真值 `allowlist_count`（差分证据见
        # :meth:`test_cleared_row_reports_allowlist_count_zero_while_view_keeps_default`）；
        # 本插件的写回执 `cleared_models` 仍是「这一次清掉了 N 条」的直接依据。
        row = self._row_of(endpoint_id)
        self.assertIsNotNone(row, "清空后条目从 GET /endpoints 里消失了")
        self.assertEqual([str(before.get("model") or "")], row.get("models") or [],
                         "清空后行里的白名单视图不是「只剩默认模型」那一个（读路径注入了 model:）")
        self.assertEqual(str(before.get("model") or ""), str(row.get("model") or ""))
        self.assertEqual("providers", row.get("source"), "行上的来源标记变了（前端按钮的闸门依据）")
        self.assertIs(bool(before.get("discover_models")), bool(row.get("discover_models")),
                      "清空改动了钉住状态的行视图")

    def test_cleared_row_reports_allowlist_count_zero_while_view_keeps_default(self):
        """契约补丁的**差分证据**：同一行上 ``allowlist_count == 0`` 而 ``models`` 仍有注入的 1 条。

        M5 上报、编排裁定「只加字段」的那个缺口就在这里闭合：``models`` 是官方注入后的
        **视图**（清成 ``{}`` 后仍剩默认模型），``allowlist_count`` 才是**磁盘真值**，
        卡片的「已添加 0 个模型」由此算得出来。期望条数一律运行时从副本推（prompt §2.7）。
        本用例只读 ``GET /endpoints`` 与清空的写响应，写盘动作仍受 ``_guard_before_write`` 管辖。
        """
        endpoint_id = self._clear_sample()
        raw_before = read_raw_config()
        entry_before = _provider_entry(raw_before, endpoint_id)
        default_model = str(entry_before.get("model") or "")
        disk_ids_before = _disk_model_ids(entry_before)
        disk_before = len(disk_ids_before)
        self.assertTrue(disk_before, "样本白名单为空，差分判据无从验证")

        before = self._row_of(endpoint_id)
        self.assertIsNotNone(before, "前置：providers: 条目在 GET /endpoints 里没有行")
        self.assertIn("allowlist_count", before, f"清空前该行缺 allowlist_count：{sorted(before)}")
        self.assertEqual(disk_before, before["allowlist_count"],
                         "清空前的 allowlist_count 不等于副本磁盘的 models 条数")
        self.assertEqual(_injected_view_len(disk_ids_before, default_model), len(before["models"]),
                         "清空前的注入视图长度对不上（models 语义已改，补丁不该碰它）")

        response = self._clear(endpoint_id)
        self.assertEqual(200, response.status_code, response.text)
        payload = response.json()
        self.assertEqual(disk_before, payload.get("cleared_models"),
                         "写回执的条数与副本磁盘条数不一致（两个口径都该等于磁盘真值）")

        written = [row for row in (payload.get("endpoints") or []) if row.get("id") == endpoint_id]
        self.assertEqual(1, len(written), "写响应的行里该条目没了（_write_response 复用 M1 的组装）")
        self.assertEqual(0, written[0].get("allowlist_count"),
                         "写响应里的行仍报非零白名单条数（磁盘已经清空了）")

        row = self._row_of(endpoint_id)
        self.assertIsNotNone(row, "清空后条目从 GET /endpoints 里消失了")
        self.assertEqual(0, row.get("allowlist_count"),
                         "清空后 allowlist_count 没归零（磁盘 {} → 卡片该显「已添加 0 个模型」）")
        self.assertEqual([default_model], row.get("models") or [],
                         "注入视图的半边判据变了（models 语义冻结，不许被顺手清零）")
        self.assertEqual(_disk_models_count(read_raw_config(), endpoint_id), row["allowlist_count"],
                         "落盘的 models 条数与行上的 allowlist_count 不一致")
        self.assertLess(row["allowlist_count"], len(row["models"]),
                        "两个数字此刻没分出真假：本用例的差分判据没被踩到")
        # 只加字段：M1 冻结的字段集一个都不能少（前端所有既有读法照常工作）
        for field in ("id", "name", "base_url", "model", "models", "discover_models",
                      "has_api_key", "api_key_plaintext", "is_current", "source"):
            self.assertIn(field, row, f"补丁把既有字段 {field} 弄丢了")
        # legacy（cc-switch）行：同样带字段，且等于行里的条数（那条路没有注入）
        legacy_name = self._legacy_sample()
        legacy_row = self._row_of(f"{plugin_api.CC_ID_PREFIX}{legacy_name}")
        self.assertIsNotNone(legacy_row, f"前置：cc:{legacy_name} 行不在列表里")
        self.assertIn("allowlist_count", legacy_row)
        self.assertEqual(len(legacy_row.get("models") or []), legacy_row["allowlist_count"],
                         "legacy 行的 allowlist_count 该等于它自己的 models 条数（无注入）")
        self.assertEqual(_legacy_disk_models_count(read_raw_config(), legacy_name),
                         legacy_row["allowlist_count"],
                         "legacy 行的 allowlist_count 不等于副本磁盘条数")

    def test_cleared_row_seeds_allowlist_models_empty_so_the_editor_refolds_nothing(self):
        """契约补丁第二处追加（**编辑态**）的差分证据：清空后同一行上
        ``allowlist_models == []`` 而 ``models == [默认模型]``，于是 `formFromRow` 的种子为空。

        M5 上报的缺口只说了一半：卡片数字由 `allowlist_count` 救回来了，但「查看 → 编辑」的
        回填过去吃 ``row.models``（注入视图），所以**刚清空**的条目一打开编辑器就又挂着默认模型
        那一条 —— 卡片显「已添加 0 个模型」、栏里显 1 条，与 M4.7「『已添加』栏列出当前白名单
        = 磁盘真值」正好相反（UC-12 / T24 的意图被回填路径绕过）。

        期望值一律运行时从副本推（prompt §2.7）。本用例只跑清空与两次只读列表，种子是**纯函数**，
        所以「打开编辑器」这一步落盘零字节 —— 由 `config.yaml` 的字节快照钉住。
        """
        endpoint_id = self._clear_sample()
        entry_before = _provider_entry(read_raw_config(), endpoint_id)
        default_model = str(entry_before.get("model") or "")
        disk_ids_before = _disk_model_ids(entry_before)
        self.assertTrue(disk_ids_before and default_model,
                        "样本没有白名单或没有默认模型，编辑态差分判据无从验证")

        before = self._row_of(endpoint_id)
        self.assertIsNotNone(before, "前置：providers: 条目在 GET /endpoints 里没有行")
        self.assertIn("allowlist_models", before,
                      f"清空前该行缺 allowlist_models：{sorted(before)}")
        self.assertEqual(disk_ids_before, before["allowlist_models"],
                         "清空前的 allowlist_models 不等于副本磁盘的 models 键序")
        self.assertEqual(before["allowlist_count"], len(before["allowlist_models"]),
                         "两个派生字段各说一套（同一条来源链该保证的事）")
        # 清空前的种子 = 磁盘白名单（默认模型只在「非空且不在栏里」时才被并进第 0 位；
        # 本机快照里默认模型通常已在白名单内，所以这里按同一规则推期望值，不假设分布）
        expected_seed_before = list(disk_ids_before)
        if default_model and expected_seed_before and default_model not in expected_seed_before:
            expected_seed_before.insert(0, default_model)
        self.assertEqual(expected_seed_before, _form_added_seed(before),
                         "清空前的编辑器种子不等于磁盘白名单")

        # 种子镜像自己的两条规则（合成行，**不碰副本**）：JS 改了这两条这里必须跟着改
        self.assertEqual(["m2", "m1", "m3"], _form_added_seed(
            {"allowlist_models": ["m1", "m3"], "model": "m2", "models": ["m2", "m1", "m3"]}),
            "非空栏 + 默认模型缺席 ⇒ 折到第 0 位（不变式 1 的提交前判据依赖它）")
        self.assertEqual([], _form_added_seed(
            {"allowlist_models": [], "model": "m2", "models": ["m2"]}),
            "空栏是合法状态：不许把注入的默认模型塞成「已添加」")
        self.assertEqual(["m2", "m1"], _form_added_seed({"model": "m2", "models": ["m2", "m1"]}),
                         "字段缺失（老响应）时退回现行行为：种子 = 行里的 models")

        self.assertEqual(200, self._clear(endpoint_id).status_code)

        config_after_clear = _read_copy_bytes("config.yaml")
        row = self._row_of(endpoint_id)
        self.assertIsNotNone(row, "清空后条目从 GET /endpoints 里消失了")
        # ① 新字段：磁盘真值 = 空；既有字段：注入视图 = 只剩默认模型那一条（冻结不动）
        self.assertEqual([], row["allowlist_models"],
                         "清空后 allowlist_models 没成空列表（磁盘 {} → 编辑器种子该是空栏）")
        self.assertEqual([default_model], row.get("models") or [],
                         "注入视图的半边判据变了（models 语义冻结，不许被顺手清零）")
        self.assertEqual(0, row["allowlist_count"])
        self.assertEqual(row["allowlist_count"], len(row["allowlist_models"]))
        self.assertNotEqual(row["models"], row["allowlist_models"],
                            "两个字段此刻没分出真假：本用例的差分判据没被踩到")
        # ② formFromRow 的等价种子 = 空（**回归点**：旧种子吃 row.models，会带回默认模型）
        self.assertEqual([], _form_added_seed(row),
                         "编辑器仍把注入的默认模型当成用户「已添加」的那一条（formFromRow 缺口未修）")
        old_seed = _form_added_seed(dict(row, allowlist_models=None))
        self.assertEqual([default_model], old_seed,
                         "回退路径（老响应无该字段）应当仍是现行行为：注入视图里那 1 条")
        self.assertNotEqual(old_seed, _form_added_seed(row),
                            "新字段没改变回填结果 —— 补丁等于没生效")
        # ③ 「只是打开编辑器」= 一个字节都不写（种子是纯函数，写盘要用户点保存）
        self.assertEqual(config_after_clear, _read_copy_bytes("config.yaml"),
                         "回填种子把副本写了（本用例没有任何写动作）")
        self.assertEqual({}, _entry_models(read_raw_config(), endpoint_id),
                         "副本磁盘的 models 被种子动过（清空态必须保持）")
        # ④ 只加字段：M1 冻结的字段集与既有语义都在原位
        for field in ("id", "name", "base_url", "model", "models", "discover_models",
                      "has_api_key", "api_key_plaintext", "is_current", "source",
                      "allowlist_count"):
            self.assertIn(field, row, f"补丁把既有字段 {field} 弄丢了")
        # ⑤ cc-switch 行：那条路没有注入，allowlist_models 就是它自己的 models（磁盘序列）
        legacy_name = self._legacy_sample()
        legacy_row = self._row_of(f"{plugin_api.CC_ID_PREFIX}{legacy_name}")
        self.assertIsNotNone(legacy_row, f"前置：cc:{legacy_name} 行不在列表里")
        self.assertIn("allowlist_models", legacy_row)
        self.assertEqual(legacy_row.get("models"), legacy_row["allowlist_models"],
                         "legacy 行的 allowlist_models 该照抄它自己的 models（无注入）")
        self.assertEqual(_disk_model_ids(next(
            entry for entry in _raw_legacy(read_raw_config())
            if str(entry.get("name") or "").strip() == legacy_name)), legacy_row["allowlist_models"],
            "legacy 行的 allowlist_models 不等于副本磁盘的 models 序列")
        self.assertEqual(legacy_row["allowlist_count"], len(legacy_row["allowlist_models"]))

    def test_clear_keeps_pool_floor_fields_untouched_and_top_level_model_alone(self):
        """E7 / §7.7 不变式 3 的落盘半边：清空**不动** ``model:`` 与顶层 ``model:`` 镜像。

        池里那个默认模型是唯一删不掉的东西（``_absorb_entry_models`` 无条件折入，
        出处见 prompt §2.6 C8），所以清空后条目的 ``model:`` 必须仍在、顶层 ``model:``
        （当前供应商）必须一字节不变 —— 「清空」≠「池里空」。
        """
        endpoint_id = self._clear_sample()
        raw_before = read_raw_config()
        default_before = str(_provider_entry(raw_before, endpoint_id).get("model") or "")
        self.assertTrue(default_before, "样本没有默认模型，池下限判据无从验证")
        top_model_before = copy.deepcopy(raw_before.get("model"))
        providers_keys_before = sorted(str(key) for key in _raw_providers(raw_before))
        legacy_before = copy.deepcopy(_raw_legacy(raw_before))

        self.assertEqual(200, self._clear(endpoint_id).status_code)

        raw_after = read_raw_config()
        self.assertEqual(default_before,
                         str(_provider_entry(raw_after, endpoint_id).get("model") or ""),
                         "清空白名单把条目的默认模型也抹了（池里那个删不掉，E7）")
        self.assertEqual(top_model_before, copy.deepcopy(raw_after.get("model")),
                         "清空写到了顶层 model:（那等于切换当前供应商）")
        self.assertEqual(providers_keys_before, sorted(str(key) for key in _raw_providers(raw_after)),
                         "providers: 的条目集变了（清空不许增删条目）")
        self.assertEqual(legacy_before, _raw_legacy(raw_after),
                         "清空 providers: 条目时碰了 custom_providers:")

    def test_clear_is_idempotent_second_call_writes_no_bytes(self):
        """连点两次「清空白名单」：第二次**一个字节都不动**（顺带证明平台的注释块不累加）。"""
        endpoint_id = self._clear_sample()
        first = self._clear(endpoint_id)
        self.assertEqual(200, first.status_code, first.text)
        snapshot = _read_copy_bytes("config.yaml")
        env_snapshot = _read_copy_bytes(".env")

        second = self._clear(endpoint_id)
        self.assertEqual(200, second.status_code, second.text)
        self.assertEqual(0, second.json().get("cleared_models"),
                         "第二次清空又报清掉了 N 条（幂等性判据）")
        self.assertEqual(snapshot, _read_copy_bytes("config.yaml"),
                         "第二次清空又写了文件（幂等性 / 平台注释块在累加）")
        self.assertEqual(env_snapshot, _read_copy_bytes(".env"))

    # ------------------------------------------------------------- M5.3 / M5.7：拒绝 cc-switch

    def test_clear_cc_switch_entry_is_refused_and_writes_nothing(self):
        """M5.7：对 cc-switch 条目（本机快照 ``sensenova``）清空 → 4xx 拒绝且 config **零改动**。

        两条都要测：① 前端传来的 ``cc:<name>`` 形态（后端不信前端）；② 只有前缀没有条目名。
        两种都必须**一个字节都不写** —— 被拒的请求连 config 都不必打开（detail 里先拒）。
        """
        name = self._legacy_sample()
        config_before = _read_copy_bytes("config.yaml")
        env_before = _read_copy_bytes(".env")
        self.assertEqual(self.pristine_config, config_before, "前置假设变了：副本已被改过")

        for endpoint_id, expected in ((f"{plugin_api.CC_ID_PREFIX}{name}", name),
                                      (plugin_api.CC_ID_PREFIX, plugin_api.CC_ID_PREFIX)):
            with self.subTest(endpoint_id=endpoint_id):
                response = self._clear(endpoint_id)
                self.assertEqual(400, response.status_code,
                                 f"cc-switch 条目应当被明确拒成 400，实到 {response.status_code}")
                detail = (response.json() or {}).get("detail") or ""
                self.assertIn(expected, detail,
                              "拒绝原文要带上被拒的 id（前端 toast 的判据）")
                self.assertIn("cc-switch", detail, "拒绝原文要说明是决议 12 的所有权问题")
                self.assertEqual(config_before, _read_copy_bytes("config.yaml"),
                                 "拒绝之后副本被改了（「报错不动手」破防）")
                self.assertEqual(env_before, _read_copy_bytes(".env"), "拒绝之后 .env 被改了")

    def test_clear_bare_legacy_name_is_404_and_writes_nothing(self):
        """不带 ``cc:`` 前缀的裸条目名（只存在于 ``custom_providers:``）→ 404，**不动手**。

        后端不信前端：清空一律只认 ``providers:`` 那一段，``custom_providers:`` 里的同名
        条目既不会被写、也不会被「顺手迁移」过去。
        """
        name = self._legacy_sample()
        self.assertIsNone(self._row_of(name), "前置假设变了：裸 name 在列表里有行（那就是 providers: 条目）")
        config_before = _read_copy_bytes("config.yaml")
        providers_keys_before = sorted(str(key) for key in _raw_providers(read_raw_config()))

        response = self._clear(name)
        self.assertEqual(404, response.status_code, response.text)
        detail = (response.json() or {}).get("detail") or ""
        self.assertIn(name, detail)
        self.assertEqual(config_before, _read_copy_bytes("config.yaml"),
                         "定位失败却写了副本（「报错不动手」破防）")
        self.assertEqual(providers_keys_before,
                         sorted(str(key) for key in _raw_providers(read_raw_config())),
                         "被拒的清空往 providers: 里加了条目（绝不静默新建）")

    # ------------------------------------------------------------- M5.8：不存在的 id → 404

    def test_clear_missing_entry_is_404_and_creates_no_entry(self):
        """M5.8：不存在的 id → 404 且**不创建**新条目（防「静默新建」）：providers 键集不变。"""
        config_before = _read_copy_bytes("config.yaml")
        env_before = _read_copy_bytes(".env")
        providers_keys_before = sorted(str(key) for key in _raw_providers(read_raw_config()))
        raw_providers_before = copy.deepcopy(_raw_providers(read_raw_config()))

        response = self._clear(MISSING_CLEAR_ID)
        self.assertEqual(404, response.status_code, response.text)
        detail = (response.json() or {}).get("detail") or ""
        self.assertIn(MISSING_CLEAR_ID, detail, "错误原文要带上定位不到的 id")
        self.assertEqual(providers_keys_before,
                         sorted(str(key) for key in _raw_providers(read_raw_config())),
                         "providers: 里多出了一条（清空绝不新建条目）")
        self.assertEqual(raw_providers_before, _raw_providers(read_raw_config()),
                         "providers: 段被改写（404 之后应当什么都没动）")
        self.assertEqual(config_before, _read_copy_bytes("config.yaml"), "404 之后副本被改了")
        self.assertEqual(env_before, _read_copy_bytes(".env"), "404 之后 .env 被改了")

    def test_clear_empty_id_is_400_without_touching_disk(self):
        """路径里 id 为空 → 400（不拿空串去扫全表），且磁盘一字节不动。"""
        config_before = _read_copy_bytes("config.yaml")
        self._guard_before_write()
        with self.assertRaises(HTTPException) as caught:
            plugin_api.clear_models_endpoint("")
        self.assertEqual(400, caught.exception.status_code)
        self.assertEqual(config_before, _read_copy_bytes("config.yaml"))

    # ------------------------------------------------------------ R8：profile 透传（写路径）

    def test_clear_with_current_profile_is_a_no_override(self):
        """R8：``profile=current`` 与不传等价（``_config_profile_scope`` 无覆盖），清空仍落在副本上。"""
        self._guard_before_write()
        endpoint_id = self._clear_sample()
        response = self.client.post(
            CLEAR_PATH_TEMPLATE.format(endpoint_id=endpoint_id), params={"profile": "current"})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual({}, _entry_models(read_raw_config(), endpoint_id),
                         "profile=current 的清空没落到进程默认 profile（= 副本）上")

    def test_clear_unknown_profile_is_refused_and_writes_nothing(self):
        """R8 的反面：未知 profile 必须**如实失败**，不能悄悄写默认 profile 的条目。"""
        self._guard_before_write()
        endpoint_id = self._clear_sample()
        config_before = _read_copy_bytes("config.yaml")
        response = self.client.post(
            CLEAR_PATH_TEMPLATE.format(endpoint_id=endpoint_id),
            params={"profile": "definitely-not-a-profile"})
        self.assertGreaterEqual(response.status_code, 400, response.text)
        self.assertLess(response.status_code, 500, response.text)
        self.assertEqual(config_before, _read_copy_bytes("config.yaml"),
                         "未知 profile 却写了默认 profile 的副本（R8 破防）")

    # ------------------------------------------------- 共享 helper 契约（对 M6/M7 冻结）

    def test_locate_providers_entry_helper_contract(self):
        """M5 归属的共享 helper（progress §四 / prompt §5.6）：``(stored_key, entry)`` +
        **活引用** + 找不到抛 404 + **绝不新建**、**绝不写盘**。
        """
        import inspect

        signature = inspect.signature(plugin_api._locate_providers_entry)
        self.assertEqual(["cfg", "endpoint_id"], list(signature.parameters))
        hints = typing.get_type_hints(plugin_api._locate_providers_entry)
        self.assertEqual(Dict[str, Any], hints["cfg"])
        self.assertEqual(Tuple[Optional[str], Dict[str, Any]], hints["return"])

        endpoint_id = self._clear_sample()
        with _config_profile_scope(None):
            cfg = load_config()
        stored, entry = plugin_api._locate_providers_entry(cfg, endpoint_id)
        self.assertEqual(endpoint_id, str(stored))
        # 活引用：改的就是 cfg["providers"] 里那份，这是「定点改字段 → 一次 save」的前提
        self.assertIs(entry, _raw_providers(cfg)[endpoint_id])
        # 定位链完全交给官方 find_provider_entry（**大小写敏感**：只做 str().strip() 后比对，
        # 所以这里不拿 upper() 去试探 —— 换名重扫是 legacy 那条链的口径，不是这条）
        padded_stored, padded_entry = plugin_api._locate_providers_entry(cfg, f"  {endpoint_id}  ")
        self.assertIs(entry, padded_entry)
        self.assertEqual(endpoint_id, str(padded_stored))
        # 找不到 → 404，且**不**在 cfg 里留下空条目
        with self.assertRaises(HTTPException) as caught:
            plugin_api._locate_providers_entry(cfg, MISSING_CLEAR_ID)
        self.assertEqual(404, caught.exception.status_code)
        self.assertNotIn(MISSING_CLEAR_ID, _raw_providers(cfg))
        # 只有 cc: 前缀 / 空 cfg / None cfg 都不能把 helper 打成异常之外的东西
        for bad in (None, "", "   ", {}):
            with self.subTest(bad=bad):
                with self.assertRaises(HTTPException) as still_404:
                    plugin_api._locate_providers_entry(bad if isinstance(bad, dict) else {},
                                                       bad)
                self.assertEqual(404, still_404.exception.status_code)
        # 纯读 helper：跑完一圈副本还是字节级原样
        self.assertEqual(self.pristine_config, _read_copy_bytes("config.yaml"))

    def test_clear_route_contract_path_profile_and_no_key_material(self):
        """契约：路径 ``/endpoints/{endpoint_id}/clear-models``、形参
        ``(endpoint_id, profile=None)``、响应复用 ``_write_response`` 且无密钥材料。
        """
        import inspect

        signature = inspect.signature(plugin_api.clear_models_endpoint)
        self.assertEqual(["endpoint_id", "profile"], list(signature.parameters))
        self.assertIsNone(signature.parameters["profile"].default)
        self.assertEqual("/endpoints/{endpoint_id}/clear-models", plugin_api.CLEAR_MODELS_PATH)

        routes = {(tuple(sorted(route.methods)), route.path) for route in plugin_api.router.routes}
        self.assertIn((("POST",), "/endpoints/{endpoint_id}/clear-models"), routes,
                      f"路由表没挂上 clear-models：{sorted(path for _methods, path in routes)}")

        endpoint_id = self._clear_sample()
        response = self._clear(endpoint_id)
        self.assertEqual(200, response.status_code, response.text)
        self.assertNotIn(FORBIDDEN_POOL_COPY, response.text)
        # 文本级只查 redact 预览（`has_api_key` / `api_key_plaintext` 里含 "api_key" 子串是正常的）
        self.assertNotIn("api_key_preview", response.text, "清空响应里出现了密钥预览字段")
        for row in (response.json().get("endpoints") or []):
            for key in FORBIDDEN_RESPONSE_KEYS:
                self.assertNotIn(key, row, f"清空响应的行里出现了密钥材料字段 {key}")

    # ------------------------------------------------------- 前端静态 lint（铁律 3 / M5.3）

    def test_plugin_js_copy_and_gate_lint(self):
        """``plugin.js`` 的**静态** lint（不是渲染证据，见 prompt §5.4）：
        铁律 3 的禁用说法零命中 + M5.4 的二次确认文案在位 + 清空按钮按 ``source`` 闸门渲染
        + ``useClearAllowlist`` 把 ``confirmOpen`` / ``setConfirmOpen`` **两个都**导出
        （少一个二次确认弹窗就废掉，见方法末尾那条护栏）。
        """
        source = (DESKTOP_DIR / "plugin.js").read_text(encoding="utf-8")
        backend = Path(plugin_api.__file__).read_text(encoding="utf-8")
        for text in (source, backend):
            self.assertNotIn(FORBIDDEN_POOL_COPY, text,
                             f"铁律 3：「{FORBIDDEN_POOL_COPY}」这类做不到的说法不许出现在任何文案里（E7）")
        # M5.4 逐字文案（模板里的 {count} / {model} 运行时填）
        self.assertIn("将移除该供应商的全部 {count} 个已添加模型", source)
        self.assertIn("保存后该供应商在选择器里只剩默认模型 {model}。", source)
        # M5.5 的两半
        self.assertIn("已添加 0 个模型", source)
        self.assertIn("选择器里仍会有默认模型 {model} 一个", source)
        # M5.3 的闸门：只对 source === 'providers' 的行渲染
        self.assertIn("function isProvidersSource(row) {", source)
        self.assertIn("return textOf(row && row.source) === PROVIDERS_SOURCE", source)
        self.assertIn("isProvidersSource(row)", source)
        # 走的是 M4 的同一条失效路径与同一个 detail 出口
        self.assertIn("ctx.rest(clearModelsPathFor(target.id), { method: 'POST' })", source)
        self.assertIn("queryClient.invalidateQueries({ queryKey: ENDPOINTS_QUERY_KEY })", source)
        self.assertIn("backendErrorDetail(error), CLEAR_FAILED_TOAST_TITLE", source)
        # 契约补丁的前端半边：磁盘真值字段只在**一处**被读（`modelCountOf`），
        # 卡片「已添加 N 个模型」/ poolCompareText / 本区逐行计数全经它取数，不许散落第二份判据
        self.assertEqual(1, source.count("row.allowlist_count"),
                         "allowlist_count 的读取没收敛到 modelCountOf 一处（判据会散落）")
        self.assertIn("if (Number.isInteger(allowlistCount) && allowlistCount >= 0) {", source)
        # 契约补丁的**编辑态半边**（本 follow-up）：磁盘真值列表也只在**一处**被读
        # （`formFromRow` 的「已添加」栏种子），展示口径仍走上面那个 `modelCountOf`，一处不改。
        self.assertEqual(1, source.count("row.allowlist_models"),
                         "allowlist_models 的读取没收敛到 formFromRow 的种子一处（回填判据会散落）")
        self.assertIn("const diskAllowlist = row && row.allowlist_models", source,
                      "formFromRow 不再优先吃磁盘真值字段（清空后的条目会被回填成「已添加 1 条」）")
        self.assertIn("const addedSeed = Array.isArray(diskAllowlist)", source,
                      "种子不再从磁盘真值分支（或回退路径被改写）")
        self.assertIn("models: rowModel && models.length > 0 && !models.includes(rowModel)", source,
                      "默认模型又被无条件折进「已添加」栏（空栏该保持为空 —— M4.7）")
        self.assertEqual(1, source.count("function modelCountOf(row) {"),
                         "展示用的计数函数被复制出第二份（本补丁明确不动它）")
        # 回归护栏（缺陷修复 round 1，真机确认）：本区的 `ConfirmDialog` 把 `open` 读自
        # `actions.confirmOpen`，所以 **hook 的 return 必须把 `confirmOpen` 这个 state 本身
        # 一起导出**。此前只导出了 `setConfirmOpen` ⇒ `open` 恒为 undefined ⇒ 点
        # 「清空白名单」什么都不发生、零 console 报错、零 toast（M6 的迁移区同一个因，
        # 见 test_migrate.py 的同名护栏）。⚠️ 静态判据、不是渲染证据（prompt §5.4）。
        def assert_confirm_state_exported(hook_name: str) -> None:
            """`hook_name` 的 `return {}` 必须同时导出 `confirmOpen` 与 `setConfirmOpen`。

            前提（全文件有 `ConfirmDialog` 读 `actions.confirmOpen`）一旦消失就**响**而不是
            悄悄跳过 —— 永真的护栏比没有护栏更坏（与 M6 那条「找不到区块头注即 fail」同口径）。
            """
            self.assertIn(CONFIRM_DIALOG_OPEN_EXPR, source,
                          f"{hook_name} 的护栏前提已失效：全文件不再有任何 ConfirmDialog 读"
                          f"「{CONFIRM_DIALOG_OPEN_EXPR}」，请同步改写本判据（别让它悄悄永真）")
            start = source.index(f"function {hook_name}(")
            returned = source[start:source.index("\n  }", source.index("\n  return {", start))]
            for state, why in (("confirmOpen", "弹窗永远打不开（open 恒为 undefined）"),
                               ("setConfirmOpen", "弹窗关不掉")):
                self.assertIn(f"\n    {state}", returned,
                              f"{hook_name} 的 return 少了 {state} ⇒ {why}")

        assert_confirm_state_exported("useClearAllowlist")


if __name__ == "__main__":
    unittest.main()
