"""M6 验收：cc-switch 条目迁移 ``POST /endpoints/migrate``（M6.6 / M6.7 / M6.8；T15–T17 / T20 / R11）。

⚠️ **本文件是全项目唯一会搬动明文密钥的测试**，所以纪律比别处更硬：

* 只跑在 ``HERMES_HOME`` 的**临时副本**上（``tasks/progress.md`` §五 / ``prompt.md`` §5.1 逐字命令）；
  每个写用例的**第一行**都是 ``_guard_before_write()``（``config.yaml`` 与 ``.env`` 两条路径都必须
  落在系统临时目录下的副本里），``setUp`` / ``tearDown`` 各自把副本还原成 ``setUpClass`` 的字节快照。
* **任何断言都不许把密钥值打印出来**：副本里带着真密钥，而 unittest 的默认失败消息会把两个参数
  原文吐进测试输出。所以涉及密钥的比较一律走 ``_assert_absent`` / ``_assert_present``（固定
  ``msg``）与 **sha256 比对**；本文件不出现任何真实 key 的字面量。
* 零网络（迁移是纯 config / ``.env`` 操作，用 ``mock`` 把探测函数换成「一调就炸」来证明）。
* 计数一律「先从副本读、再断言」（prompt §2.7）：``sensenova`` / ``bailian`` 只是**优先**取材对象，
  「2 条 legacy」「3 个模型」都是 2026-09-22 快照，不硬编码成期望值。

跑法（填好占位后照抄；跑完 ``rm -rf`` —— 副本里有明文密钥，不许留在盘上）：

    cd "<本仓根目录>"                        # 必须 cd 到含 tests/ 的目录，discover 依赖 cwd
    V="<装有 hermes_cli 的 venv 里的 python>"
    T="<临时目录>/supplier-models-harness"    # 副本务必建在仓外，本仓 .gitignore 已排除 config/.env
    rm -rf "$T" && mkdir -p "$T"
    cp "<副本源 config.yaml>" "$T/config.yaml"   # 副本源须仍留有 legacy custom_providers: 条目
    cp "<副本源 .env>"        "$T/.env"
    HERMES_HOME="$(cygpath -w "$T")" "$V" -m unittest discover -s tests -v
    rm -rf "$T"

判据口径（⚠️ 批 2 决策 3）：**值级比对**为主 —— 官方 ``save_config`` 首次写入会在文件末尾追加
约 40 行**纯注释**示例块（``_commented_sections_for_save``），值层无影响但逐行 diff 必假红，
所以「迁移只搬了那一个条目」由 :func:`_changed_paths` 钉死（摘掉搬家的两侧之后两份 config
必须逐值相等）。

关于 T15 原文的「``config.yaml`` 里**不再有明文 key**」：**实测做不到整文件归零**（上报进决策日志）：
本机除两条 legacy 明文之外还有**第三处**明文 —— ``auxiliary.vision.api_key``（R11 原文），
迁移**不碰**它。所以那句话在这里被拆成三条可证的形式：
① 被迁移的那把 key 在 ``config.yaml`` 里**一处都找不到**；
② ``sk-`` 出现次数**恰好减 1**；
③ 两条 legacy 全迁完之后，残留数**等于** ``auxiliary.*`` 自己带明文的槽数（本机 = 1）——
   这一条同时是 R11 的自动化证据。
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
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
MIGRATE_PATH = f"{MOUNT_PREFIX}/endpoints/migrate"
# 密钥铁律（与 M1 的 KEY_MATERIAL_KEYS 同口径）：响应里不许出现任何密钥材料字段
FORBIDDEN_RESPONSE_KEYS = ("api_key", "api_key_preview")
# 任务书点名的样本（本机 2026-09-22 快照：两条 legacy 都带明文 key）——**只当优先取材对象**
PREFERRED_MIGRATE_NAME = "sensenova"
PREFERRED_AUX_HIT_NAME = "bailian"
# 造边界用例用的合成条目（`.invalid` TLD：结构上不可能有真端点，也证明零网络）
SYNTH_PREFIX = "zz-m6-"
SYNTH_KEY_ENV_ONLY_NAME = f"{SYNTH_PREFIX}keyenv-only"
SYNTH_TEMPLATE_NAME = f"{SYNTH_PREFIX}template-src"
SYNTH_COLLISION_NAME = f"{SYNTH_PREFIX}collider"
SYNTH_DUPLICATE_NAME = f"{SYNTH_PREFIX}twin"
SYNTH_NOKEY_NAME = f"{SYNTH_PREFIX}nokey"
SYNTH_BASE_URL = "https://m6-synth.invalid/v1"
SYNTH_MODEL = "zz-m6-synth-model"
# 边界④ 的模板源变量：**测试自己造假的值**，只用于证明「展开值不会被物化」
SYNTH_TEMPLATE_VAR = "ZZ_M6_FAKE_TEMPLATE_SOURCE"
SYNTH_TEMPLATE_EXPANDED = "zz-m6-fake-expanded-value-not-a-real-key"
# 语义铁律 3 里**永远不许出现**的说法（拆成两段拼起来，好让全文 grep 连本文件一起过）
FORBIDDEN_POOL_COPY = "不提供" + "任何模型"
# 二次确认弹窗的 `open` 在 M5 / M6 区块里的取法（`:2967` / `:3399`）。
# 用作「hook 必须导出 confirmOpen state」那条护栏的**前提标记**（缺陷修复 round 1，
# 与 test_clear_models.py 的同名常量配对）。
CONFIRM_DIALOG_OPEN_EXPR = "open: actions.confirmOpen"


# --------------------------------------------------------------- 副本读写原语

def _home() -> Path:
    value = os.environ.get("HERMES_HOME")
    if not value:
        raise AssertionError("HERMES_HOME 未设置 —— 请用 progress §五 的测试骨架命令跑")
    return Path(value).resolve()


def _copy_file(name: str) -> Path:
    return _home() / name


def _read_copy_bytes(name: str = "config.yaml") -> bytes:
    return _copy_file(name).read_bytes()


def _write_copy_bytes(data: bytes, name: str = "config.yaml") -> None:
    _copy_file(name).write_bytes(data)


def _read_copy_text(name: str = "config.yaml") -> str:
    return _read_copy_bytes(name).decode("utf-8", errors="replace")


def _raw() -> Dict[str, Any]:
    return read_raw_config() or {}


def _raw_providers(raw: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    providers = (raw if raw is not None else _raw()).get("providers")
    return providers if isinstance(providers, dict) else {}


def _raw_legacy(raw: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    sequence = (raw if raw is not None else _raw()).get("custom_providers")
    return [entry for entry in (sequence or []) if isinstance(entry, dict)]


def _legacy_names(raw: Optional[Dict[str, Any]] = None) -> List[str]:
    return [str(entry.get("name") or "").strip() for entry in _raw_legacy(raw)]


def _provider_entry(raw: Dict[str, Any], endpoint_id: Any) -> Dict[str, Any]:
    """按**官方同一条**定位链（``find_provider_entry``）取 ``providers:`` 条目（返回副本）。"""
    _stored, entry = plugin_api.find_provider_entry((raw or {}).get("providers"), endpoint_id)
    return copy.deepcopy(entry) if isinstance(entry, dict) else {}


def _disk_model_ids(entry: Any) -> List[str]:
    """条目 ``models:`` 的**磁盘** id 列表（dict 数键、list 数元素、其它形状 → ``[]``）。

    故意在测试里另写一遍口径、**不**调后端的 ``_entry_model_ids``：期望值与实装值必须来自两处
    代码，否则断言永远为真、抓不到实现漂移（prompt §2.7）。
    """
    models = entry.get("models") if isinstance(entry, dict) else None
    if isinstance(models, dict):
        return [str(key).strip() for key in models if str(key).strip()]
    if isinstance(models, (list, tuple)):
        return [str(item).strip() for item in models if str(item).strip()]
    return []


def _disk_models_subfields(entry: Any) -> Dict[str, Dict[str, Any]]:
    """``models:`` 每个键的**子字段**（本机 sensenova 每条带 ``name:``，官方重建时会丢）。"""
    models = entry.get("models") if isinstance(entry, dict) else None
    if not isinstance(models, dict):
        return {}
    return {str(key): copy.deepcopy(value)
            for key, value in models.items() if isinstance(value, dict)}


def _sha(value: Any) -> str:
    marker = "\x00<absent>" if value is None else str(value)
    return hashlib.sha256(marker.encode("utf-8")).hexdigest()


def _env_value(env_text: str, name: str) -> Optional[str]:
    """``.env`` 里某个变量的值（去引号）；不存在 → ``None``。**只给哈希比对用**。"""
    for line in env_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("export "):
            stripped = stripped[len("export "):].strip()
        key, separator, value = stripped.partition("=")
        if separator and key.strip() == name:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            return value
    return None


def _env_names(env_text: str) -> List[str]:
    names = []
    for line in env_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        names.append(stripped.partition("=")[0].replace("export ", "").strip())
    return names


def _assert_absent(test: unittest.TestCase, needle: str, haystack: str, message: str) -> None:
    """**脱敏版** ``assertNotIn``：默认失败消息会把 needle（可能是密钥）原文吐进测试输出。"""
    if needle:
        test.assertTrue(needle not in haystack, message)


def _assert_present(test: unittest.TestCase, needle: str, haystack: str, message: str) -> None:
    """脱敏版 ``assertIn``（同上：失败消息不回显 needle）。"""
    if needle:
        test.assertTrue(needle in haystack, message)


def _walk_keys(node: Any):
    if isinstance(node, dict):
        for key, value in node.items():
            yield str(key)
            yield from _walk_keys(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _walk_keys(item)


def _changed_paths(before: Any, after: Any, path: str = "") -> List[str]:
    """两份解析后 config 的**值级**差异路径（只报「路径 + 变化种类」，**绝不回显值**——
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


def _excise_moved_entry(raw: Dict[str, Any], endpoint_id: Any) -> Dict[str, Any]:
    """深拷贝后**只摘掉** ``providers.<endpoint_id>`` 与整个 ``custom_providers`` 段。

    剩下的必须逐值相等 —— 这就是「迁移只搬了那一个条目、其余零改动」的值级判据。
    （``custom_providers`` 整段摘掉是因为「搬家」本身就表现为「少一条」；段内**别的条目**有没有
    被顺手改动由「其它条目名与内容原样」那几条断言另行钉住，条目自身丢字段由
    :meth:`MigrateRouteTest.test_migrated_entry_keeps_carried_fields` 钉住。）
    """
    cut = copy.deepcopy(raw or {})
    providers = cut.get("providers")
    if isinstance(providers, dict):
        stored, _entry = plugin_api.find_provider_entry(providers, endpoint_id)
        if stored is not None:
            providers.pop(stored, None)
    cut.pop("custom_providers", None)
    return cut


def _auxiliary_plaintext_slots(raw: Dict[str, Any]) -> int:
    """``auxiliary.*`` 里**自己带非模板明文 ``api_key``** 的槽数（R11 的「第三处明文」）。"""
    auxiliary = raw.get("auxiliary")
    if not isinstance(auxiliary, dict):
        return 0
    return sum(1 for slot in auxiliary.values()
               if isinstance(slot, dict) and plugin_api._raw_key_is_plaintext(slot.get("api_key")))


def _legacy_plaintext_entries(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    """``custom_providers:`` 里带**非模板明文** ``api_key`` 的条目（迁移的取材面）。"""
    return [entry for entry in _raw_legacy(raw)
            if plugin_api._raw_key_is_plaintext(entry.get("api_key"))]


def _expected_secret_residual(text_before: str, raw_before: Dict[str, Any], secret: str,
                              migrated_names: List[str]) -> int:
    """迁走 ``migrated_names`` 这批条目之后，``secret`` 这串在 ``config.yaml`` 里还应出现几次。

    ⚠️ 这里必须按「次数」而不是「有 / 无」判，因为 **R11 的实测事实**：本机
    ``auxiliary.vision.api_key`` 与 ``custom_providers.bailian.api_key`` 是**同一把** key ——
    迁移只摘 ``custom_providers:`` 那一份，辅助槽里那一份一模一样的字符串**照旧留着**
    （设计明确「迁移不动它，只额外提示」）。所以「迁完 = 全文找不到这串」对 ``bailian`` 是**假**的。
    同理，多条 legacy 共用一把 key 时只迁其中一条，残留次数仍要留出没迁的那几条。
    """
    wanted = {str(name or "").strip() for name in migrated_names}
    hits_in_migrated = sum(1 for entry in _legacy_plaintext_entries(raw_before)
                           if str(entry.get("api_key") or "") == secret
                           and str(entry.get("name") or "").strip() in wanted)
    return text_before.count(secret) - hits_in_migrated


def _sk_count_outside_custom_providers(raw: Dict[str, Any]) -> int:
    """``custom_providers`` **之外**还有几处 ``sk-``（把该段摘掉后序列化再数）。

    这是「迁完所有 legacy 明文之后 config.yaml 里理应剩多少 ``sk-``」的期望值 ——
    R11 说的那第三处明文就落在这里（``auxiliary.vision.api_key``）。
    """
    cut = copy.deepcopy(raw or {})
    cut.pop("custom_providers", None)
    return yaml.safe_dump(cut, allow_unicode=True, sort_keys=False).count("sk-")


def _load_copy_yaml() -> Dict[str, Any]:
    """副本 YAML 的**原样**解析（造前置状态用；不走平台缓存）。"""
    data = yaml.safe_load(_read_copy_text("config.yaml"))
    return data if isinstance(data, dict) else {}


def _store_copy_yaml(data: Dict[str, Any]) -> None:
    """把改过的前置状态写回副本（**只碰副本**；``tearDown`` 会还原成 pristine 字节）。"""
    _write_copy_bytes(yaml.safe_dump(data, allow_unicode=True, sort_keys=False).encode("utf-8"))
    invalidate_env_cache()


def _synth_legacy_entry(name: str, **extra: Any) -> Dict[str, Any]:
    """一条最小可用的合成 legacy（cc-switch 形态）条目：``name`` / ``base_url`` / ``model``。"""
    entry: Dict[str, Any] = {
        "name": name,
        "base_url": SYNTH_BASE_URL,
        "model": SYNTH_MODEL,
        "models": {SYNTH_MODEL: {"name": SYNTH_MODEL}},
    }
    entry.update(extra)
    return entry


def _add_legacy_entries(entries: List[Dict[str, Any]]) -> None:
    data = _load_copy_yaml()
    sequence = data.get("custom_providers")
    if not isinstance(sequence, list):
        sequence = []
    sequence.extend(entries)
    data["custom_providers"] = sequence
    _store_copy_yaml(data)


def _add_provider_entry(endpoint_id: str, entry: Dict[str, Any]) -> None:
    data = _load_copy_yaml()
    providers = data.get("providers")
    if not isinstance(providers, dict):
        providers = {}
    providers[endpoint_id] = entry
    data["providers"] = providers
    _store_copy_yaml(data)


def _strip_js_comments(text: str) -> str:
    """剥掉 JS 的块注释与整行行注释 —— 「无 JSX 标签」「颜色只用 ``var(--ui-*)``」这两条判据
    只能对**代码**跑：注释里写的 ``cc:<name>`` 会被 JSX 正则误当成标签。
    """
    without_blocks = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(?m)^[ \t]*//.*$", "", without_blocks)


# 模块区块头的统一形状（``plugin.js`` 里 M2–M7 逐字一致）：``/* ─── 区块 M<n>：<标题> …``。
# 只匹配**带长划线的区块头**，所以区块内部的定位注释（``/* 区块 M6：迁移区… */``）不会被
# 误认成边界。
JS_BLOCK_HEADER_RE = re.compile(r"/\*[ \t]*─+[ \t]*区块 M\d+：")


def _module_block_tail(source: str, marker: str) -> str:
    """取「``marker`` 所在**模块区块头结束之后 → 下一个模块区块头开始之前**」的原文。

    ⚠️ 判「本区块自己写了什么」（``useMutation({`` 的份数、逐行按钮调用点、禁语……）的切片
    **必须**止于本区块自己的范围，绝不许切到文件尾 —— 后者会让本模块替**所有后续模块**的
    正常追加买单（本文件那条 ``useMutation({ == 2`` 今天只是**恰好**因为 M7 自己没写 mutation
    才没红；M7 已把这件事上报：「那条判据的作用域应该 cut 在 M6 区块末尾而不是文件尾」）。
    起点取头注的 ``*/`` **之后**：``marker`` 在那条块注释内部，不这样切的话
    :func:`_strip_js_comments` 看不见开头的 ``/*``，头注会漏进「代码」里污染 JSX / 颜色判据。
    """
    header = source.index(marker)
    start = source.index("*/", header) + 2
    following = [position for position in
                 (match.start() for match in JS_BLOCK_HEADER_RE.finditer(source))
                 if position >= start]
    return source[start:following[0]] if following else source[start:]


def _js_const_source(text: str, name: str) -> str:
    """取 ``const <name> = …`` 这一条声明的原文（到下一个声明 / 注释块为止）。"""
    needle = f"const {name}"
    start = text.index(needle)
    rest = text[start:]
    stop = len(rest)
    for marker in ("\nconst ", "\n/**", "\nfunction ", "\n/* "):
        found = rest.find(marker, len(needle))
        if 0 < found < stop:
            stop = found
    return rest[:stop]


class MigrateRouteTest(unittest.TestCase):
    """M6.1 / M6.2 / M6.3 / M6.6 / M6.7 / M6.8：单次原子写 + 边界四条 + R11 前置检查。"""

    @classmethod
    def setUpClass(cls):
        app = FastAPI()
        app.include_router(plugin_api.router, prefix=MOUNT_PREFIX)
        cls.client = TestClient(app)
        cls.pristine_config = _read_copy_bytes("config.yaml")
        cls.pristine_env = _read_copy_bytes(".env") if _copy_file(".env").exists() else None

    # ---------------------------------------------------------------- 基础设施

    def setUp(self):
        self._guard_before_write()
        self.assertEqual(str((_home() / ".env").resolve()).lower(),
                         str(Path(get_env_path()).resolve()).lower(),
                         "get_env_path() 不在副本上，写测试会碰真 .env")
        os.environ.pop(SYNTH_TEMPLATE_VAR, None)     # 模板用例的假变量：每个用例从干净开始
        self._restore_copy()
        self._refresh()

    def tearDown(self):
        os.environ.pop(SYNTH_TEMPLATE_VAR, None)
        self._restore_copy()

    def _guard_before_write(self) -> None:
        """红线（prompt §5.2）：任何写入之前先确认落在 ``HERMES_HOME`` 临时副本上。"""
        home = _home()
        config_path = Path(get_config_path()).resolve()
        self.assertTrue(home.is_relative_to(Path(tempfile.gettempdir()).resolve()),
                        "写入前守卫失败：HERMES_HOME 不在系统临时目录下")
        self.assertTrue(str(config_path).lower().startswith(str(home).lower()),
                        "写入前守卫失败：get_config_path() 不在副本目录之下")
        self.assertEqual(str(home / "config.yaml").lower(), str(config_path).lower())

    def _restore_copy(self) -> None:
        """把副本还原成 ``setUpClass`` 的字节快照（**每个用例一份新鲜副本**）。"""
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
        raw = _raw()
        self.assertTrue(_raw_providers(raw), "副本里没有 providers: 条目，「搬到哪」无从验证")
        self.assertTrue(_raw_legacy(raw), "副本里没有 custom_providers: 条目，「从哪搬」无从验证")

    # ------------------------------------------------------------------ 取材

    def _migrate_sample(self) -> str:
        """一条「legacy + 有明文 key + 有默认模型」的条目名（T15 的迁移对象）。

        优先任务书点名的 ``sensenova``（本机快照 3 个模型、明文 ``api_key``），拿不到就退到
        「任一满足条件的 legacy 条目」——条数一律运行时读（prompt §2.7）。
        """
        candidates = [str(entry.get("name") or "").strip() for entry in _raw_legacy()
                      if str(entry.get("name") or "").strip()
                      and plugin_api._raw_key_is_plaintext(entry.get("api_key"))
                      and (str(entry.get("model") or "").strip() or _disk_model_ids(entry))]
        self.assertTrue(candidates,
                        "副本里没有「legacy + 明文 key + 有默认模型」的条目，T15 的迁移判据无从验证")
        preferred = [name for name in candidates if name == PREFERRED_MIGRATE_NAME]
        return preferred[0] if preferred else candidates[0]

    def _legacy_sample_pool(self) -> List[str]:
        return [str(entry.get("name") or "").strip() for entry in _raw_legacy()
                if str(entry.get("name") or "").strip()]

    def _aux_referenced_names(self) -> List[str]:
        """``auxiliary.*`` 以 ``custom:<name>`` 形式引用到的 legacy 条目名（R11 的命中集）。"""
        names = []
        auxiliary = _raw().get("auxiliary")
        if isinstance(auxiliary, dict):
            for slot in auxiliary.values():
                if isinstance(slot, dict):
                    provider = str(slot.get("provider") or "").strip()
                    if provider.lower().startswith(plugin_api.AUX_CUSTOM_REF_PREFIX):
                        names.append(provider[len(plugin_api.AUX_CUSTOM_REF_PREFIX):].strip())
        return [name for name in names if name]

    def _aux_sample(self, want_hit: bool) -> str:
        """挑一条 legacy 条目，其 R11 前置检查按 ``want_hit`` 命中 / 不命中（M6.3 的两半）。

        **不假设本机分布**：拿副本自己的 auxiliary 引用集实时比对（本机快照：
        ``auxiliary.vision.provider = custom:bailian`` ⇒ ``bailian`` 命中、``sensenova`` 不命中）。
        """
        referenced = set(self._aux_referenced_names())
        for name in self._legacy_sample_pool():
            if (name in referenced) is want_hit:
                return name
        raise unittest.SkipTest(
            "副本里的 legacy 条目在 auxiliary.* 引用上只覆盖了一侧，R11 的另一半判据无从验证"
            " —— 按 progress §五 换一份副本再跑")

    def _rows(self) -> List[Dict[str, Any]]:
        response = self.client.get(LIST_PATH)
        self.assertEqual(200, response.status_code, response.text)
        return response.json().get("endpoints") or []

    def _rows_for(self, row_id: str) -> List[Dict[str, Any]]:
        return [row for row in self._rows() if str(row.get("id") or "") == row_id]

    def _migrate(self, payload: Dict[str, Any]):
        """``POST /endpoints/migrate``；顺手证明这条链路**零网络**（探测函数一调就炸）。"""
        self._guard_before_write()
        with mock.patch.object(plugin_api, "validate_custom_endpoint",
                               side_effect=AssertionError("迁移链路不许发探测请求（纯 config 操作）")):
            return self.client.post(MIGRATE_PATH, json=payload)

    def _precheck(self, payload: Dict[str, Any]):
        """``precheck_only=true``：同样零网络，而且**一次都不许写盘**（save_config 一调就炸）。"""
        self._guard_before_write()
        with mock.patch.object(plugin_api, "save_config",
                               side_effect=AssertionError("precheck 分支一次都不许写盘")):
            return self.client.post(MIGRATE_PATH, json={**payload, "precheck_only": True})

    # ───────────────────────── M6.6（T15 自动化半边）：迁移 sensenova 的完整判据

    def test_migrate_moves_entry_gone_from_custom_providers_key_into_env(self):
        """M6.6 / T15：``providers.<id>`` 出现 + 原条目从 ``custom_providers:`` 消失 +
        那把明文 key 在 ``config.yaml`` 里一处都找不到 + ``.env`` 出现 ``key_env`` 命名的变量
        且**值等于原明文**（sha256 比对）+ 列表里**只有一行** + ``allowlist_*`` 自洽。
        """
        name = self._migrate_sample()
        target_id = plugin_api._custom_endpoint_id(name)
        env_var = plugin_api.custom_endpoint_key_env(target_id)

        raw_before = _raw()
        legacy_before = next(entry for entry in _raw_legacy(raw_before)
                             if str(entry.get("name") or "").strip() == name)
        secret = str(legacy_before.get("api_key") or "")
        self.assertTrue(plugin_api._raw_key_is_plaintext(secret),
                        "取材要求明文 key，否则 .env 的等值判据无从验证")
        self.assertIsNone(_env_value(_read_copy_text(".env"), env_var),
                          "前置：目标 .env 变量已经存在，等值判据会被污染")
        disk_ids_before = _disk_model_ids(legacy_before)
        names_before = _legacy_names(raw_before)
        providers_before = _raw_providers(raw_before)
        text_before = _read_copy_text("config.yaml")
        sk_count_before = text_before.count("sk-")
        env_before = _read_copy_bytes(".env")

        response = self._migrate({"id": f"{plugin_api.CC_ID_PREFIX}{name}"})
        self.assertEqual(200, response.status_code, response.text)
        payload = response.json()
        self.assertIs(True, payload.get("ok"))
        self.assertEqual(target_id, payload.get("id"), "响应的 id 不是迁移后的 providers: key")
        migrated = payload.get("migrated") or {}
        self.assertEqual(f"{plugin_api.CC_ID_PREFIX}{name}", migrated.get("from_id"),
                         "回执没报迁移前的行 id（M7 的按钮显隐判据要吃这一对）")
        self.assertEqual(target_id, migrated.get("to_id"))
        self.assertIs(True, migrated.get("removed_from_custom_providers"))
        self.assertEqual(plugin_api.KEY_DISPOSITION_PLAINTEXT, migrated.get("key_disposition"))
        self.assertEqual(env_var, migrated.get("env_var_name"), "回执没报密钥落点的变量名")
        self.assertIs(bool(legacy_before.get("discover_models", True)),
                      migrated.get("discover_models_before"))
        self.assertIs(False, migrated.get("discover_models"),
                      "铁律 1：回执里 discover_models 报的必须是迁移后的 false")

        raw_after = _raw()
        # ① 搬到 providers: 了，而且原条目从 custom_providers: 消失（决议 9：不保留）
        entry_after = _provider_entry(raw_after, target_id)
        self.assertTrue(entry_after, "providers: 里没有迁移后的条目")
        self.assertNotIn(name, _legacy_names(raw_after), "原条目还赖在 custom_providers: 里")
        self.assertEqual([item for item in names_before if item != name],
                         _legacy_names(raw_after),
                         "custom_providers: 里别的条目被顺手改了数量或顺序")
        # ② 明文 key 从 config.yaml 里「属于该条目的那一份」彻底消失（T15 那句话的可证形式；
        #    同串 key 若还被 auxiliary.* 复用，那是 R11 的另一处，本次迁移**不该**动它）
        text_after = _read_copy_text("config.yaml")
        self.assertEqual(_expected_secret_residual(text_before, raw_before, secret, [name]),
                         text_after.count(secret),
                         "迁移后该 key 串的残留次数不对（该条目那一份必须消失，别的槽不动）")
        self.assertEqual([], [entry for entry in _raw_legacy(raw_after)
                              if str(entry.get("api_key") or "") == secret],
                         "custom_providers: 段里还留着那把明文 key")
        self.assertEqual(sk_count_before - 1, text_after.count("sk-"),
                         "sk- 出现次数没有恰好少 1（第三处明文归 R11，别把它算进这一条）")
        # ③ .env 里出现 key_env 命名的变量，值等于原明文（**只比哈希**）
        env_after = _read_copy_text(".env")
        self.assertIn(env_var, _env_names(env_after), "迁移后 .env 里没有那个密钥变量")
        self.assertEqual(_sha(secret), _sha(_env_value(env_after, env_var)),
                         ".env 里的密钥值与原 config 明文不等（哈希比对，不打印值）")
        self.assertLess(len(env_before), len(_read_copy_bytes(".env")),
                        "官方 save_env_value 没往 .env 追加那一行（密钥没进 .env）")
        # ④ 新条目：引用 .env、不落明文
        self.assertEqual(env_var, entry_after.get("key_env"))
        self.assertNotIn("api_key", entry_after, "providers: 条目里还写着 api_key 明文")
        self.assertIs(False, entry_after.get("discover_models"))
        self.assertEqual(str(legacy_before.get("name") or "").strip(), entry_after.get("name"))
        self.assertEqual(len(providers_before) + 1, len(_raw_providers(raw_after)),
                         "providers: 段多出来的不恰好是 1 条")
        # ⑤ 白名单被带过来（官方 models_map 只增不减 + 无条件折默认模型 —— 批 3 实测口径）
        default_model = str(legacy_before.get("model")
                            or (disk_ids_before[0] if disk_ids_before else "")).strip()
        expected_ids = list(disk_ids_before)
        if default_model and default_model not in expected_ids:
            expected_ids.insert(0, default_model)
        self.assertEqual(sorted(expected_ids), sorted(_disk_model_ids(entry_after)),
                         "迁移后的白名单没把 legacy 的模型带过来（或顺手多塞了别条）")
        # ⑥ 值级无损：摘掉「搬家的两侧」之后整份 config 逐值相等
        self.assertEqual([], _changed_paths(_excise_moved_entry(raw_before, target_id),
                                            _excise_moved_entry(raw_after, target_id)),
                         "除搬家那一条之外还有别的字段变了（M6 收敛条件的无损判据）")
        # ⑦ 列表里只有一行（§2.5 发现 3：不会两行，但位置会变）
        self.assertEqual(1, len(self._rows_for(target_id)),
                         "迁移后 GET /endpoints 里该供应商不止一行（两套并存的特征）")
        self.assertEqual([], self._rows_for(f"{plugin_api.CC_ID_PREFIX}{name}"),
                         "cc-switch 区里还留着那一行")
        row = self._rows_for(target_id)[0]
        self.assertEqual("providers", row.get("source"), "行上的来源标记没变成 providers:")
        self.assertIs(False, row.get("discover_models"))
        self.assertIs(True, row.get("has_api_key"))
        self.assertIs(False, row.get("api_key_plaintext"), "迁移后的行仍被报成明文密钥")
        self.assertEqual(len(expected_ids), row.get("allowlist_count"),
                         "契约补丁字段 allowlist_count 与磁盘白名单不符")
        self.assertEqual(sorted(expected_ids), sorted(row.get("allowlist_models") or []),
                         "契约补丁字段 allowlist_models 与磁盘白名单不符")
        self.assertEqual(row["allowlist_count"], len(row["allowlist_models"]),
                         "两个派生字段各说一套（同一条来源链该保证的事）")

    def test_migrating_every_plaintext_legacy_entry_leaves_only_auxiliary(self):
        """R11 的自动化证据（T15「不再有明文 key」的**实测边界**）：把副本里**所有**带明文的
        legacy 条目**逐条**迁完（一次一条请求 = 逐条显式确认，**没有**批量迁移）后：

        * ``custom_providers:`` 段里**一条明文都没有**（这才是 T15 那句「不再有明文 key」在
          本模块责任范围内的可证形式）；
        * 全文 ``sk-`` 的残留数 == 迁移前「``custom_providers`` 之外」的 ``sk-`` 数 ——
          ⚠️ **不是 0**：本机 ``auxiliary.vision.api_key`` 与 ``bailian`` 的 key 是**同一把**
          字符串（R11 原文），迁移只该摘 legacy 那一份，辅助槽那一份**照旧留着**；
        * ``auxiliary.*`` 自己带明文的槽数**一字未变**（迁移不动它，只额外提示）。
        """
        raw_before = _raw()
        plaintext_entries = _legacy_plaintext_entries(raw_before)
        targets = [str(entry.get("name") or "").strip() for entry in plaintext_entries]
        secrets = [str(entry.get("api_key") or "") for entry in plaintext_entries]
        self.assertTrue(targets, "副本里没有带明文的 legacy 条目，R11 的残留判据无从验证")
        text_before = _read_copy_text("config.yaml")
        before_aux = _auxiliary_plaintext_slots(raw_before)
        outside_sk = _sk_count_outside_custom_providers(raw_before)

        for name in targets:                        # ← 逐条，不做批量（决议 9）
            response = self._migrate({"name": name})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(plugin_api.KEY_DISPOSITION_PLAINTEXT,
                             response.json()["migrated"]["key_disposition"])

        raw_after = _raw()
        text_after = _read_copy_text("config.yaml")
        self.assertEqual([], _legacy_plaintext_entries(raw_after),
                         "custom_providers: 段里还留着明文 key")
        for secret in set(secrets):
            self.assertEqual(_expected_secret_residual(text_before, raw_before, secret, targets),
                             text_after.count(secret),
                             "该 key 串的残留次数不对：legacy 那几份必须消失、别的槽不许动")
        self.assertEqual(outside_sk, text_after.count("sk-"),
                         "迁完 legacy 后残留的 sk- 数不等于「custom_providers 之外」的 sk- 数（R11）")
        self.assertEqual(before_aux, _auxiliary_plaintext_slots(raw_after),
                         "迁移顺手改了 auxiliary.*（R11 明确：迁移不动它，只额外提示）")

    def test_migrated_entry_keeps_carried_fields(self):
        """M6.1 的「搬家不丢家当」：官方 ``CustomEndpointUpdate`` 表达不了的字段
        （本机 ``bailian`` 的 ``api_mode``、``models:`` 的 ``name:`` 子字段）必须被带过去。

        依据：官方合并策略自己的说明（``config_env.py:464-468``「rebuilding from scratch silently
        dropped them」）+ ``models_map`` 只认键名（``:479-487``）。
        取材优先 ``bailian``，拿不到就退到「任一带额外字段或 ``models:`` 子字段的 legacy 条目」。
        """
        def extras(entry: Dict[str, Any]) -> Dict[str, Any]:
            return {key: value for key, value in entry.items()
                    if key not in plugin_api.MIGRATE_OFFICIAL_OWNED_FIELDS}

        candidates = [entry for entry in _raw_legacy()
                      if str(entry.get("name") or "").strip()
                      and (extras(entry) or _disk_models_subfields(entry))]
        self.assertTrue(candidates,
                        "副本里的 legacy 条目没有任何需要额外搬的字段，本判据无从验证")
        preferred = [entry for entry in candidates
                     if str(entry.get("name") or "").strip() == PREFERRED_AUX_HIT_NAME]
        legacy_before = (preferred or candidates)[0]
        name = str(legacy_before.get("name") or "").strip()
        target_id = plugin_api._custom_endpoint_id(name)
        carried = extras(legacy_before)
        subfields_before = _disk_models_subfields(legacy_before)

        response = self._migrate({"id": f"{plugin_api.CC_ID_PREFIX}{name}"})
        self.assertEqual(200, response.status_code, response.text)
        entry_after = _provider_entry(_raw(), target_id)
        for key, value in carried.items():
            self.assertIn(key, entry_after, f"迁移把 legacy 的 {key} 字段弄丢了")
            self.assertEqual(value, entry_after.get(key), f"迁移改写了 legacy 的 {key} 字段")
        subfields_after = _disk_models_subfields(entry_after)
        for model_id, subfields in subfields_before.items():
            if model_id in subfields_after:
                self.assertEqual(subfields, subfields_after[model_id],
                                 f"models:{model_id} 的子字段在搬家中被丢了")

    # ───────────────────────────────────────── M6.7：单次原子写（核心不变式）

    def test_save_config_called_exactly_once_per_migration(self):
        """M6.7① 核心不变式：一次迁移 = ``save_config`` **恰好一次**（monkeypatch 计数）。

        这条判据的存在理由就是 §7.6 头注那个 v0.2 bug：走官方 route 会 load+save 两次，
        第二次把第一次写进 ``providers:`` 的条目覆盖掉 ⇒ 条目凭空消失。
        """
        name = self._migrate_sample()
        target_id = plugin_api._custom_endpoint_id(name)
        with mock.patch.object(plugin_api, "save_config",
                               wraps=plugin_api.save_config) as counter:
            response = self._migrate({"id": f"{plugin_api.CC_ID_PREFIX}{name}"})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(1, counter.call_count,
                         "迁移不是单次原子写（>1 次 save_config 会把刚写进去的条目盖掉）")
        self.assertTrue(_provider_entry(_raw(), target_id),
                        "单次写之后 providers: 条目却没落盘")

    def test_precheck_writes_no_bytes_and_reports_the_same_plan(self):
        """M6.3 的只读半边：``precheck_only=true`` 一次都不写盘，且报的判据与真跑一致。

        为什么非要一个只读分支：「辅助任务槽仍持有一份明文 key」必须在用户点「仍然迁移」
        **之前**出现在弹窗里（M6.3），所以判据不能只挂在写回执上。
        """
        hit_name = self._aux_sample(want_hit=True)
        miss_name = self._aux_sample(want_hit=False)
        config_before = _read_copy_bytes("config.yaml")
        env_before = _read_copy_bytes(".env")

        hit = self._precheck({"name": hit_name})
        self.assertEqual(200, hit.status_code, hit.text)
        payload = hit.json()
        self.assertIs(True, payload.get("precheck_only"))
        self.assertIn(plugin_api.MIGRATE_WARN_AUX_REF, payload.get("precheck_warnings") or [],
                      "R11 命中（auxiliary.* 引用 custom:<name>）却没在 precheck 里报警")
        self.assertEqual([], payload.get("endpoints"), "precheck 不该返回整个列表（白读一遍盘）")
        self.assertIn(hit_name, _legacy_names(), "precheck 之后原条目竟然不在 custom_providers: 里")

        miss = self._precheck({"name": miss_name})
        self.assertEqual(200, miss.status_code, miss.text)
        self.assertEqual([], miss.json().get("precheck_warnings"),
                         "R11 未命中的条目被报 warnings（假告警会让用户不敢迁）")

        self.assertEqual(config_before, _read_copy_bytes("config.yaml"), "precheck 写了 config.yaml")
        self.assertEqual(env_before, _read_copy_bytes(".env"), "precheck 写了 .env")
        # 同一份判据在真跑的响应里也在（弹窗看到的与真正落盘的不分叉）
        response = self._migrate({"name": hit_name})
        self.assertEqual(200, response.status_code, response.text)
        self.assertIn(plugin_api.MIGRATE_WARN_AUX_REF,
                      response.json().get("precheck_warnings") or [],
                      "真跑的响应没带 precheck 那份告警")

    def test_save_config_failure_keeps_entry_and_leaves_config_untouched(self):
        """M6.7② / T20：模拟 ``save_config`` 抛错 → 条目**不丢**、config.yaml 字节不变。

        实测的官方时序：``_write_custom_endpoint:499`` 的 ``save_env_value`` 发生在
        ``save_config`` **之前**，所以这一路上密钥已经在 ``.env`` 里了；条目本身仍完整留在
        ``custom_providers:`` ⇒ 「providers: 与 custom_providers: 至少有一份存在」自动成立，
        重试即可补齐，不存在两边都没有的窗口。
        """
        name = self._migrate_sample()
        target_id = plugin_api._custom_endpoint_id(name)
        env_var = plugin_api.custom_endpoint_key_env(target_id)
        secret = next(str(entry.get("api_key") or "") for entry in _raw_legacy()
                      if str(entry.get("name") or "").strip() == name)
        config_before = _read_copy_bytes("config.yaml")

        with mock.patch.object(plugin_api, "save_config",
                               side_effect=RuntimeError("模拟 save_config 抛错")):
            with self.assertRaises(RuntimeError):
                self._migrate({"id": f"{plugin_api.CC_ID_PREFIX}{name}"})

        self.assertEqual(config_before, _read_copy_bytes("config.yaml"),
                         "save_config 失败却把 config.yaml 改了（原子性破了）")
        raw_after = _raw()
        self.assertTrue(_provider_entry(raw_after, target_id) or name in _legacy_names(raw_after),
                        "T20：迁移中途失败后 providers: 与 custom_providers: 两边都没有该条目")
        self.assertIn(name, _legacy_names(raw_after),
                      "写盘失败时原条目应完好留在 custom_providers:（不丢数据）")
        self.assertEqual(_sha(secret), _sha(_env_value(_read_copy_text(".env"), env_var)),
                         "save_config 失败后 .env 里没有那把 key（那才是真的丢了密钥）")
        self.assertEqual(1, len(self._rows_for(f"{plugin_api.CC_ID_PREFIX}{name}")),
                         "失败后列表里那条 cc-switch 行不见了（用户会以为条目被删了）")

    # ───────────────────────────────── M6.2 / M6.8：边界四条，每条都零写入

    def test_id_collision_aborts_and_writes_zero_bytes(self):
        """M6.8 / §7.6 边界②：``_custom_endpoint_id(name)`` 撞既有 ``providers:`` 键
        → **409 中止**、副本**两个文件一字节都不动**（不静默覆盖，也不静默搬走别人）。
        """
        name = SYNTH_COLLISION_NAME
        target_id = plugin_api._custom_endpoint_id(name)
        _add_provider_entry(target_id, {
            "name": "占位：已经存在的标准条目", "base_url": "https://taken.invalid/v1",
            "model": "zz-taken-model", "models": {"zz-taken-model": {}},
            "discover_models": False, "key_env": "ZZ_TAKEN_KEY_ENV"})
        _add_legacy_entries([_synth_legacy_entry(name, api_key="zz-m6-synthetic-plaintext-key")])
        self._refresh()
        config_before = _read_copy_bytes("config.yaml")
        env_before = _read_copy_bytes(".env")

        response = self._migrate({"name": name})
        self.assertEqual(409, response.status_code, response.text)
        _assert_present(self, "不静默覆盖", response.json().get("detail") or "",
                        "409 的 detail 没说明中止理由（边界② 要的就是「不静默覆盖」）")
        self.assertEqual(config_before, _read_copy_bytes("config.yaml"), "冲突场景写了 config.yaml")
        self.assertEqual(env_before, _read_copy_bytes(".env"),
                         "冲突场景写了 .env（官方 save_env_value 必须根本没被调用）")
        keeper = _provider_entry(_raw(), target_id)
        self.assertEqual("zz-taken-model", keeper.get("model"), "既有 providers: 条目被改写了")
        self.assertEqual("ZZ_TAKEN_KEY_ENV", keeper.get("key_env"), "既有条目的 key_env 被换了")
        self.assertIn(name, _legacy_names(_raw()), "冲突场景顺手把 legacy 原条目删了")

    def test_precheck_reports_collision_without_aborting_the_dialog(self):
        """边界② 在 precheck 里只**报**（``target_id_collision``），不当 409 打死弹窗。"""
        name = SYNTH_COLLISION_NAME
        target_id = plugin_api._custom_endpoint_id(name)
        _add_provider_entry(target_id, {
            "name": "占位", "base_url": "https://taken.invalid/v1",
            "model": "zz-taken-model", "models": {"zz-taken-model": {}}})
        _add_legacy_entries([_synth_legacy_entry(name, api_key="zz-m6-synthetic-plaintext-key")])
        self._refresh()
        config_before = _read_copy_bytes("config.yaml")
        env_before = _read_copy_bytes(".env")

        response = self._precheck({"name": name})
        self.assertEqual(200, response.status_code, response.text)
        payload = response.json()
        self.assertIs(True, payload.get("target_exists"))
        self.assertEqual(target_id, payload.get("target_id"))
        self.assertIn(plugin_api.MIGRATE_WARN_ID_COLLISION, payload.get("precheck_warnings") or [])
        self.assertEqual(config_before, _read_copy_bytes("config.yaml"), "precheck 冲突分支写了盘")
        self.assertEqual(env_before, _read_copy_bytes(".env"), "precheck 冲突分支写了 .env")

    def test_same_name_multi_match_aborts_and_writes_zero_bytes(self):
        """§7.6 边界①：同名同端点多条 → 409、零写入（与 M4 钉住同一条「报错不动手」语义）。"""
        _add_legacy_entries([
            _synth_legacy_entry(SYNTH_DUPLICATE_NAME, api_key="zz-m6-duplicate-a"),
            _synth_legacy_entry(SYNTH_DUPLICATE_NAME, api_key="zz-m6-duplicate-b"),
        ])
        self._refresh()
        config_before = _read_copy_bytes("config.yaml")
        env_before = _read_copy_bytes(".env")

        response = self._migrate({"name": SYNTH_DUPLICATE_NAME})
        self.assertEqual(409, response.status_code, response.text)
        _assert_present(self, "不猜目标", response.json().get("detail") or "",
                        "多条同名的 detail 没说明「要先手工清理」")
        self.assertEqual(config_before, _read_copy_bytes("config.yaml"), "边界① 写了 config.yaml")
        self.assertEqual(env_before, _read_copy_bytes(".env"), "边界① 写了 .env")
        # 给了第二定位键也仍然多条（base_url 相同 ⇒ 精确定位救不回来）
        second = self._migrate({"name": SYNTH_DUPLICATE_NAME, "base_url": SYNTH_BASE_URL})
        self.assertEqual(409, second.status_code, second.text)
        self.assertEqual(config_before, _read_copy_bytes("config.yaml"),
                         "带 base_url 的重试分支写了盘")

    def test_key_env_only_entry_migrates_and_leaves_env_untouched(self):
        """§7.6 边界③：原条目只有 ``key_env`` 无明文 → 直接迁移，**``.env`` 零字节变化**，
        并且那份引用要原样落在新条目上（不带走引用 = 把密钥配置弄丢）。
        """
        key_env = "ZZ_M6_SYNTHETIC_KEY_ENV"
        _add_legacy_entries([_synth_legacy_entry(SYNTH_KEY_ENV_ONLY_NAME, key_env=key_env)])
        self._refresh()
        fixture_config = _read_copy_bytes("config.yaml")
        env_before = _read_copy_bytes(".env")

        response = self._migrate({"name": SYNTH_KEY_ENV_ONLY_NAME})
        self.assertEqual(200, response.status_code, response.text)
        migrated = response.json().get("migrated") or {}
        self.assertEqual(plugin_api.KEY_DISPOSITION_KEY_ENV, migrated.get("key_disposition"))
        self.assertEqual(key_env, migrated.get("env_var_name"),
                         "回执该报出密钥本来所在的那个变量名（弹窗要念给用户）")

        entry_after = _provider_entry(
            _raw(), plugin_api._custom_endpoint_id(SYNTH_KEY_ENV_ONLY_NAME))
        self.assertEqual(key_env, entry_after.get("key_env"), "key_env 引用没搬到新条目上")
        self.assertNotIn("api_key", entry_after, "只有 key_env 的条目被迁移后凭空多出明文")
        self.assertEqual(env_before, _read_copy_bytes(".env"),
                         "边界③ 写了 .env（本来就没有明文可搬）")
        self.assertNotEqual(fixture_config, _read_copy_bytes("config.yaml"),
                            "迁移没落盘（providers: 里该多出一条）")
        self.assertNotIn(SYNTH_KEY_ENV_ONLY_NAME, _legacy_names(_raw()),
                         "边界③ 迁移后原条目没摘掉")

    def test_env_template_entry_migrates_without_materializing_the_expanded_secret(self):
        """§7.6 边界④：原条目 ``api_key`` 是 ``${VAR}`` 模板 → **不物化**。

        实测的官方行为（判据来源，不是愿望）：
          * ``_config_api_key_is_env_ref``（``config_env.py:354``）只查 ``providers:``
            （``_raw_provider_api_key:348`` 走 ``read_raw_config()["providers"]``），对 legacy
            条目**永远 False** ⇒ 官方那条「明文才搬 .env」的兜底对 legacy 不成立，
            模板判据必须由插件自己下（M6.2④ 的原话）；
          * ``load_config()`` 会把 ``${VAR}`` 展开（本用例用 ``os.environ`` 供一个**假**值），
            照 v0.3 的写法（``api_key = 展开值``）就会 ``save_env_value`` 把展开值抄进
            ``HERMES_CUSTOM_<ID>_API_KEY`` —— 正是官方 docstring 想避免的事；
          * ``body.api_key=None`` ⇒ 官方三个分支全不命中（合并的 providers 条目里没有明文），
            ``.env`` 一字节都不写；引用由插件原样写回 ``providers:`` 的 ``api_key``，
            并且 ``save_config`` 的 ``_preserve_env_ref_templates`` 不会把它替换成展开值。
        """
        template = f"${{{SYNTH_TEMPLATE_VAR}}}"
        _add_legacy_entries([_synth_legacy_entry(SYNTH_TEMPLATE_NAME, api_key=template)])
        os.environ[SYNTH_TEMPLATE_VAR] = SYNTH_TEMPLATE_EXPANDED
        self._refresh()
        # 前置：load_config() 确实把模板展开了（否则「不物化」这条判据是空转）
        with _config_profile_scope(None):
            expanded_sequence = load_config().get("custom_providers") or []
        self.assertTrue(any(isinstance(entry, dict)
                            and SYNTH_TEMPLATE_EXPANDED == str(entry.get("api_key") or "")
                            for entry in expanded_sequence),
                        "前置失败：load_config() 没展开 ${VAR}，本用例的判据无从验证")
        env_before = _read_copy_bytes(".env")

        response = self._migrate({"name": SYNTH_TEMPLATE_NAME})
        self.assertEqual(200, response.status_code, response.text)
        migrated = response.json().get("migrated") or {}
        self.assertEqual(plugin_api.KEY_DISPOSITION_TEMPLATE, migrated.get("key_disposition"))
        self.assertIsNone(migrated.get("env_var_name"),
                          "模板条目不该报出一个新的 .env 变量名（那意味着要新建变量）")

        entry_after = _provider_entry(
            _raw(), plugin_api._custom_endpoint_id(SYNTH_TEMPLATE_NAME))
        self.assertEqual(template, entry_after.get("api_key"),
                         "providers: 条目没把 ${VAR} 模板原样带过去（被展开值替换了？）")
        self.assertNotIn("key_env", entry_after, "模板条目被凭空补了 key_env（等于新建 .env 变量）")
        config_text = _read_copy_text("config.yaml")
        env_text = _read_copy_text(".env")
        _assert_absent(self, SYNTH_TEMPLATE_EXPANDED, config_text,
                       "展开后的明文被写进了 config.yaml")
        _assert_absent(self, SYNTH_TEMPLATE_EXPANDED, env_text,
                       "展开后的明文被物化进了 .env（边界④ 正是这件事）")
        self.assertEqual(env_before, _read_copy_bytes(".env"), "边界④ 写了 .env 的字节")

    def test_no_key_entry_migrates_and_reports_no_key(self):
        """边界③/④ 的退化情形：既无明文也无 ``key_env`` → 照样搬，回执 ``no_key``（不谎报）。"""
        _add_legacy_entries([_synth_legacy_entry(SYNTH_NOKEY_NAME)])
        self._refresh()
        env_before = _read_copy_bytes(".env")

        response = self._migrate({"name": SYNTH_NOKEY_NAME})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(plugin_api.KEY_DISPOSITION_NONE,
                         response.json()["migrated"]["key_disposition"])
        self.assertEqual(env_before, _read_copy_bytes(".env"), "没密钥的条目迁移却写了 .env")
        entry_after = _provider_entry(_raw(), plugin_api._custom_endpoint_id(SYNTH_NOKEY_NAME))
        self.assertNotIn("key_env", entry_after)
        self.assertNotIn("api_key", entry_after)

    # ------------------------------------------------------------------ 路由契约

    def test_migrate_route_signature_and_path_contract(self):
        """契约（**对 M7/M8 冻结**）：路由 ``POST /endpoints/migrate``、形参
        ``(body, profile=None)``、``_write_response`` 复用 + 无密钥材料。"""
        signature = inspect.signature(plugin_api.migrate_endpoint)
        self.assertEqual(["body", "profile"], list(signature.parameters))
        self.assertIsNone(signature.parameters["profile"].default)
        self.assertEqual("/endpoints/migrate", plugin_api.MIGRATE_PATH)
        routes = {(tuple(sorted(route.methods)), route.path) for route in plugin_api.router.routes}
        self.assertIn((("POST",), "/endpoints/migrate"), routes,
                      f"路由表没挂上 migrate：{sorted(path for _m, path in routes)}")
        # 请求体形状（M6 定义、对 M7/M8 冻结）
        fields = set(plugin_api.EndpointMigrateRequest.model_fields)
        self.assertEqual({"id", "name", "base_url", "precheck_only"}, fields)

        name = self._migrate_sample()
        secret = next(str(entry.get("api_key") or "") for entry in _raw_legacy()
                      if str(entry.get("name") or "").strip() == name)
        response = self._migrate({"id": f"{plugin_api.CC_ID_PREFIX}{name}"})
        self.assertEqual(200, response.status_code, response.text)
        payload = response.json()
        for key in _walk_keys(payload):
            self.assertNotIn(key, FORBIDDEN_RESPONSE_KEYS, "响应里出现了密钥材料字段名")
        _assert_absent(self, secret, json.dumps(payload, ensure_ascii=False),
                       "迁移响应里回传了密钥内容（铁律：密钥不出网关）")
        # 顶层形状（M4/M5 已冻结的那一半不许漂）
        for field in ("ok", "id", "endpoints", "current", "migrated", "precheck_warnings"):
            self.assertIn(field, payload, f"迁移响应缺字段 {field}（M7/M8 的读契约）")
        self.assertIsInstance(payload.get("precheck_warnings"), list)

    def test_rejection_faces_write_nothing(self):
        """四种拒绝面（400 空 body / 400 只有前缀 / 404 查无此条目 / 404 拿 providers: id 来迁）
        全部**零写入**，与 M4/M5 的「报错不动手」同一口径。"""
        config_before = _read_copy_bytes("config.yaml")
        env_before = _read_copy_bytes(".env")

        empty = self.client.post(MIGRATE_PATH, json={})
        self.assertEqual(400, empty.status_code, empty.text)
        bare = self.client.post(MIGRATE_PATH, json={"id": plugin_api.CC_ID_PREFIX})
        self.assertEqual(400, bare.status_code, bare.text)
        missing = self.client.post(MIGRATE_PATH, json={"name": "zz-m6-no-such-legacy-entry"})
        self.assertEqual(404, missing.status_code, missing.text)
        providers_id = sorted(_raw_providers().keys(), key=str)[0]
        wrong_section = self.client.post(MIGRATE_PATH, json={"id": providers_id})
        self.assertEqual(404, wrong_section.status_code, wrong_section.text)

        self.assertEqual(config_before, _read_copy_bytes("config.yaml"), "拒绝分支写了 config.yaml")
        self.assertEqual(env_before, _read_copy_bytes(".env"), "拒绝分支写了 .env")

    def test_unknown_profile_is_refused_and_writes_nothing(self):
        """R8 的作用域半边：未知 profile 必须**如实失败**，不能悄悄写默认 profile 的条目。"""
        self._guard_before_write()
        name = self._migrate_sample()
        config_before = _read_copy_bytes("config.yaml")
        env_before = _read_copy_bytes(".env")
        response = self.client.post(MIGRATE_PATH, params={"profile": "definitely-not-a-profile"},
                                    json={"name": name})
        self.assertGreaterEqual(response.status_code, 400, response.text)
        self.assertLess(response.status_code, 500, response.text)
        self.assertEqual(config_before, _read_copy_bytes("config.yaml"),
                         "未知 profile 却写了默认 profile 的副本（R8 破防）")
        self.assertEqual(env_before, _read_copy_bytes(".env"), "未知 profile 却写了 .env")
        self.assertIn(name, _legacy_names(_raw()), "未知 profile 把条目迁走了")

    def test_migrating_twice_is_404_and_creates_no_duplicate(self):
        """迁完再迁一次 → 404（原条目已不在 ``custom_providers:``），**不会**多出第二份。"""
        name = self._migrate_sample()
        target_id = plugin_api._custom_endpoint_id(name)
        self.assertEqual(200, self._migrate({"name": name}).status_code)
        providers_before = sorted(_raw_providers().keys(), key=str)
        config_before = _read_copy_bytes("config.yaml")

        second = self._migrate({"name": name})
        self.assertEqual(404, second.status_code, second.text)
        # 改动记录：2026-09-25 R3（M10.16，设计 §2.5 + 表 B · B6）：detail 的动作名统一成 R3 的
        # 新词，字面针脚随之从「已经迁移过」换成「已经收编过」；判据意图（得告诉用户这条已经
        # 收走了）与其余断言一字未改。
        _assert_present(self, "已经收编过", second.json().get("detail") or "",
                        "重复收编的 detail 没告诉用户这条已经收走了")
        self.assertEqual(config_before, _read_copy_bytes("config.yaml"), "重复迁移又写了一次盘")
        self.assertEqual(providers_before, sorted(_raw_providers().keys(), key=str),
                         "重复迁移改动了 providers: 的键集合")

    # ───────────────────────────────────────────── 共享 helper 契约（M6 复用他人区块）

    def test_shared_helpers_reused_not_modified(self):
        """M4 / M5 归属的共享 helper 对 M6 冻结（progress §四：只调用不修改）。"""
        locate = inspect.signature(plugin_api._locate_legacy_entries)
        self.assertEqual(["sequence", "name", "base_url"], list(locate.parameters))
        self.assertEqual("", locate.parameters["base_url"].default)
        providers_entry = inspect.signature(plugin_api._locate_providers_entry)
        self.assertEqual(["cfg", "endpoint_id"], list(providers_entry.parameters))
        hints = typing.get_type_hints(plugin_api._locate_providers_entry)
        self.assertEqual(Tuple[Optional[str], Dict[str, Any]], hints["return"])

        cfg = load_config()
        name = self._migrate_sample()
        matches = plugin_api._locate_legacy_entries(cfg.get("custom_providers"), name)
        self.assertEqual(1, len(matches), "M4 的候选列表语义变了（零条 404 / 多条 409 的前提）")
        # 迁移用的定位链与钉住用的**是同一条**：否则 M6 的边界① 与 M4 会各说一套
        with mock.patch.object(plugin_api, "save_config",
                               side_effect=AssertionError("helper 契约用例是纯读，不许写盘")):
            plan = plugin_api._migrate_plan(cfg, read_raw_config(), f"{plugin_api.CC_ID_PREFIX}{name}",
                                            "", strict=False)
        self.assertIs(matches[0], plan["legacy_entry"], "plan 没复用 _locate_legacy_entries 的活引用")
        self.assertEqual(plugin_api._entry_model_ids(matches[0]), plan["models"])
        self.assertEqual(plugin_api._custom_endpoint_id(name), plan["endpoint_id"])
        self.assertEqual(plugin_api._entry_base_url(matches[0]), plan["base_url"],
                         "plan 的 base_url 不是 M4 的 _entry_base_url 同一口径")
        self.assertEqual(len(plan["models"]), plan["allowlist_count"],
                         "allowlist_count 与迁移载荷的 models 条数不是同一个数")

    # ------------------------------------------------------- plugin.js 的静态断言

    def test_plugin_js_migrate_gate_and_copy_lint(self):
        """M6.4 / M6.5 的静态半边（prompt §5.4：由本模块实跑、逐条附命令与原始输出；
        **不算**人工项，也不是渲染证据）：

        * 入口闸门：卡片动作行里「高级」位**只在 ``isCcSwitchRow(row)`` 分支之内**，且卡片内
          迁移入口恰好一个调用点 ⇒ ``providers:`` 卡片拿不到该按钮（M6.4「入口放高级里」）；
        * 代价表文案（§3.2 UC-10 逐字）+ R11 那句「辅助任务槽仍持有一份明文 key」都在位；
        * 「行位置会变」两处各归一处常量：弹窗的 ``MIGRATE_MOVE_LINE`` 与完成态的
          ``MIGRATE_DONE_TEMPLATE``（M6.5 / §2.5 发现 3）；
        * 本模块只有 **两个** mutation（precheck + migrate）⇒ 没有批量迁移的实现位。
          ⚠️ 这一条与文案 / JSX / 颜色那几条一律按 **M6 区块自己的范围**判（头注之后 →
          下一个模块区块头之前），**不切到文件尾** —— 否则后续模块追加 mutation 会被当成
          「M6 里长出的第三处」（M7 上报的那件事）；
        * 三条硬边界：无 JSX 标签（按**去注释后的代码**判）、颜色一律 ``var(--ui-*)``、
          前端不手工拼 ``?profile=``（**全站**判据，但同样按去注释后的代码判 —— 与 M7 的
          :class:`PluginJsM7LintTest` 终检同口径）；
        * 铁律 3 的禁语（「不提供任何模型」）全文件零命中；
        * 回归护栏（缺陷修复 round 1）：``useMigrateToStandard`` 的 ``return {}`` 里
          ``confirmOpen`` 与 ``setConfirmOpen`` **两个都得在** —— 少了前者本区的
          ``ConfirmDialog`` 因 ``open`` 恒为 undefined 而**永不打开**（真机踩过）。
        """
        source = (DESKTOP_DIR / "plugin.js").read_text(encoding="utf-8")
        marker = "区块 M6：cc-switch 条目迁移"
        self.assertIn(marker, source, "找不到 M6 区块头注（前端区块被改动或挪了位置）")
        # 切片边界：**M6 区块头结束之后 → 下一个模块区块头（M7）开始之前**（不是文件尾）。
        # 起点取头注的 ``*/`` 之后：marker 在那条块注释**内部**，不这样切的话
        # `_strip_js_comments` 看不见开头 `/*`，那段注释会漏进「代码」里污染 JSX / 颜色判据。
        # 终点切在下一个区块头：M6 只替 M6 自己的代码买单，后续模块（M7 / M8 / …）的正常追加
        # 不会再被这里的 mutation 计数与禁语判据误伤（见 :func:`_module_block_tail`）。
        tail = _module_block_tail(source, marker)
        tail_code = _strip_js_comments(tail)
        # 「不手工拼 ?profile=」是**全站**判据（§2.6 C2 已结案），整份文件都要判，但只判
        # **去注释后的代码**：后续模块很可能在注释里**引用**这个查询串来解释「为什么前端不拼」
        # （改动记录不是活文案），拿原文判就会把说明本身判成违规 —— 与 M7 的
        # `PluginJsM7LintTest.test_no_hardcoded_colour_jsx_or_manual_profile_anywhere` 同口径。
        source_code = _strip_js_comments(source)

        # ① 闸门：卡片动作行内「高级」位受 isCcSwitchRow 保护
        card_start = source.index("function ProviderCardActions")
        card_body = source[card_start:source.index("function ProviderCard({", card_start)]
        self.assertEqual(1, card_body.count("isCcSwitchRow(row)"),
                         "卡片里 cc-switch 闸门不唯一（迁移入口的判据必须只有一处）")
        self.assertEqual(1, card_body.count("onClick: () => requestMigrateToStandard(row),"),
                         "卡片里的迁移入口不是恰好一个调用点")
        self.assertLess(card_body.index("isCcSwitchRow(row)"),
                        card_body.index("onClick: () => requestMigrateToStandard(row),"),
                        "迁移调用点不在 isCcSwitchRow 闸门之后 ⇒ providers: 卡片也会拿到该按钮")
        self.assertLess(card_body.index("isProvidersSource(row)"),
                        card_body.index("isCcSwitchRow(row)"),
                        "M2/M5 的动作项被挪到了 M6 之后（纪律：只追加、不重排他人区块）")
        self.assertIn("function isCcSwitchRow(row) {", source)
        self.assertIn("return textOf(row && row.source) === CC_SOURCE", source)

        # ② 走的是同一条失效路径 / 同一个 detail 出口（M4.8 口径），且只挂一次
        self.assertIn("body: { base_url: target.base_url, id: target.id, precheck_only: true }",
                      tail_code)
        self.assertIn("body: { base_url: target.base_url, id: target.id },", tail_code)
        self.assertIn("${SAVE_ENDPOINTS_PATH}/migrate", tail_code,
                      "迁移路径没复用 M4 的 SAVE_ENDPOINTS_PATH 常量")
        self.assertIn("queryClient.invalidateQueries({ queryKey: ENDPOINTS_QUERY_KEY })", tail_code,
                      "M6.5：迁移后没走同一条列表失效路径")
        self.assertIn("backendErrorDetail(error), MIGRATE_FAILED_TOAST_TITLE", tail_code,
                      "失败没透传后端 detail 原文")
        self.assertIn("haptic('success')", tail_code)
        self.assertEqual(2, tail_code.count("useMutation({"),
                         "M6 区块里出现了第三个 mutation（批量迁移不该存在）")
        self.assertEqual(1, source.count("function MigrateZone"), "MigrateZone 的定义不唯一")
        self.assertEqual(1, source.count("jsx(MigrateZone"), "MigrateZone 的挂载点不是恰好一处")
        self.assertEqual(1, tail_code.count("onClick: () => actions.askMigrate(row),"),
                         "本区的逐行按钮出现第二个调用点")
        for forbidden in ("migrateAll", "batchMigrate", "MIGRATE_BATCH", "撤销迁移", "解除迁移"):
            self.assertNotIn(forbidden, tail_code, f"M6 不该有 {forbidden}（决议 9 / 无 un-migrate）")

        # ③ 文案在位（§3.2 UC-10 草案逐字 + M6.3 的 R11 那句 + 决议 10 的代价三条）
        # 改动记录：2026-09-25 R3（M10.16，设计 §2.5 + §2.3 表 A · A17 / A18 / A19）：本区入口的
        # 两条标签与动作 Tip 已按批准终稿改成 R3 的新词（「收编」「收编进本插件」），旧入口串
        # 不再是标签文案，故前两项针脚同步换成新串；弹窗正文那几条（代价三条 / R11 那句）属
        # 豁免档（本轮裁定 D2），针脚与 plugin.js 原文都逐字未动。
        for copy in ("收编进本插件", "仍然迁移", "收编",
                     "辅助任务槽仍持有一份明文 key",
                     "无法再编辑、删除或启用", "Hermes 托管",
                     "不要迁移——直接钉住就够了"):
            self.assertIn(copy, tail, f"M6 的文案少了：{copy}")
        self.assertIn("位置会变", _js_const_source(tail, "MIGRATE_MOVE_LINE"),
                      "弹窗少了「行位置会变」那一句（M6.5）")
        self.assertIn("位置会变", _js_const_source(tail, "MIGRATE_DONE_TEMPLATE"),
                      "迁移完成状态少了「行位置会变」（M6.5：两处各说明一次）")

        # ④ 三条硬边界（按去注释后的代码判，注释里的 `cc:<name>` 不算 JSX）
        self.assertIsNone(re.search(r"<[A-Za-z][A-Za-z0-9]*[\s/>]", tail_code),
                          "M6 区块里出现了 JSX 标签（磁盘插件无编译）")
        for color in re.findall(r"color:\s*'([^']+)'", tail_code):
            self.assertTrue(color.startswith("var(--ui-"),
                            f"M6 区块出现了非主题色：{color}")
        self.assertNotIn("?profile=", source_code,
                         "前端手工拼了 profile 参数（§2.6 C2 已结案：不该拼）")
        self.assertNotIn(FORBIDDEN_POOL_COPY, source, "出现了铁律 3 禁止的「池里一个都不给」说法")

        # ⑤ 回归护栏（缺陷修复 round 1，真机确认）：本区弹窗的 `open` 读自
        # `actions.confirmOpen`（`:3399`），所以 **hook 的 return 必须把 `confirmOpen` 这个
        # state 本身一起导出**。此前 `useMigrateToStandard` 只导出了 `setConfirmOpen` ⇒
        # `open` 恒为 undefined ⇒ 点「迁移到标准形态」什么都不发生、零 console 报错、零 toast
        # （M5 的 `useClearAllowlist` 当时同一个因；M7 的 `useDangerActions` 是对的，照它抄）。
        # ⚠️ 静态判据、不是渲染证据（prompt §5.4）。
        def assert_confirm_state_exported(hook_name: str) -> None:
            """`hook_name` 的 `return {}` 必须同时导出 `confirmOpen` 与 `setConfirmOpen`。

            前提（全文件有 `ConfirmDialog` 读 `actions.confirmOpen`）一旦消失就**响**而不是
            悄悄跳过 —— 永真的护栏比没有护栏更坏（与本方法「找不到区块头注即 fail」同口径）。
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

        assert_confirm_state_exported("useMigrateToStandard")


if __name__ == "__main__":
    unittest.main()

