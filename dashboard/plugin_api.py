"""supplier-models 后端基座（M1，**只读**）：profile 透传 + ``providers:`` / ``custom_providers:`` 双读。

薄包装原则（设计 §6.3）：``providers:`` 一段整个交给官方
``list_custom_endpoints``（``hermes_cli/web_routers/config_env.py:530``），插件只补官方看不到的
``custom_providers:``（cc-switch 按 Hermes v11 旧 schema 写回的 legacy 序列，§2.5 发现 1），
再把两类条目归一化成同构行。官方 ``_custom_endpoint_response`` 只遍历 ``cfg["providers"]``，
所以 legacy 段必须自己读，否则用户的 sensenova / bailian 会在页面上"凭空消失"。

三条不可退让的口径：

* **profile 原样透传**（§6.3 v0.4 F5 / R8）：官方函数签名全是 ``(…, profile: Optional[str] = None)``，
  ``None`` = 无覆盖 = 读写进程级默认 profile。前端 ``ctx.rest`` 已 profile-aware（``api/plugins.ts:85``
  的 ``profileScoped()``），所以 R8 的落地条件在**本文件的路由形参**，不在前端拼参数。
* **密钥铁律**（§6.3「密钥处理铁律」）：响应里关于密钥**只回** ``has_api_key`` / ``api_key_plaintext``
  两个布尔；官方行自带的 ``api_key_preview``（``_api_key_display:331`` 的 redact 预览）在归一化时
  一并剥掉——密钥内容不进网关。
* **明文判据只看未展开原值**（§6.3 v0.4 F9）：``load_config()`` 已把 ``${VAR}`` 展开，用展开值判
  会把「密钥本来在 .env 里、config 只写模板」的条目误报成明文密钥；判据一律取
  ``read_raw_config()`` 的磁盘原值。

本文件是共享 helper ``_cc_switch_rows`` 的归属方（progress §四 / prompt §5.6），后续模块
（M3 密钥回退、M6 迁移）只调用不修改，故**函数名与签名按设计 §6.3 冻结**。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter

from hermes_cli.config import load_config, read_raw_config
from hermes_cli.config_providers import find_provider_entry
from hermes_cli.web_routers.config_env import list_custom_endpoints
from hermes_cli.web_server_profiles import _config_profile_scope

router = APIRouter()

# legacy 行的 id 前缀：providers: 条目的 id 可能与 legacy 条目的 name 撞车，
# 同 id 会让前端 key 冲突 / 动作指错条目（§6.3）
CC_ID_PREFIX = "cc:"
# legacy 条目的运行时身份是 custom:<name>（R12；实测 picker 返回 slug='custom:bailian'）
CURRENT_PROVIDER_PREFIX = "custom:"
# 密钥内容一律不出网关：官方行的 redact 预览在这里被剥掉，只留两个布尔
KEY_MATERIAL_KEYS = ("api_key_preview", "api_key")


def _raw_key_is_plaintext(raw_key: Any) -> bool:
    """磁盘**未展开**的 ``api_key`` 有内容且不是 ``${VAR}`` 模板 → 明文密钥落在 config.yaml。"""
    value = str(raw_key or "").strip()
    return bool(value) and not value.startswith("${")


def _cc_switch_rows(cfg: Dict[str, Any], raw_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """读 ``custom_providers:`` 序列（cc-switch 写入的形态），归一化成与官方行同构的结构。

    ``raw_cfg`` 必须来自 ``read_raw_config()``：判「是否明文密钥」只能看**未展开**的原值。
    ``is_current`` 在这里一律留 ``False``，由调用方按 ``model.provider`` 判定（R12 要求
    ``<name>`` 与 ``custom:<name>`` 两种写法都比对，见 :func:`_apply_is_current`）。
    """
    raw_by_name = {str(e.get("name") or "").strip(): e
                   for e in (raw_cfg.get("custom_providers") or []) if isinstance(e, dict)}
    rows = []
    for entry in (cfg.get("custom_providers") or []):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        base_url = str(entry.get("base_url") or entry.get("url") or "").strip()
        if not name or not base_url:
            continue
        raw_entry = raw_by_name.get(name) or {}
        models = entry.get("models")
        ids = list(models) if isinstance(models, (dict, list)) else []
        rows.append({
            # id 带来源前缀：providers: 条目的 id 可能与 legacy 条目的 name 撞车，
            # 同 id 会让前端 key 冲突 / 动作指错条目
            "id": f"{CC_ID_PREFIX}{name}", "name": name, "base_url": base_url,
            "model": str(entry.get("model") or (ids[0] if ids else "")),
            "models": ids,
            "discover_models": entry.get("discover_models", True),   # 未设 = 默认 True
            "has_api_key": bool(entry.get("api_key") or entry.get("key_env")),
            # ⚠ 明文密钥只报「有/无」，绝不回传内容；判据用 raw 值——
            # load_config() 已展开 ${VAR}，用展开值判会把模板误报成「明文密钥在 config.yaml」
            "api_key_plaintext": bool(str(raw_entry.get("api_key") or "").strip())
                                 and not str(raw_entry.get("api_key") or "").strip().startswith("${"),
            "is_current": False,   # 由调用方按 model.provider 判定
            "source": "cc-switch",
        })
    return rows


def _providers_key_is_plaintext(raw_cfg: Dict[str, Any], endpoint_id: Any) -> bool:
    """``providers:`` 行的同款判据：磁盘原值里有非模板 ``api_key`` 才算明文。

    官方页写的条目正常只有 ``key_env``（密钥在 .env），判出来就是 False。
    """
    try:
        _stored, entry = find_provider_entry((raw_cfg or {}).get("providers"), endpoint_id)
    except Exception:      # 判据失败只降级成「不报明文」，绝不让只读列表 500
        return False
    return _raw_key_is_plaintext(entry.get("api_key")) if isinstance(entry, dict) else False


def _normalize_provider_row(row: Dict[str, Any], api_key_plaintext: bool) -> Dict[str, Any]:
    """官方行 → 与 ``_cc_switch_rows`` 同构：剥掉一切密钥材料，补上 ``api_key_plaintext``。"""
    safe = {key: value for key, value in row.items() if key not in KEY_MATERIAL_KEYS}
    safe["api_key_plaintext"] = bool(api_key_plaintext)
    return safe


def _current_provider_spellings(current_provider: Any) -> set:
    """``model.provider`` 的等价写法集合：原样 + 剥掉 ``custom:`` 前缀（R12）。"""
    current = str(current_provider or "").strip().lower()
    if not current:
        return set()
    spellings = {current}
    if current.startswith(CURRENT_PROVIDER_PREFIX):
        bare = current[len(CURRENT_PROVIDER_PREFIX):].strip()
        if bare:
            spellings.add(bare)
    return spellings


def _apply_is_current(rows: List[Dict[str, Any]], current_provider: Any) -> List[Dict[str, Any]]:
    """就地写 ``is_current``：``<name>`` 与 ``custom:<name>`` 两种写法都要比对（R12）。

    ``providers:`` 行的运行时身份是 dict key（靠 alias 才认 display name），legacy 行是
    ``custom:<name>``；两处都拿 id / name 去比剥前缀后的集合，谁命中谁就是当前供应商。
    """
    spellings = _current_provider_spellings(current_provider)
    for row in rows:
        candidates = set()
        for key in ("id", "name"):
            value = str(row.get(key) or "").strip().lower()
            if value:
                candidates.add(value)
                if value.startswith(CURRENT_PROVIDER_PREFIX):
                    candidates.add(value[len(CURRENT_PROVIDER_PREFIX):].strip())
                elif value.startswith(CC_ID_PREFIX):
                    candidates.add(value[len(CC_ID_PREFIX):].strip())
        row["is_current"] = bool(candidates & spellings)
    return rows


# ── 契约补丁（编排裁定 2026-09-22；M5 上报的读契约缺口）：新增**整数字段** `allowlist_count` ──
#
# `providers:` 行里的 `models` 出自官方 `_models_from_custom_endpoint_entry`
# （`hermes_cli/web_routers/config_env.py:317-328`），它把条目的 `model:` **无条件
# `insert(0, …)`**，所以那个列表是「磁盘白名单 ∪ 注入的默认模型」的**视图**：磁盘清成 `{}`
# 之后行里仍然挂着默认模型那 1 条，卡片上的「已添加 0 个模型」在前端**算不出来**
# （M5 只能靠本插件自己的写回执 `cleared_models` 说这句话）。
#
# 裁定口径：**既有字段与语义一律冻结**（`models` 继续是注入后的视图，不改名、不改义、不动
# `_cc_switch_rows` 的签名），只**加**一个整数字段 `allowlist_count` = 磁盘上真实记着的白名单
# 条数。M6 迁移后的展示与 M8 R1 的数字同吃这一处。
# `_cc_switch_rows` 写 legacy 行 `source` 的字面值（本补丁的分支判据；那里是唯一的产出方）
CC_SOURCE = "cc-switch"


def _providers_allowlist_count(raw_cfg: Dict[str, Any], endpoint_id: Any) -> int:
    """`providers:` 条目**磁盘上**的白名单条数（不含官方注入的默认模型）。

    计数一律看 `read_raw_config()` 的**未展开原值**：这才是「磁盘上记了几条」，且与 M1
    的明文密钥判据（:func:`_providers_key_is_plaintext`）走同一条 raw 链。
    定位复用官方 `find_provider_entry`（所以 `2070:` 这类被 YAML 读成 int 的 key 也认），
    容忍度复用 :func:`_entry_model_ids`（`models:` 是 dict → 取键、是 list → 取元素、
    其它形状 → `[]`）。**纯读、不重新 load**：`raw_cfg` 由调用方在 profile 作用域里备好。

    定位不到条目 → `0`：官方合成的 `direct-config` 行
    （`config_env.py:403-407`，只由顶层 `model:` 拼出）背后没有 `providers:` 条目，
    它的磁盘白名单本来就是空的。
    """
    try:
        _stored, entry = find_provider_entry((raw_cfg or {}).get("providers"), endpoint_id)
    except Exception:      # 派生字段绝不把只读列表打成 500（与 M1 的判据同一降级口径）
        return 0
    return len(_entry_model_ids(entry))


def _apply_allowlist_count(rows: List[Dict[str, Any]],
                           raw_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """就地写 `allowlist_count`；形状与 :func:`_apply_is_current` 一致（原样返回同一个 rows）。

    * `providers:` / `direct-config` 行：取磁盘真值（见 :func:`_providers_allowlist_count`）。
    * cc-switch 行：:func:`_cc_switch_rows` 的 `models` **就是**磁盘 `entry["models"]` 的键 /
      元素序列（它只在 `model` 字段上兜到 `ids[0]`，**没有**往列表里注入默认模型），
      所以 `len(row["models"])` 已经等于磁盘条数。哪天那条注入出现了，这里要改成数 raw 条目。
    """
    for row in rows:
        if str(row.get("source") or "") == CC_SOURCE:
            row["allowlist_count"] = len(row.get("models") or [])
            continue
        row["allowlist_count"] = _providers_allowlist_count(raw_cfg, row.get("id"))
    return rows


# 契约补丁的**第二处追加**（编排裁定 2026-09-22 follow-up，M5 上报缺口的编辑态半边）：
# 整数只够卡片显数字，**编辑回填**要的是那串 id 本身。`formFromRow` 过去吃 `row["models"]`
# 当「已添加」栏的种子，而那是注入后的视图（磁盘 `{}` 的条目也会带回默认模型那一条），
# 于是「刚清空」的条目一打开编辑器就又被填上默认模型 —— 与 M4.7「『已添加』栏列出当前
# 白名单 = 磁盘真值」相反。故随行再带一个 `allowlist_models`：**磁盘上**记着的白名单 id
# 列表（磁盘顺序）。`allowlist_count` 一律不动，且与本字段同一条来源链，
# 所以 `allowlist_count == len(allowlist_models)` 恒成立（tests 逐行钉死这条等式）。


def _providers_allowlist_models(raw_cfg: Dict[str, Any], endpoint_id: Any) -> List[str]:
    """`providers:` 条目**磁盘上**的白名单 id 列表（磁盘顺序，不含官方注入的默认模型）。

    与 :func:`_providers_allowlist_count` 是**同一条** raw 定位链（官方 `find_provider_entry`）
    加**同一个** `_entry_model_ids`，只是不截成 `len(…)`：两个字段因此不可能各说一套。
    定位不到条目（`direct-config` 行背后没有 `providers:` 条目）/ 形状不是容器 → ``[]``；
    纯读、不重新 load，异常一律降级成 ``[]``（派生字段绝不把只读列表打成 500）。
    """
    try:
        _stored, entry = find_provider_entry((raw_cfg or {}).get("providers"), endpoint_id)
    except Exception:      # 与计数同一个降级口径
        return []
    return _entry_model_ids(entry if isinstance(entry, dict) else None)


def _apply_allowlist_models(rows: List[Dict[str, Any]],
                            raw_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """就地写 `allowlist_models`；形状与分支判据都和 :func:`_apply_allowlist_count` 一致。

    * `providers:` / `direct-config` 行：取 raw 条目的键序 / 元素序（见
      :func:`_providers_allowlist_models`），`direct-config` 与定位不到 → ``[]``。
    * cc-switch 行：行里的 `models` **本来就是**磁盘序列（`_cc_switch_rows:71-72` 不注入默认
      模型），所以与计数同口径地照抄它 —— 但**复刻一份新 list**，两个字段不共享同一个对象，
      免得日后谁就地改 `row["models"]` 把磁盘真值那一份也串了。
    """
    for row in rows:
        if str(row.get("source") or "") == CC_SOURCE:
            row["allowlist_models"] = list(row.get("models") or [])
            continue
        row["allowlist_models"] = _providers_allowlist_models(raw_cfg, row.get("id"))
    return rows


@router.get("/endpoints")
def endpoints(profile: Optional[str] = None):
    """双读端点列表（只读；M1 范围，本模块不写任何配置）。

    返回形状沿用官方 ``list_custom_endpoints`` 的信封，``endpoints`` 换成合并后的行：
    ``providers:`` 在前、``custom_providers:``（cc-switch legacy）在后（M1.4 的合并顺序）。
    ``profile`` 原样透传给官方函数与自己的 profile 作用域（R8）。

    每行除 M1 冻结的字段集外还带**两个磁盘真值派生字段**（整数 `allowlist_count` 与列表
    `allowlist_models`，见本文件末尾方向追加的「契约补丁」注释块）：`models` 是注入默认模型
    后的**视图**，那两个新字段才是磁盘上真实记着的白名单。前端两处各有分工：卡片
    「已添加 N 个模型」吃 `allowlist_count`，编辑回填的「已添加」栏种子吃 `allowlist_models`（M4.7）。
    """
    response = list_custom_endpoints(profile)
    payload = response if isinstance(response, dict) else {}

    # legacy 段要在同一个 profile 作用域里读，否则多 profile 用户会看到别的 profile 的条目
    with _config_profile_scope(profile):
        cfg = load_config()
        raw_cfg = read_raw_config()

    current = payload.get("current") if isinstance(payload.get("current"), dict) else {}
    current_provider = str(current.get("provider") or "")

    rows: List[Dict[str, Any]] = []
    for row in (payload.get("endpoints") or []):
        if not isinstance(row, dict):
            continue
        rows.append(_normalize_provider_row(
            row, _providers_key_is_plaintext(raw_cfg, row.get("id"))))
    rows.extend(_cc_switch_rows(cfg, raw_cfg))
    _apply_is_current(rows, current_provider)
    # 契约补丁（只加字段）：`allowlist_count` = 磁盘白名单条数，`models` 仍是注入后的视图
    _apply_allowlist_count(rows, raw_cfg)
    # 同一补丁的第二处追加（编辑态半边）：`allowlist_models` = 磁盘白名单 id（磁盘顺序）
    _apply_allowlist_models(rows, raw_cfg)
    # 契约补丁 · 第三处（编排裁定 2026-09-26，M12 上报）：只加 managed_from 一个字段，官方行构造器是白名单（config_env.py:366-376 实测），不透传未知字段
    _apply_clone_origin(rows, raw_cfg)

    result = dict(payload)
    result["endpoints"] = rows
    return result


# ═══ 区块 M3（追加）：POST /endpoints/validate + 编辑态密钥回退 ═════════════════════
#
# 设计依据：§3.2 UC-01/UC-02、§6.3（validate 路由与 `_with_stored_key` 骨架）、
# §4「获取模型列表 ≤ 8s，复用官方 `_endpoint_probe_client`」、§10.1 R13（决策 14）。
# 本区块只**追加**，M1 的函数与常量一行未改；M1 的 `CC_ID_PREFIX` / `_raw_key_is_plaintext`
# / `KEY_MATERIAL_KEYS` / `find_provider_entry` 在这里按原样复用（progress §四：只调用不修改）。
#
# 三条口径：
#
# * **不吃 profile**（M3.1，§6.3 v0.4 F5 明说的唯一例外）：官方
#   `validate_custom_endpoint(body)`（`config_env.py:622`）的签名里**没有** profile 形参，
#   本路由因此也不声明——探测是纯只读动作，作用域取进程默认，配置一个字都不写。
# * **回退来的密钥只活在发请求那一跳**（决策 14 / R13）：表单不回填 Key（§3.2 UC-02 官方
#   同口径），而官方只在 `body.api_key` 非空时挂 `Authorization`（`config_env.py:629-631`），
#   不回退就是裸发 → 必然 401 → 页面给出误导性的「API Key 无效或无权限」。取到的值
#   **不回显、不回传前端、不进日志**（与 `_api_key_display:331` 只回 redact 预览同理）。
# * **拿不到就裸探测**（prompt §2.6 C5）：`_scoped_key_env`（`model_switch.py:1614`）在
#   multiplex 无 secret scope 时 fail-closed 返回空串（`UnscopedSecretError` 被它自己吞掉）——
#   本区块把空串当作「无鉴权探测」继续走，**不抛异常**，也**绝不**去读别的 profile 的 key。
#
# 注：这里的 import 写在 M3 区块内而不是并到文件头的 M1 import 段，是为了守住「只追加、
# 不重写他人区块」这条纪律（E402 只是风格提示，插件目录不在任何 lint 门禁内）。
from hermes_cli.model_switch import _scoped_key_env  # noqa: E402
from hermes_cli.web_models import CustomEndpointUpdate  # noqa: E402
from hermes_cli.web_routers.config_env import validate_custom_endpoint  # noqa: E402

PROBE_PATH = "/endpoints/validate"

# 官方 validate 的失败原文（`config_env.py:625-641`）。本区块只**判读**它、不改它，
# 判出来的 `error_kind` 才是前端文案的判据（M3.9）——上游改了句子只会退化成 `unknown`，
# 不会把 401 误报成「不可达」。
PROBE_MESSAGE_NO_URL = "Enter an endpoint URL first."
PROBE_MESSAGE_REJECTED = "The endpoint rejected the API key."
PROBE_MESSAGE_HTTP_PREFIX = "Endpoint returned HTTP "

# `error_kind` 取值表（前端 copyOfProbeFailure() 一一对应）
PROBE_KIND_OK = "ok"
PROBE_KIND_EMPTY = "empty"                 # 通了但 0 条 → 仍允许手动添加（UC-06）
PROBE_KIND_NO_ENDPOINT = "no_endpoint"
PROBE_KIND_AUTH = "auth"                   # 401 / 403
PROBE_KIND_UNREACHABLE = "unreachable"     # 连不上（官方 8s 超时也落在这里）
PROBE_KIND_HTTP = "http"                   # 其它非 2xx
PROBE_KIND_UNKNOWN = "unknown"


def _legacy_providers_entry(sequence: Any, name: Any) -> Optional[Dict[str, Any]]:
    """在 ``custom_providers:`` 序列里按 ``name`` 取条目（大小写不敏感）；没有 → ``None``。

    官方 ``find_provider_entry`` 只管 ``providers:`` 那一段（dict），legacy 是 list，
    所以这里自己扫一遍——匹配口径与 M1 的 ``_cc_switch_rows`` 一致（``str(name).strip()``）。
    """
    wanted = str(name or "").strip().lower()
    if not wanted:
        return None
    for entry in (sequence or []):
        if isinstance(entry, dict) and str(entry.get("name") or "").strip().lower() == wanted:
            return entry
    return None


def _entry_api_key(entry: Optional[Dict[str, Any]], raw_entry: Optional[Dict[str, Any]]) -> str:
    """条目自己带的 ``api_key``：磁盘原值是明文就用原值，是 ``${VAR}`` 模板就用展开值。

    两种来源（``providers:`` / ``custom_providers:``）共用这一处判读，免得口径漂移。
    ``load_config()`` 已经把 ``${VAR}`` 换成真值，模板形态只有拿展开值才可能认证成功。
    """
    raw_value = str((raw_entry or {}).get("api_key") or "").strip()
    if _raw_key_is_plaintext(raw_value):
        return raw_value
    return str((entry or {}).get("api_key") or "").strip()


def _entry_key_env(entry: Optional[Dict[str, Any]], raw_entry: Optional[Dict[str, Any]]) -> str:
    """``key_env`` 指向的环境变量名（原值优先，兼容只写在展开值里的情况）。"""
    for source in (raw_entry, entry):
        value = str((source or {}).get("key_env") or "").strip()
        if value:
            return value
    return ""


def _providers_stored_key(cfg: Dict[str, Any], raw_cfg: Dict[str, Any], endpoint_id: str) -> str:
    """``providers:`` 条目：``key_env`` → ``_scoped_key_env``（.env），拿不到再退条目自带的 ``api_key``。

    条目定位复用官方 ``find_provider_entry``（``config_providers.py:85``：精确命中 → 再扫
    alias），所以前端传来的 id 与磁盘 key 的大小写/别名差异都能认。
    """
    _stored, entry = find_provider_entry((cfg or {}).get("providers"), endpoint_id)
    if not isinstance(entry, dict):
        return ""
    _raw_stored, raw_entry = find_provider_entry((raw_cfg or {}).get("providers"), endpoint_id)
    key_env = _entry_key_env(entry, raw_entry)
    if key_env:
        value = _scoped_key_env(key_env)          # fail-closed：拿不到就是 ""（C5）
        if value:
            return value
    return _entry_api_key(entry, raw_entry)


def _legacy_stored_key(cfg: Dict[str, Any], raw_cfg: Dict[str, Any], name: str) -> str:
    """legacy（``custom_providers:``，cc-switch 写的形态）条目：先取磁盘上的**明文** ``api_key``。

    明文是当前形态的真实分布（§2.5 发现 1：cc-switch 直接把 key 写进 config.yaml）；
    只有条目里没有明文时才依次退到展开值与 ``key_env``。
    """
    entry = _legacy_providers_entry((cfg or {}).get("custom_providers"), name)
    if not isinstance(entry, dict):
        return ""
    raw_entry = _legacy_providers_entry((raw_cfg or {}).get("custom_providers"), name)
    key = _entry_api_key(entry, raw_entry)
    if key:
        return key
    key_env = _entry_key_env(entry, raw_entry)
    return _scoped_key_env(key_env) if key_env else ""


def _stored_key_for(endpoint_id: str) -> str:
    """按 id 形态定位条目并取其已存密钥；**定位不到就返回 ""**（新建路径不回退，M3.11）。

    id 形态（与 M1 `/endpoints` 吐给前端的行 id 同一套写法）：

    * ``cc:<name>`` → ``custom_providers:`` 里按 name 找（legacy 行）
    * 其余（含空串）→ ``providers:`` 里用官方 ``find_provider_entry`` 找
    """
    identity = str(endpoint_id or "").strip()
    if not identity:
        return ""
    try:
        cfg = load_config()
        raw_cfg = read_raw_config()
        if identity.startswith(CC_ID_PREFIX):
            return _legacy_stored_key(cfg, raw_cfg, identity[len(CC_ID_PREFIX):])
        return _providers_stored_key(cfg, raw_cfg, identity)
    except Exception:
        # 回退失败绝不能把探测打成 500：官方拿不到 key 就是裸探测，语义比报错有用。
        # 日志只写 id（不是密钥材料），密钥值在任何分支都不落日志。
        return ""


def _with_stored_key(body: CustomEndpointUpdate) -> CustomEndpointUpdate:
    """M3 归属的共享 helper（progress §四 / prompt §5.6）：**函数名与签名对 M4+ 冻结**。

    ``body.api_key`` 非空 → 原样返回（用户当场填的 Key 永远优先，绝不拿已存值覆盖）。
    为空 → 只有当 ``body.id`` 指向一个**已存在**的条目时，才把该条目的已存密钥装进一份
    **副本**返回（Pydantic ``model_copy``，原 body 不被改，所以响应里没有任何地方能回显它）；
    条目不存在、或密钥取不到（含 ``_scoped_key_env`` 的 fail-closed 空串）→ 原样返回，
    由官方按「无鉴权探测」处理。
    """
    if str(getattr(body, "api_key", "") or "").strip():
        return body
    stored = _stored_key_for(str(getattr(body, "id", "") or ""))
    if not stored:
        return body
    return body.model_copy(update={"api_key": stored})


def _probe_error_kind(result: Dict[str, Any]) -> str:
    """官方探测结果 → 机器可读的失败类别（前端文案的唯一判据，M3.9）。"""
    message = str(result.get("message") or "").strip()
    if result.get("ok"):
        return PROBE_KIND_OK if (result.get("models") or []) else PROBE_KIND_EMPTY
    if result.get("reachable") is False:
        return PROBE_KIND_UNREACHABLE
    if message == PROBE_MESSAGE_NO_URL:
        return PROBE_KIND_NO_ENDPOINT
    if message == PROBE_MESSAGE_REJECTED:
        return PROBE_KIND_AUTH
    if message.startswith(PROBE_MESSAGE_HTTP_PREFIX):
        return PROBE_KIND_HTTP
    return PROBE_KIND_UNKNOWN


def _probe_models(result: Dict[str, Any]) -> List[str]:
    """候选模型 id：去空、去重、保序（官方 ``_parse_model_ids`` 已去空，这里只兜重复）。"""
    seen = set()
    ids = []
    for item in (result.get("models") or []):
        model_id = str(item or "").strip()
        if model_id and model_id not in seen:
            seen.add(model_id)
            ids.append(model_id)
    return ids


@router.post(PROBE_PATH)
async def validate_endpoint(body: CustomEndpointUpdate):
    """「获取模型列表」：包一层官方 ``validate_custom_endpoint(body)``（M3.1 / §6.3）。

    超时按官方口径（``_endpoint_probe_client(url, 8.0)``，``config_env.py:647``），
    前端**不**再加更短的超时把它盖掉（§4 性能行）。

    响应 = 官方四字段 ``{ok, reachable, message, models}`` 原样（``models`` 顺手去重）
    + 派生字段 ``error_kind``；再兜一道：任何密钥材料字段（``api_key`` /
    ``api_key_preview``，见 M1 的 ``KEY_MATERIAL_KEYS``）都不出口。
    形状：``{"ok": bool, "reachable": bool, "message": str, "models": [str, ...],
    "error_kind": "ok|empty|no_endpoint|auth|unreachable|http|unknown"}``。
    """
    result = await validate_custom_endpoint(_with_stored_key(body))
    payload = result if isinstance(result, dict) else {}
    safe = {key: value for key, value in payload.items() if key not in KEY_MATERIAL_KEYS}
    safe["models"] = _probe_models(payload)
    safe["error_kind"] = _probe_error_kind(payload)
    return safe


# ═══ 区块 M4（追加）：写链路 —— POST /endpoints（保存）+ POST /endpoints/{id}/pin（钉住）════
#
# 设计依据：§6.3（路由表 + 「钉住的两条路径」决策 11）、§7.1 / §7.3 / §7.7（落盘结构、
# ``models:`` 用 dict、三条不变式）、§3.2 UC-01/UC-02/UC-03/UC-11、§7.4、§10.1 R1。
# 本区块只**追加**：M1 的 ``endpoints`` / ``_normalize_provider_row`` / ``_cc_switch_rows`` /
# ``CC_ID_PREFIX`` 与 M3 的 ``CustomEndpointUpdate`` import 原样复用（progress §四：只调用不修改）。
#
# 三条语义铁律里本模块占两条，外加「钉住不动 models」这一条：
#
# * **铁律 1（保存必须强制钉住）**：``POST /endpoints`` 在进官方 upsert 之前把
#   ``discover_models`` 改写成 ``False``，**无条件**——与前端「允许自动发现（高级）」那个勾选无关
#   （M3 的 Tip 已把该勾选定义为「不参与落盘」）。不强制的话下一次 live 探测会把白名单
#   覆盖成端点全量目录（§2.3 真相 1 / §7.3）。
# * **铁律 2（``api_key`` 缺省 = 保留，``""`` = 清空）**：保存路径**原样透传** body，
#   既不给它补密钥、也不剥：``None`` → 官方 ``_write_custom_endpoint:497-512`` 保留
#   ``key_env`` 与 ``.env`` 变量（T23 / 防 E6）；``""`` → 官方 ``remove_env_value`` 删 ``.env``
#   变量并摘掉 ``key_env``——**这是预期行为，不是 bug**（M4.10），显式清密钥在 UI 上是
#   独立动作 + 二次确认，不混在普通保存里。
#   注意 M3 的 ``_with_stored_key`` **只服务 validate**（回退来的密钥只活在发请求那一跳），
#   保存路径故意不调用它：否则普通保存会把已存密钥再写一次进 ``.env``，
#   并且让「清空密钥」这条语义永远做不到。
# * **钉住不动 ``models:`` 内容**（M4.6：钉住只阻止未来的覆盖，不清理已灌入的模型）：
#   ``providers:`` 路径按 §6.3 回传**完整字段**，字段**读自磁盘**（不读客户端），
#   ``api_key`` 省略、``make_default`` 恒 ``False``、``context_length`` 不回填
#   （官方从 ``existing`` 原样带走；回填反而会把值多写进默认模型的子字段）；
#   legacy 路径**只**写 ``discover_models`` 一个键。
#
# ⚠️ **「无损」只对 legacy 路径成立**（§6.3 v0.4 / 本任务文件备注）：官方 upsert 会
# ``.rstrip("/")`` base_url、``.strip()`` name、把 ``models`` 重建成 dict，还会把条目上有的
# 明文字段搬进 ``.env``；T22 的一行 diff 判据绑的是 :func:`_pin_legacy_entry_in_place` 这条路。
#
# 与 M5 / M6 的共享口径（progress §六 / prompt §6）：直接写 config 一律
# ``load_config()`` → **定点改字段** → **一次** ``save_config()``。
# :func:`_locate_legacy_entries`（M4 归属的共享 helper，与 M5 的 ``_locate_providers_entry``
# 对称）+ :func:`_pin_legacy_entry_in_place` 就是这条路径的参考实现，M5/M6 沿用同一形状
# 与同一套无损判据（T22）。
from typing import Tuple  # noqa: E402

from fastapi import HTTPException  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from hermes_cli.config import save_config  # noqa: E402
from hermes_cli.web_routers.config_env import upsert_custom_endpoint  # noqa: E402

SAVE_PATH = "/endpoints"
PIN_PATH = "/endpoints/{endpoint_id}/pin"
# 钉住走了哪条路径（响应里的机器可读标记，决策 11）：官方 upsert vs 插件就地改字段
PIN_PATH_PROVIDERS = "providers"
PIN_PATH_LEGACY = "legacy"
# M9.1 / 设计 §1.2.1：**保存**走了哪条通道（响应里的机器可读判据，**不进任何 UI 文案**）。
# ``official`` = 无删除，维持官方 ``upsert_custom_endpoint``（本插件零直写）；
# ``direct``   = 有删除，插件直写（与 M5 清空 / M6 迁移同一条单次原子写通道）。
# 命名对测试冻结（设计 §1.5 的通道断言、M10/M12 的联动都吃这两串）。
SAVE_CHANNEL_OFFICIAL = "official"
SAVE_CHANNEL_DIRECT = "direct"


class EndpointPinRequest(BaseModel):
    """``POST /endpoints/{id}/pin`` 的**可选**请求体（M4 定义，对 M5+ 冻结）。

    只有 ``base_url`` 有语义：它是 legacy 条目的第二定位键（§6.3「按 ``(name, base_url)``
    定位」；``name`` 取 id 的 ``cc:`` 前缀后缀）。``providers:`` 路径**整份忽略**本模型——
    钉住读的字段一律取磁盘现值，不取客户端，免得把「表单里没保存的改动」顺带写进
    config（钉住的语义只是阻止未来覆盖）。
    """

    base_url: str = ""


def _entry_model_ids(entry: Optional[Dict[str, Any]]) -> List[str]:
    """条目 ``models:`` 的 id 列表：dict → 键序，list → 元素序；其它形状 → ``[]``。

    与 M1 ``_cc_switch_rows`` 的 ``ids = list(models) if isinstance(models, (dict, list))``
    同一口径（§7.3 采用 dict，list 是 cc-switch 旧写入与手改配置的兜底）。
    """
    models = (entry or {}).get("models")
    if isinstance(models, dict):
        return [str(key).strip() for key in models if str(key).strip()]
    if isinstance(models, (list, tuple)):
        return [str(item).strip() for item in models if str(item).strip()]
    return []


def _entry_base_url(entry: Optional[Dict[str, Any]]) -> str:
    """条目的端点 URL（``base_url`` → ``url`` 兜底，与 M1 / 官方 ``_custom_endpoint_response`` 同口径）。"""
    return str((entry or {}).get("base_url") or (entry or {}).get("url") or "").strip()


def _write_response(endpoint_id: str, profile: Optional[str],
                    extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """写路由的统一响应：M1 只读路由的归一化结果 + ``ok`` + ``id``（**密钥不出网关**）。

    官方 upsert 自己的返回值带 ``api_key_preview``（``_api_key_display:331`` 的 redact 预览），
    本插件不用它——写完再跑一遍 M1 的 ``endpoints(profile)``，既守住 M1.5 的密钥铁律
    （progress §十五 决策 2：前端不得依赖 ``api_key_preview``），也让前端拿到最新行。
    """
    payload = dict(endpoints(profile) or {})
    payload["ok"] = True
    payload["id"] = endpoint_id
    if extra:
        payload.update(extra)
    return payload


def _providers_pin_body(endpoint_id: str,
                        profile: Optional[str]) -> Tuple[CustomEndpointUpdate, str]:
    """``providers:`` 条目 → 官方 upsert 的**完整字段** body（读自磁盘）＋ 实际存储 key。

    缺 ``name`` / ``base_url`` / ``model`` 时官方在 ``_write_custom_endpoint:444-452`` 抛 400
    （**不是**「用空值覆盖」），所以这里三件套一律从条目现值取；``models`` 取条目全部 id
    （官方 ``models_map`` 只增不减，回传全量才不会让钉住顺手丢模型）。
    条目不存在 → 404，**不动手**。
    """
    with _config_profile_scope(profile):
        cfg = load_config()

    stored, entry = find_provider_entry((cfg or {}).get("providers"), endpoint_id)
    if not isinstance(entry, dict):
        # 改动记录：2026-09-25 R3（M10.9，设计 §2.4 表 B · B4）：detail 改使用者语言，与
        # `_locate_providers_entry` 的「真不存在」分支同一条措辞；被拒的 id 仍写进原文
        # （前端 toast 的判据）。**状态码 404 与判据语义一字未动。**
        raise HTTPException(
            status_code=404,
            detail=f"找不到该供应商 {endpoint_id}（列表可能已过期，请刷新页面）。本动作不会新建供应商。")

    ids = _entry_model_ids(entry)
    model = str(entry.get("model") or entry.get("default_model") or (ids[0] if ids else "")).strip()
    body = CustomEndpointUpdate(
        api_key=None,                                   # 字段省略 = 保留密钥（铁律 2）
        base_url=_entry_base_url(entry),
        context_length=None,                            # 不回填：官方从 existing entry 原样带走
        discover_models=False,                          # 钉住本身就是这件事（铁律 1）
        id=str(stored or endpoint_id),
        make_default=False,                             # 钉住绝不顺手切换当前供应商
        model=model,
        name=str(entry.get("name") or stored or "").strip(),
        models=ids,
    )
    return body, str(stored or endpoint_id)


def _locate_legacy_entries(sequence: Any, name: str,
                           base_url: str = "") -> List[Dict[str, Any]]:
    """在 ``custom_providers:`` 序列里按 ``(name, base_url)`` 取候选条目（M4 归属，M5/M6 可复用）。

    ``name`` 与 M1 ``_legacy_providers_entry`` 同口径（strip + 大小写不敏感）；
    ``base_url`` 非空时再按 URL 过滤（strip + 大小写不敏感，本机 URL 一律小写主机名）。
    返回**候选列表**而不是单条：调用方要区分「零条 → 404」与「多条 → 409」，
    两种都必须**不写**（§6.3「匹配不到或匹配到多条 → 报错不动手」）。
    """
    wanted_name = str(name or "").strip().lower()
    if not wanted_name:
        return []
    wanted_url = _entry_base_url({"base_url": base_url}).lower()
    matches = []
    for entry in (sequence or []):
        if not isinstance(entry, dict):
            continue
        if str(entry.get("name") or "").strip().lower() != wanted_name:
            continue
        if wanted_url and _entry_base_url(entry).lower() != wanted_url:
            continue
        matches.append(entry)
    return matches


def _pin_legacy_entry_in_place(name: str, base_url: str, profile: Optional[str]) -> None:
    """legacy（``custom_providers:``，cc-switch 写的形态）**就地**钉住：只写一个字段。

    ``load_config()`` → 按 ``(name, base_url)`` 定位 → ``entry["discover_models"] = False``
    → **一次** ``save_config()``。条目留在 ``custom_providers:`` 原位置不动（决策 11 / 决议 12：
    对 legacy 条目点「钉住」执行**不是**迁移，cc-switch 的编辑/启用/删除照旧可用）。
    定位不到 / 定位到多条 → 抛错，**不写**。
    """
    wanted = str(name or "").strip()
    wanted_url = _entry_base_url({"base_url": base_url})
    with _config_profile_scope(profile):
        cfg = load_config()
        matches = _locate_legacy_entries((cfg or {}).get("custom_providers"), wanted, wanted_url)
        # 改动记录：2026-09-25 R3（M10.9，设计 §2.4 表 B · B3）：两条 detail 改使用者语言——
        # 不再写存储段名与字段名写法，位置键说成「按（名称, 端点）」，409 保留「不猜目标」与
        # 「先在 cc-switch 里清理重名条目」这条正路；被定位的名称/端点仍写进原文（前端 toast
        # 的判据，`test_pin.py` 钉着）。**状态码 404 / 409 与判据语义一字未动。**
        if not matches:
            raise HTTPException(
                status_code=404,
                detail=(f"在由 cc-switch 管理的供应商里按（名称, 端点）找不到该条目"
                        f"（名称 {wanted}，端点 {wanted_url or '未提供'}）"
                        "——可能已被改名、删除或收编。列表未改动。"))
        if len(matches) > 1:
            raise HTTPException(
                status_code=409,
                detail=(f"在由 cc-switch 管理的供应商里按（名称, 端点）找到 {len(matches)} 条同名同端点的"
                        f"条目（名称 {wanted}）——插件不猜目标，请先在 cc-switch 里清理重名条目再钉住。"
                        "列表未改动。"))
        matches[0]["discover_models"] = False       # 只碰这一个字段
        save_config(cfg)                            # 单次原子写（atomic_yaml_write）


def _allowlist_removal_plan(entry: Dict[str, Any], page_models: List[str],
                            default_model: str) -> List[str]:
    """M9.2（设计 §1.2.1）：磁盘 ``models:`` 里**不在**「本次页面列表 ∪ {默认模型}」的那些 id。

    **纯函数、只读**：``entry`` 是 ``cfg["providers"]`` 里的活条目，这里一个字段都不改
    （真正动刀在 :func:`save_endpoint` 的直写支，随那**一次** ``save_config`` 落盘）。

    * 比较基准走 :func:`_entry_model_ids` —— 与 ``GET /endpoints`` 的 ``allowlist_models``
      **同一条判据链**（dict / list 两形状都容忍），所以「页面上看到的已添加」与
      「删除比较的基准」不可能各说一套（设计 §1.2.2 要点 1）。
    * ``default_model`` **必须**并入 keep：官方 ``_write_custom_endpoint:481`` 把它无条件
      折回池（不变式 3），pop 碰它就是「保存后默认模型从池里消失」的新缺陷。
      于是「已添加栏为空」的保存差集虽覆盖磁盘全部 id，默认模型仍被豁免，落盘结果与
      既有口径逐字一致 —— UI 文案的那句话永远是「只剩默认模型」（E7，池至少含默认模型）。
    """
    keep = {str(m).strip() for m in (*page_models, default_model) if str(m).strip()}
    return [mid for mid in _entry_model_ids(entry) if mid not in keep]


@router.post(SAVE_PATH)
def save_endpoint(body: CustomEndpointUpdate, profile: Optional[str] = None):
    """新建 / 编辑供应商，并且**保存即所得**（M9）：从「已添加」里移掉的模型真从磁盘消失。

    根因（设计 §1.1）：官方 ``_write_custom_endpoint:479-487`` 先把磁盘已有的 ``models``
    **整份抄下**再往上追加 —— **只增不减**，所以「移除」在官方链路上物理做不到
    （「基元律动」实测：下次查看模型又回来了）。本函数按差集分**两条通道**：

    * **无删除**（新建 / 纯新增 / 等值保存，差集为空，M9.4）→ **完全维持现状**走官方
      ``upsert_custom_endpoint(forced, profile)``：它自己 load+save，**本分支插件零直写**
      （只增语义对「新增」本来就是对的，不动它）。
    * **有删除**（M9.5，直写支，与 :func:`migrate_endpoint` 同型）→ ``load_config()``
      → ``find_provider_entry`` 取**活**条目 → :func:`_allowlist_removal_plan` 算差集
      → ``_write_custom_endpoint(cfg, forced)``（**内存调用、不落盘**：key 三支处理
      ``:498-512`` / base_url ``.rstrip("/")`` / slug 规整 ``:439`` / 键搬家 ``:514-515``
      / ``make_default`` 顶层镜像 ``:519-524`` 这五件事**保持官方一份实现**）
      → 对差集键逐个 ``pop``（**只删该删的**；留存键的 ``context_length`` 等子字段
      官方 ``dict(current)`` 已带）→ **一次** ``save_config(cfg)``（单次原子写，
      不存在「删一半」的窗口）。

    **闸门在任何读盘/写盘之前**（M9.3）：``cc:`` 前缀的 id 一律 **400**，零读零写。
    前端 ``saveBlockReason`` / ``SAVE_DISABLED_LEGACY`` 已经拦了一层，**后端不信前端**
    （与 :func:`clear_models_endpoint` 同口径）。顺手封掉现状那个真实口子：官方
    ``_custom_endpoint_id("cc:bailian")`` 会 slug 化成 ``cc-bailian`` 并**新建**一条
    ``providers:`` 影子条目。detail 按 M10 §2.2 规则表起草（无存储字段名、无编号、
    动词「保存」、含「列表未改动」承诺与正路指引），与 :func:`_cc_refusal_detail` 同语。

    **铁律 1**：``discover_models`` 在这里被无条件改写成 ``False``（``model_copy`` 出副本，
    原 body 不动，所以请求日志里也不会留下「前端说要自动发现」的残留语义）——**两条通道吃
    同一份** ``forced``。
    **铁律 2**：``api_key`` 不在这里碰——缺省 = 保留，``""`` = 清空（见本区块头注）。
    ``context_length`` 原样透传（表单里留空 → ``None`` → 官方不写该字段）。
    **不变式 3**：默认模型永远在池里 —— keep 集显式并入 ``model``，``pop`` 也绝不碰它。

    请求体 = 官方 ``CustomEndpointUpdate``（``web_models.py:36``）：
    ``{id, name, base_url, model, api_key?, context_length?, discover_models?, make_default?, models?}``；
    前端 payload 见 ``plugin.js`` 的 ``buildSavePayload``（§7.7 ``FormState`` → snake_case）。
    响应：``{ok, id, endpoints: [...], current: {...}}``（``endpoints`` 即 M1 的归一化行，
    无密钥材料）+ 两个回执字段（M9.6）：``removed_models``（本次真删掉的条数，前端
    「已保存，本次删除 N 个模型」的**唯一**数字来源）与 ``write_channel``
    （``SAVE_CHANNEL_OFFICIAL`` | ``SAVE_CHANNEL_DIRECT``，测试钉链路选择的机器可读判据，
    **不进任何 UI 文案**）。
    """
    identity = str(body.id or "").strip()
    if identity.startswith(CC_ID_PREFIX):
        # M9.3：闸门在**任何读盘 / 写盘之前** ⇒ 被拒的请求一个字节都不落盘，
        # 也不会被 slug 化成 `cc-<name>` 新建一条 providers: 影子条目（设计 §1.2.2 要点 5）。
        bare = identity[len(CC_ID_PREFIX):].strip()
        raise HTTPException(
            status_code=400,
            detail=(f"{bare or identity} 由 cc-switch 管理：它的模型白名单归 cc-switch 所有，"
                    "写了会被它下次编辑覆盖，所以本插件不提供保存。"
                    "要先交给本插件管，请走「收编 → 收编进本插件」；"
                    "否则请回 cc-switch 修改它的模型列表。列表未改动。"))

    forced = body.model_copy(update={"discover_models": False})     # 铁律 1（两条通道同一份）

    with _config_profile_scope(profile):
        cfg = load_config()
        # 比较基准 = **磁盘活条目**，定位与官方写入器同一条链（``_write_custom_endpoint:439``
        # 自己就是 ``_custom_endpoint_id(body.id or body.name)`` → ``find_provider_entry``）。
        # 新建 ⇒ 查无条目 ⇒ 差集为空 ⇒ 走官方支。请求体**不做二次磁盘重读**（设计 §1.2.2 要点 1）：
        # 表单打开后官方页给同一条目加的模型会按页面所见删掉，护栏 = 回执删除数（拍板 #12）。
        _stored, entry = find_provider_entry(
            (cfg or {}).get("providers"), _custom_endpoint_id(forced.id or forced.name))
        removed = (_allowlist_removal_plan(entry, list(forced.models or []), forced.model)
                   if isinstance(entry, dict) else [])

        if not removed:
            # M9.4 无删除：官方 upsert（它自己 load+save），插件侧一次直写都不发生。
            result = upsert_custom_endpoint(forced, profile)
            payload = result if isinstance(result, dict) else {}
            endpoint_id = str(payload.get("id") or "")
            receipt = {"removed_models": 0, "write_channel": SAVE_CHANNEL_OFFICIAL}
        else:
            # M9.5 有删除：内存内调官方写入器 → 只 pop 差集键 → **一次**原子落盘。
            endpoint_id, new_entry = _write_custom_endpoint(cfg, forced)   # 不落盘
            models_map = new_entry.get("models") if isinstance(new_entry, dict) else None
            if isinstance(models_map, dict):
                for model_id in removed:
                    models_map.pop(model_id, None)
            save_config(cfg)                       # ← 单次原子写（不存在「删一半」窗口）
            receipt = {"removed_models": len(removed), "write_channel": SAVE_CHANNEL_DIRECT}

    # M9.6：统一走既有 _write_response（写后重跑 M1 只读列表，密钥不出网关）。
    return _write_response(endpoint_id, profile, receipt)


@router.post(PIN_PATH)
def pin_endpoint(endpoint_id: str, body: Optional[EndpointPinRequest] = None,
                 profile: Optional[str] = None):
    """「钉住」= 把 ``discover_models`` 写成 ``false``（M4.2 / 决策 11，两条路径**必须分开**）。

    * id 不以 ``cc:`` 开头 → ``providers:`` 条目：官方 ``upsert_custom_endpoint``，字段读自磁盘
      （见 :func:`_providers_pin_body`）。⚠️ 这条路**不保证无损**（rstrip / strip / dict 重建）。
    * id 以 ``cc:`` 开头 → legacy 条目：插件自己就地改一个字段
      （见 :func:`_pin_legacy_entry_in_place`）；``base_url`` 取可选 body 作第二定位键。

    两条路都**不碰** ``models:`` 的内容（钉住只阻止未来覆盖，不清理已灌入的模型 —— UC-11 / 决议 3），
    也都**不碰密钥**（``providers:`` 路径 ``api_key=None``；legacy 路径只改一个键）。
    响应同 :func:`_write_response`，另带 ``pin_path`` = ``providers`` | ``legacy`` 与
    ``discover_models``（钉住后的期望值，前端徽章的机器可读依据）。
    """
    identity = str(endpoint_id or "").strip()
    if not identity:
        raise HTTPException(status_code=400, detail="缺少供应商标识（路径里的 {id} 是空的）。")

    if identity.startswith(CC_ID_PREFIX):
        name = identity[len(CC_ID_PREFIX):].strip()
        if not name:
            raise HTTPException(status_code=400,
                                detail=f"id {identity!r} 只有 cc: 前缀、没有条目名，无法定位。")
        base_url = str(getattr(body, "base_url", "") or "")
        _pin_legacy_entry_in_place(name, base_url, profile)
        return _write_response(identity, profile,
                               {"pin_path": PIN_PATH_LEGACY, "discover_models": False})

    forced, stored = _providers_pin_body(identity, profile)
    result = upsert_custom_endpoint(forced, profile)
    payload = result if isinstance(result, dict) else {}
    pinned_id = str(payload.get("id") or stored)
    return _write_response(pinned_id, profile,
                           {"pin_path": PIN_PATH_PROVIDERS, "discover_models": False})


# ═══ 区块 M5（追加）：清空白名单 —— POST /endpoints/{id}/clear-models（第二处直接写 config）═
#
# 设计依据：§3.2 UC-12（**含 v0.4 的写路径修正**）、§6.3 路由表 + 决策 13、
# §7.7 不变式 3 的 v0.4 改写、§2.6 E7、§11 M5。锚点：T19 / T24 / UC-12 / 决策 13。
# 本区块只**追加**：M1 的 ``CC_ID_PREFIX`` / M4 的 ``_entry_model_ids`` /
# ``_entry_base_url`` / ``_locate_legacy_entries`` / ``_write_response`` 原样复用
# （progress §四：共享 helper 只调用不修改），无需新增 import。
#
# 为什么必须自己写 config（R14 / 决策 13）：官方 ``upsert_custom_endpoint`` **只增不减**
# ——``_write_custom_endpoint:479-487`` 先把 ``existing_models`` 整份拷进 ``models_map``
# 再往上加，从不删；官方页也删不掉（``custom-endpoints-settings.tsx:85`` 空列表直接不发
# 该字段）。所以「把被灌进来的全量目录清掉」在 v0.3 的设计里**没有写路径**，
# 只能走：``load_config()`` → 定位 ``providers:`` 条目 → ``entry["models"] = {}``
# → **一次** ``save_config(cfg)``（M4↔M5↔M6 共享口径，progress §六 / prompt §6）。
#
# 四条口径：
#
# * **只写 ``models:`` 一个字段**（M5.2 / 决策 13 派生）：``model:`` / ``discover_models``
#   / ``key_env`` / ``api_key`` 一律不碰。T24 的判据就是「除 models 外整份 config 零改动」。
# * **找不到 → 404，绝不静默新建**（M5.1）：清空动作只能作用在真实存在的条目上；
#   ``providers:`` 的 dict key 集合在调用前后必须完全一致（见 :func:`_locate_providers_entry`）。
# * **只对 ``providers:`` 行开放**（M5.3 / 决议 12 / UC-12 v0.4）：``cc:`` 前缀 id 一律拒绝。
#   理由不是「前端没画按钮」，而是**那些条目的 ``models:`` 归 cc-switch 所有**，插件写进去
#   会被它下次编辑覆盖。前端同样不渲染该按钮，但**后端不信前端**。
# * **清空 ≠ 池里空**（E7 / 不变式 3 的 v0.4 改写）：``_absorb_entry_models``
#   （``model_switch_providers.py:472-482``，prompt §2.6 C8 修正出处）会把条目的 ``model:``
#   无条件折进池，所以正确结果是「``models: {}`` + 池里剩默认模型那一个」。
#   本区块**不写任何 UI 文案**，但语义上要为这一点把关：``model:`` 字段绝不参与本次写入。
#
# 本区块**不需要新的 import**：``load_config`` / ``read_raw_config`` / ``save_config`` /
# ``find_provider_entry`` / ``_config_profile_scope`` / ``HTTPException`` / ``Tuple`` 全部
# 由 M1 与 M4 的 import 段提供（M4 已在 :418 导入 ``Tuple``）。

CLEAR_MODELS_PATH = "/endpoints/{endpoint_id}/clear-models"


def _locate_providers_entry(cfg: Dict[str, Any],
                            endpoint_id: Any) -> Tuple[Optional[str], Dict[str, Any]]:
    """M5 归属的共享 helper（progress §四 / prompt §5.6）：**函数名与签名对 M6/M7 冻结**。

    在 ``providers:`` 那段 **dict** 里按 id 定位条目，定位一律走官方
    ``find_provider_entry``（``config_providers.py:85``：精确命中 → 再按字符串身份扫一遍，
    所以 ``2070:`` 这类被 YAML 读成 int 的 key 也认）。

    返回 ``(stored_key, entry)``：``entry`` 是 ``cfg["providers"]`` 里的**活引用**
    （不是副本），调用方就地改字段后即随 ``save_config(cfg)`` 落盘 —— 这正是
    「定点改字段 → 一次保存」这条口径的实现前提。

    找不到 → 抛 **404**，**绝不静默新建**（M5.1：清空只能作用在真实存在的条目上）。
    纯读 helper：本身不碰磁盘，所以不写副本。
    """
    identity = str(endpoint_id or "").strip()
    stored, entry = find_provider_entry((cfg or {}).get("providers"), identity)
    if isinstance(entry, dict):
        return stored, entry

    # 改动记录：2026-09-25 R3（M10.8，设计 §2.4 表 B · B2）：两条 404 的 detail 改成使用者
    # 语言——存储段名说成「本插件管理的供应商 / 由 cc-switch 管理的供应商」，设计编号去掉，
    # 「不代写」「列表未改动」两条承诺保留；被定位的 id 仍写进原文（`test_clear_models.py`
    # 钉着「错误原文要带上定位不到的 id」）。**状态码 404 与判据语义一字未动。**
    # 定位失败时分两种情形给可执行的 detail：① 该名字其实在由 cc-switch 管理的那段里
    # （它的模型白名单归 cc-switch 所有，本插件不代写）——这时 404 的原文必须说明
    # 「不是找不到，是不该由本插件写」；② 真不存在（列表过期 / 已删除）。
    legacy = _locate_legacy_entries((cfg or {}).get("custom_providers"), identity)
    if legacy:
        detail = (f"本插件管理的供应商里没有 {identity}，但找到 {len(legacy)} 个由 cc-switch 管理的"
                  f"同名供应商（端点 {_entry_base_url(legacy[0]) or '未记录'}）——它们的模型白名单归 "
                  "cc-switch 所有，本插件不代写。列表未改动。")
    else:
        detail = (f"找不到该供应商 {identity}（列表可能已过期，请刷新页面）。本动作不会新建供应商。")
    raise HTTPException(status_code=404, detail=detail)


@router.post(CLEAR_MODELS_PATH)
def clear_models_endpoint(endpoint_id: str, profile: Optional[str] = None):
    """「清空白名单」（UC-12 / 决策 13）：把 ``providers:`` 条目的 ``models:`` 写成 ``{}``。

    流程严格三拍（M5.1，与 :func:`_pin_legacy_entry_in_place` 同一形状）：
    ``with _config_profile_scope(profile)`` → ``cfg = load_config()`` → 定位条目
    （找不到 404，不静默新建）→ ``entry["models"] = {}`` → **一次** ``save_config(cfg)``
    （原子写，无损口径沿用 M4 的 T22 判据）。

    写入范围（M5.2）：**只有 ``models:``**。``model:`` / ``discover_models`` / ``key_env``
    / ``api_key`` 一个都不碰；顶层 ``model:``（当前供应商镜像）同样不动。

    拒绝面（M5.3）：空 id → 400；``cc:`` 前缀 id → 400（且**读 config 之前就**拒绝，
    所以那种请求连一次打开文件都不会发生）。``providers:`` 里查无此 id → 404。
    三种拒绝都不写盘。

    响应：复用 :func:`_write_response` 的 ``{ok, id, endpoints, current}``（M1 归一化行，
    无密钥材料），另带 ``cleared_models``（清掉了几条，供前端回执）与 ``default_model``
    （条目自己的 ``model:``，即「池里仍然只剩它」的那一个 —— E7 / 不变式 3）。
    """
    identity = str(endpoint_id or "").strip()
    if not identity:
        raise HTTPException(status_code=400, detail="缺少供应商标识（路径里的 {id} 是空的）。")
    if identity.startswith(CC_ID_PREFIX):
        bare = identity[len(CC_ID_PREFIX):].strip()
        suffix = "" if bare else "（只有前缀、没有条目名）"
        # 改动记录：2026-09-25 R3（M10.7，设计 §2.4 表 B · B1）：detail 改使用者语言——存储
        # 字段名说成「模型白名单」，设计编号与「只对哪一段开放」那句去掉，所有权与「写了会被
        # 它下次编辑覆盖」这条拒绝原因、「列表未改动」承诺逐字保留；被拒的 id 仍在原文里。
        # **状态码 400 与判据语义一字未动**（上面那条空 id 的 400 也没动）。
        raise HTTPException(
            status_code=400,
            detail=(f"{identity}{suffix} 由 cc-switch 管理的供应商，它的模型白名单归 cc-switch 所有，"
                    "写了会被它下次编辑覆盖，所以本插件不提供清空。列表未改动。"))

    with _config_profile_scope(profile):
        cfg = load_config()
        stored, entry = _locate_providers_entry(cfg, identity)
        cleared_models = len(_entry_model_ids(entry))
        default_model = str(entry.get("model") or entry.get("default_model") or "").strip()
        entry["models"] = {}          # M5.2：只碰这一个字段
        save_config(cfg)              # 单次原子写（atomic_yaml_write）

    return _write_response(str(stored if stored is not None else identity), profile,
                           {"cleared_models": cleared_models, "default_model": default_model})


# ═══ 区块 M6（追加）：cc-switch 条目迁移 —— POST /endpoints/migrate ════════════════
#
# 全项目**唯一搬动明文密钥**的写路径（第三处直接改 config）。设计依据：§3.2 UC-10
# （迁移弹窗 + 代价表文案）、§7.6（迁移细节 + 边界情况表，**v0.3 改写过第 3/4 步**）、
# §2.5 发现 2/3/6（明文密钥是安全问题、迁移必然搬家且行位置会变、cc-switch 保留未知字段
# ⇒ 默认不迁移）、§6.3 路由表、§10.1 R11、§10.2 决议 9/10/13 的派生口径。
# 本区块只**追加**：M1 的 ``CC_ID_PREFIX``、M3 的 ``CustomEndpointUpdate`` /
# ``_entry_key_env``、M4 的 ``_locate_legacy_entries`` / ``_entry_model_ids`` /
# ``_entry_base_url`` / ``_write_response`` 原样复用（progress §四：共享 helper 只调用不修改）。
# 边界② 的 ``providers:`` 冲突判据**不借** M5 的 ``_locate_providers_entry`` —— 那个 helper
# 找不到条目就抛 404，而这里要的恰好是相反语义（「撞上了才中止」），所以直接用官方
# ``find_provider_entry``（同一条定位链，口径不会漂）。
#
# ── 核心不变式：**一次** ``save_config`` ──────────────────────────────────────────
# v0.2 的写法是「先走官方 route 写 ``providers:``，再在手里那份早先 load 的 ``cfg`` 上摘
# ``custom_providers:`` 原条目后 ``save_config``」—— 官方 route 自己已经 load+save 过一次，
# 第二次保存会**把刚写进去的 ``providers:`` 条目覆盖掉**（v0.3 修掉的那个窗口，§7.6 头注）。
# 所以本模块调的是**内部**函数 ``_write_custom_endpoint(cfg, body)``（``config_env.py:438``，
# 返回 ``(endpoint_id, entry)``、**不落盘**），在**同一个** ``cfg`` 上摘掉 legacy 原条目，
# 然后**一次** ``save_config(cfg)``。M6.7 的测试用 monkeypatch 计数器把这件事钉死。
#
# ── ``_write_custom_endpoint`` 对 ``body.api_key`` 实际做了什么（读源码所得）──────────
# ``config_env.py:496-512``，其中 ``env_var = custom_endpoint_key_env(endpoint_id)`` 即
# ``HERMES_CUSTOM_<id 大写、非字母数字折成下划线>_API_KEY``（``config.py:2585``）：
#   * 非空串 → ``save_env_value(env_var, key)``（**当场写 .env，早于任何 config 落盘**）
#     + ``entry["key_env"] = env_var`` + ``entry.pop("api_key")``；
#   * ``""`` → ``remove_env_value(env_var)`` + 摘掉 ``key_env`` 与 ``api_key``（清空密钥）；
#   * 省略（``None``）→ **只有当合并进来的 ``providers:`` 条目自己带明文 ``api_key``**
#     且 ``_config_api_key_is_env_ref(endpoint_id)`` 为假时，才把它搬进 ``.env``。
# 三处后果对本模块是决定性的：
#   ① 合并的 ``existing`` 取自 ``cfg["providers"]``（legacy 条目不在那一段），所以「原条目的
#      明文 key」**不会**被那一条兜住 —— 必须像 §7.6 第 2 步那样把 key 显式装进 body；
#   ② ``_config_api_key_is_env_ref`` 只查 ``providers:``（``:354`` → ``_raw_provider_api_key:348``），
#      对 legacy 条目**永远 False** —— 「``${VAR}`` 模板不许物化」这道判据只能本模块自己下
#      （M6.2④），照 v0.3 的写法就会把展开后的明文再抄一份进用户没要求过的第二个 .env 变量；
#   ③ ``load_config()`` 已经把 ``${VAR}`` 展开，所以密钥形状一律从 ``read_raw_config()`` 的
#      **未展开**原值判（本模块唯一的 raw 用途）。
# 另外两处官方行为如实记下并在此补齐：``models_map`` **只增不减**（``:479-487`` 先拷
# ``existing_models`` 再往上加）且每个键的子 dict 一律新建 ``{}``（**legacy 的 ``name:`` /
# ``context_length:`` 子字段官方不搬**）、base_url 被 ``.rstrip("/")``、name 被 ``.strip()``。
# 前者由 :func:`_carry_unrepresentable_legacy_fields` 在**同一个 cfg**上补（仍在那一次写入里），
# 依据是官方自己对合并策略的说明（``:464-468``「rebuilding from scratch silently dropped
# them」）与 M6 收敛条件「diff 只有条目搬家 + 明文 key 消失，其余字段零改动」；后两条是
# 官方语义，UI 文案照 §2.5 发现 3 说明「行位置会变」。
#
# ── 边界四条（M6.2 / §7.6 表）──────────────────────────────────────────────────────
#   ① ``custom_providers:`` 同名多条（端点也相同）→ **409**、零写入：复用 M4 的
#      :func:`_locate_legacy_entries` 候选列表语义（「匹配不到 404 / 多条报错不动手」）；
#   ② ``_custom_endpoint_id(name)`` 撞上既有 ``providers:`` 键 → **409 中止**、零写入，
#      **绝不静默覆盖**（官方 :514-515 会 ``providers.pop(stored_key)``，那是静默搬掉别人）；
#   ③ 原条目只有 ``key_env`` 无明文 → 直接迁移、**不动 ``.env``**，把那份引用原样带到新条目
#      （不带走引用等于把密钥配置弄丢）；
#   ④ 原条目 ``api_key`` 是 ``${VAR}`` 模板 → **模板照搬、不物化**：``body.api_key=None``
#      （官方因此一个 .env 字节都不写）+ 迁移后把磁盘原样的模板串写回 ``providers:`` 的
#      ``api_key``（官方 ``:354-363`` 的 docstring 就是这个理由）。
#
# ── R11 前置检查（M6.3 / 决议 9 派生）─────────────────────────────────────────────
# 迁移**只能逐条显式确认**（决议 9：不做批量迁移），而「辅助任务槽仍持有一份明文 key」那句
# 话要在用户点「仍然迁移」**之前**出现在弹窗里 ⇒ 判据不能只挂在写回执上。设计 §6.3 只冻结了
# **路由名**、payload 形状归本模块（progress §四 / prompt §5.6 的泳道契约），所以给
# ``POST /endpoints/migrate`` 的 body 加一个 ``precheck_only`` 开关：``true`` 走**纯读**分支
# （``save_config`` 一次都不调）并返回同一份 ``precheck_warnings``；真正的迁移在响应里带
# **同一份**列表 —— 弹窗看到的与真正做的是同一次计算的产物，不可能分叉。
# **没有**新增路由、**没有**动 ``GET /endpoints`` 的行形状。
# 扫描范围：``read_raw_config()`` 的 ``auxiliary.*`` / ``model.*`` 子树里值等于
# ``custom:<name>``（大小写不敏感）的字符串。本机实测命中：``auxiliary.vision.provider`` =
# ``custom:bailian`` 且该槽自己带明文 key（R11 原文）⇒ ``bailian`` 是 hit、``sensenova`` 是 miss。
#
# ── 与 M7 的接口（progress §六 / prompt §6「M6 ↔ M7」）────────────────────────────
# 迁移成功之后条目才落到 ``providers:``，从此**才**拿到启用/删除的资格（决议 12）。
# M7 的按钮显隐判据继续吃 ``row["source"] == "providers"``：迁移响应的 ``id`` 就是新的
# ``providers:`` key，``migrated.from_id`` 是迁移前的 ``cc:<name>`` 行 id。
from copy import deepcopy  # noqa: E402
import re  # noqa: E402

from hermes_cli.config import custom_endpoint_key_env  # noqa: E402
from hermes_cli.web_routers.config_env import (  # noqa: E402
    _custom_endpoint_id,
    _write_custom_endpoint,
)

MIGRATE_PATH = "/endpoints/migrate"

# §7.6 边界④ 的模板判据：与官方 ``_config_api_key_is_env_ref``（``config_env.py:363``）
# **同一个正则**（``search``，不是 ``startswith``）。刻意与 M1 的 ``_raw_key_is_plaintext``
# 不等价：``sk-x${VAR}`` 这种混合值 M1 会报「明文密钥」（那是告警，宁可多报），
# 本模块按**模板**处理（这是搬密钥，宁可不动）。
ENV_REF_TEMPLATE_RE = re.compile(r"\$\{[^}]+\}")
# legacy 条目的运行时身份写法（R12）
AUX_CUSTOM_REF_PREFIX = "custom:"

# ``precheck_warnings`` 的取值表（M6.3 的机器可读判据；前端文案一一对应）
MIGRATE_WARN_AUX_REF = "aux_custom_ref"              # auxiliary.* 引用 custom:<name>
MIGRATE_WARN_MODEL_REF = "model_custom_ref"          # 顶层 model.* 引用 custom:<name>
MIGRATE_WARN_ID_COLLISION = "target_id_collision"    # 边界②（真跑 409，precheck 只报）
# 密钥的去向（响应里的机器可读标记；**任何一项都不带密钥内容**）
KEY_DISPOSITION_PLAINTEXT = "plaintext_to_env"       # 明文 → .env（官方 save_env_value）
KEY_DISPOSITION_TEMPLATE = "template_carried"        # ${VAR} 模板原样进 providers:，.env 零字节
KEY_DISPOSITION_KEY_ENV = "key_env_carried"          # 只有 key_env → 引用照搬，.env 零字节
KEY_DISPOSITION_NONE = "no_key"                      # 既无明文也无 key_env
# 迁移时归**官方写入路径 / 本模块显式处置**独占的字段：这些键一律不许由 legacy 条目覆盖
# （``url``/``api`` 是 base_url 的同义写法、``default_model`` 是 ``model:`` 的别名、
#  ``api_key_env``/``key_cmd`` 属密钥通道 —— 后两个若照搬会凭空多出一份密钥来源）
MIGRATE_OFFICIAL_OWNED_FIELDS = (
    "name", "base_url", "url", "api", "model", "default_model", "models",
    "discover_models", "api_key", "key_env", "api_key_env", "key_cmd",
)


class EndpointMigrateRequest(BaseModel):
    """``POST /endpoints/migrate`` 的请求体（**M6 定义，对 M7/M8 冻结的形状**）。

    定位键（**至少要给一个**，否则 400）：

    * ``id`` —— 直接吃 :func:`_cc_switch_rows` 给前端的行 id（``cc:<name>`` 或裸 ``<name>``）
    * ``name`` —— 条目名（与 ``id`` 二选一；``id`` 优先）
    * ``base_url`` —— **可选**的第二定位键，与 M4 的 :class:`EndpointPinRequest` 同口径
      （§7.6 边界①「按 ``(name, base_url)`` 精确定位；仍多条则报错让用户先手工清理」）

    ``precheck_only`` —— M6.3 的只读前置检查：``true`` 时**一次 ``save_config`` 都不发生**，
    只返回确认弹窗要用的那一份判据（代价表数据 + ``precheck_warnings``）。
    """

    id: str = ""
    name: str = ""
    base_url: str = ""
    precheck_only: bool = False


def _raw_key_is_env_template(raw_key: Any) -> bool:
    """磁盘**未展开**的 ``api_key`` 里含 ``${...}`` → 这是一份 env-ref 模板，不是明文。"""
    return bool(isinstance(raw_key, str) and ENV_REF_TEMPLATE_RE.search(raw_key))


def _custom_ref_hits(root: Any, reference: str) -> bool:
    """子树里**任何**字符串值等于 ``custom:<name>``（strip + 大小写不敏感）→ 命中。

    刻意不看字段名：``auxiliary.<slot>.provider`` 是本机的实际写法，但别的槽可能把它放在
    别的键上（R11 的原文是「扫描 ``auxiliary.*`` / ``model.*`` 里对 ``custom:<name>`` 的引用」）。
    """
    wanted = reference.strip().lower()
    if not wanted:
        return False
    if isinstance(root, str):
        return root.strip().lower() == wanted
    if isinstance(root, dict):
        return any(_custom_ref_hits(value, reference) for value in root.values())
    if isinstance(root, (list, tuple)):
        return any(_custom_ref_hits(value, reference) for value in root)
    return False


def _migrate_warnings(raw_cfg: Dict[str, Any], name: str) -> List[str]:
    """R11 / M6.3 的前置检查：该 legacy 条目被辅助槽或顶层 ``model:`` 引用时报警。

    判据取 ``read_raw_config()``（磁盘原值）——「仍持有一份明文 key」说的是**落盘**状态，
    合并过默认值、展开过 env-ref 的 ``load_config()`` 不是它的事实来源。
    返回**有序去重**的 code 列表（见 ``MIGRATE_WARN_*`` 常量表）；没有命中就是空列表。
    """
    reference = f"{AUX_CUSTOM_REF_PREFIX}{name}"
    warnings: List[str] = []
    if _custom_ref_hits((raw_cfg or {}).get("auxiliary"), reference):
        warnings.append(MIGRATE_WARN_AUX_REF)
    if _custom_ref_hits((raw_cfg or {}).get("model"), reference):
        warnings.append(MIGRATE_WARN_MODEL_REF)
    return warnings


def _raw_legacy_entry_for(raw_cfg: Dict[str, Any], name: str, index: int,
                          base_url: str) -> Dict[str, Any]:
    """legacy 条目在**未展开** config 里的对应原值（边界④ 的唯一事实来源）。

    定位优先按**下标**（``read_raw_config()`` 与 ``load_config()`` 的 ``custom_providers:``
    是同一个序列的先后次序，下标对齐最准），且必须 ``name`` 也对得上才敢用；
    否则退到按 ``(name, base_url)`` 找唯一一条；再退到按 ``name`` 的第一条；都没有 → ``{}``
    （调用方兜到展开值，但分诊规则不变，所以最坏情况仍是「按模板不动手」）。
    """
    sequence = (raw_cfg or {}).get("custom_providers")
    candidates = [entry for entry in (sequence or []) if isinstance(entry, dict)]
    if 0 <= index < len(candidates):
        positioned = candidates[index]
        if str(positioned.get("name") or "").strip().lower() == str(name or "").strip().lower():
            return positioned
    matches = _locate_legacy_entries(candidates, name, base_url)
    if len(matches) == 1:
        return matches[0]
    by_name = _locate_legacy_entries(candidates, name)
    return by_name[0] if by_name else {}


def _migrate_key_plan(legacy_entry: Dict[str, Any],
                      raw_legacy_entry: Dict[str, Any]) -> Tuple[str, str, str]:
    """边界③④的判据：**从磁盘原值出发**决定密钥怎么走。

    返回 ``(disposition, key_material, key_env_name)``：

    * ``template_carried`` —— 原值是 ``${VAR}`` 模板：``key_material`` 是**那串模板本身**
      （绝不是 ``load_config()`` 的展开值），``body.api_key`` 给 ``None`` ⇒ 官方一个 .env
      字节都不写，迁移后由 :func:`_apply_migrated_key_reference` 把模板原样写回 ``providers:``；
    * ``plaintext_to_env`` —— 原值是非模板明文：装进 body 交给官方，它
      ``save_env_value(HERMES_CUSTOM_<ID>_API_KEY, key)`` + 写 ``key_env`` + 摘掉 ``api_key``；
    * ``key_env_carried`` —— 原值无明文、只有 ``key_env``：``body.api_key=None``，引用照搬；
    * ``no_key`` —— 两份都没有：什么都不搬。
    """
    raw_key = str((raw_legacy_entry or {}).get("api_key") or "").strip()
    if not raw_key:                              # raw 段取不到时兜到展开值，但判据不变
        raw_key = str(legacy_entry.get("api_key") or "").strip()
    if raw_key and _raw_key_is_env_template(raw_key):
        return KEY_DISPOSITION_TEMPLATE, raw_key, ""
    if raw_key:
        return KEY_DISPOSITION_PLAINTEXT, raw_key, ""
    key_env = _entry_key_env(legacy_entry, raw_legacy_entry)
    if key_env:
        return KEY_DISPOSITION_KEY_ENV, "", key_env
    return KEY_DISPOSITION_NONE, "", ""


def _migrate_plan(cfg: Dict[str, Any], raw_cfg: Dict[str, Any], identity: str,
                  base_url: str, *, strict: bool) -> Dict[str, Any]:
    """**纯读**：把「这次迁移要做什么」整个算出来，一个字节都不写。

    真跑与 ``precheck_only`` 共用这一处，所以弹窗里的代价表与真正落盘做的事**不可能分叉**。
    抛错面（两种模式一致，都发生在任何写动作之前）：定位不到 → 404；同名多条 → 409（边界①）。
    ``strict=True``（真跑）时边界② 冲突 → 409；``strict=False``（precheck）时只把它记进
    ``warnings``，让弹窗在用户点「仍然迁移」**之前**就说清楚。
    """
    name = identity[len(CC_ID_PREFIX):].strip() if identity.startswith(CC_ID_PREFIX) else identity
    # 改动记录：2026-09-25 R3（M10.11，设计 §2.4 表 B · B6）：本函数四条 detail 统一替换措辞
    # （存储段名 → 「由 cc-switch 管理的供应商 / 本插件管理的供应商」，动作名统一成「收编」，
    # 设计编号引用去掉）；「不猜目标」「不静默覆盖」「列表未改动」与全部填槽逐字保留。
    # **状态码 400 / 404 / 409 与判据语义一字未动。**
    if not name:
        raise HTTPException(
            status_code=400,
            detail=f"id {identity!r} 只有 cc: 前缀、没有条目名，无法定位要收编的供应商。")

    sequence = (cfg or {}).get("custom_providers")
    matches = _locate_legacy_entries(sequence, name, base_url)
    if not matches:
        raise HTTPException(
            status_code=404,
            detail=(f"在由 cc-switch 管理的供应商里按（名称, 端点）找不到该条目"
                    f"（名称 {name}，端点 {base_url or '未提供'}）"
                    "——可能已被改名、删除，或者已经收编过。列表未改动。"))
    if len(matches) > 1:
        raise HTTPException(
            status_code=409,
            detail=(f"由 cc-switch 管理的供应商里有 {len(matches)} 条名称与端点都相同的条目"
                    f"（名称 {name}）——插件不猜目标，请先在 cc-switch 里清理重名条目再收编。"
                    "列表未改动。"))

    legacy_entry = matches[0]
    legacy_index = next((i for i, item in enumerate(sequence or []) if item is legacy_entry), -1)
    ids = _entry_model_ids(legacy_entry)
    model = str(legacy_entry.get("model") or legacy_entry.get("default_model")
                or (ids[0] if ids else "")).strip()
    entry_base_url = _entry_base_url(legacy_entry)
    raw_entry = _raw_legacy_entry_for(raw_cfg, name, legacy_index, entry_base_url)
    endpoint_id = _custom_endpoint_id(name)

    # 撞车判据：`_custom_endpoint_id(name)` 撞上既有「本插件管理的供应商」的键 → 中止，绝不静默
    # 覆盖。判据走官方自己的 `find_provider_entry`，所以「key 精确命中」与「alias 扫到」都算撞车
    # （官方 :514-515 一旦 stored_key != endpoint_id 就 `providers.pop(stored_key)`，
    #  那等于静默删掉别人的条目，正是「不静默覆盖」这条要挡的事）。
    # 改动记录：2026-09-25 R3（M10.11，表 B · B6）：detail 里的存储段名与设计编号按批准口径替换，
    # 「不静默覆盖」「列表未改动」与填槽保留；**状态码 409 与判据语义一字未动**。
    collision_stored, collision_entry = find_provider_entry(
        (cfg or {}).get("providers"), endpoint_id)
    collision = isinstance(collision_entry, dict)
    warnings = _migrate_warnings(raw_cfg, name)
    if collision:
        warnings.append(MIGRATE_WARN_ID_COLLISION)
        if strict:
            raise HTTPException(
                status_code=409,
                detail=(f"收编会写到本插件管理的供应商里的 {endpoint_id}，但那里已经有 "
                        f"{collision_stored!r} —— 已中止，不静默覆盖。列表未改动。"))

    disposition, key_material, key_env_name = _migrate_key_plan(legacy_entry, raw_entry)
    # `env_var_name` = 「密钥所在/将落的那个 .env 变量名」（**只有名字，永远没有值**）：
    # 明文那一支是官方即将写的 `HERMES_CUSTOM_<ID>_API_KEY`，key_env 那一支是本来就在用的
    # 那个变量；模板与「根本没有密钥」两种没有名字可报（弹窗文案各自另有一句）。
    env_var_name = None
    if disposition == KEY_DISPOSITION_PLAINTEXT:
        env_var_name = custom_endpoint_key_env(endpoint_id)
    elif disposition == KEY_DISPOSITION_KEY_ENV:
        env_var_name = key_env_name or None
    return {
        "allowlist_count": len(ids),
        "base_url": entry_base_url,
        "discover_models": bool(legacy_entry.get("discover_models", True)),
        "endpoint_id": endpoint_id,
        "env_var_name": env_var_name,
        "from_id": f"{CC_ID_PREFIX}{name}",
        "key_disposition": disposition,
        # ⚠️ 只有 `plaintext_to_env` 会被装进 body 交给官方写 .env；`template_carried` 的
        # `key_material` 是**未展开的模板串**，只会由本模块原样写回 providers:。
        "key_env": key_env_name,
        "key_material": key_material,
        "legacy_entry": legacy_entry,
        "legacy_index": legacy_index,
        "legacy_models": (legacy_entry.get("models")
                          if isinstance(legacy_entry.get("models"), dict) else {}),
        "model": model,
        "models": list(ids),
        "name": name,
        "target_exists": bool(collision),
        "warnings": warnings,
    }


def _carry_unrepresentable_legacy_fields(entry: Dict[str, Any], plan: Dict[str, Any]) -> None:
    """把 ``CustomEndpointUpdate`` **表达不了**、官方合并又会丢的 legacy 字段搬到新条目上。

    只补 ``entry`` 里当前**没有**的键（官方刚写的 ``name``/``base_url``/``model``/
    ``discover_models``/``models`` 一律赢），所以这不是「用 legacy 覆盖官方」，而是
    「搬家别把家当落下」：

    * ``api_mode``（本机 ``bailian`` 就是 ``chat_completions``）、``context_length``、
      ``request_timeout_seconds``、``extra_headers``…… 官方自己的合并注释
      （``config_env.py:464-468``）承认重建会把它们静默丢掉；
    * ``models:`` 的**子字段**（本机 sensenova 每条带 ``name:``）：官方 ``models_map``
      （``:479-487``）对每个键新建 ``{}``，所以按 ``setdefault`` 语义补回去。

    仍**不搬**的是 ``MIGRATE_OFFICIAL_OWNED_FIELDS`` 里的一切（含密钥通道 ``api_key`` /
    ``key_env`` —— 那两个由 :func:`_apply_migrated_key_reference` 显式处置）。
    """
    legacy_entry = plan.get("legacy_entry") or {}
    for key, value in legacy_entry.items():
        if key in MIGRATE_OFFICIAL_OWNED_FIELDS or key in entry:
            continue
        entry[key] = deepcopy(value)

    models_map = entry.get("models")
    legacy_models = plan.get("legacy_models") or {}
    if isinstance(models_map, dict):
        for model_id, subfields in legacy_models.items():
            if not isinstance(subfields, dict):
                continue
            target = models_map.get(model_id)
            if not isinstance(target, dict):
                continue
            for key, value in subfields.items():
                if key not in target:
                    target[key] = deepcopy(value)


def _apply_migrated_key_reference(entry: Dict[str, Any], plan: Dict[str, Any]) -> None:
    """边界③④的落地：``body.api_key`` 为 ``None`` 时官方什么都不写，引用由这里补。

    * ``template_carried`` → ``entry["api_key"] = "${VAR}..."``（**磁盘原样**，展开值绝不出现
      在这条路径上）+ 摘掉 ``key_env`` ⇒ ``.env`` 零字节变化，密钥仍只在它原来那个变量里；
    * ``key_env_carried`` → ``entry["key_env"] = <legacy 的变量名>`` + 摘掉 ``api_key``；
    * ``plaintext_to_env`` / ``no_key`` → **什么都不补**：前者官方已经写了 ``key_env`` 并摘掉
      明文，后者本来就没有密钥。
    """
    disposition = str(plan.get("key_disposition") or "")
    if disposition == KEY_DISPOSITION_TEMPLATE:
        entry["api_key"] = str(plan.get("key_material") or "")
        entry.pop("key_env", None)
    elif disposition == KEY_DISPOSITION_KEY_ENV:
        entry["key_env"] = str(plan.get("key_env") or "")
        entry.pop("api_key", None)


def _migrate_body(plan: Dict[str, Any]) -> CustomEndpointUpdate:
    """§7.6 第 2 步的 ``CustomEndpointUpdate``（组装口径逐字照任务书 M6.1）。

    ``discover_models=False`` 是**铁律 1**（搬家不许顺手允许 live 探测）；
    ``make_default=False``（迁移**不是**切换当前供应商，顶层 ``model:`` 镜像一个字节都不动 ——
    引用了它的那部分由 R11 的告警交给人自己决定）；``context_length=None``（官方从合并的
    existing entry 原样带走，回填反而会多写默认模型的子字段）。
    ``api_key`` 只在「明文进 .env」那一种处置下才给值，其余一律 ``None`` = 官方不碰 ``.env``。
    """
    disposition = str(plan.get("key_disposition") or "")
    return CustomEndpointUpdate(
        api_key=(str(plan.get("key_material") or "")
                 if disposition == KEY_DISPOSITION_PLAINTEXT else None),
        base_url=str(plan.get("base_url") or ""),
        context_length=None,
        discover_models=False,
        id=str(plan.get("endpoint_id") or ""),
        make_default=False,
        model=str(plan.get("model") or ""),
        name=str(plan.get("name") or ""),
        models=list(plan.get("models") or []),
    )


def _migrate_summary(plan: Dict[str, Any], endpoint_id: str) -> Dict[str, Any]:
    """响应的 ``migrated`` 段：弹窗那份代价表数据 + 迁移结果（**不含任何密钥材料**）。

    ``env_var_name`` 是**环境变量名**（不是值），与 ``_cc_switch_rows`` 的
    ``api_key_plaintext`` 同量级的信息；密钥内容 / 展开值 / 哈希在任何字段与日志里都不出现。
    ``discover_models`` 报的是**迁移后**的值（铁律 1 恒 ``False``），legacy 条目原来钉没钉
    在 ``discover_models_before`` —— 两个字段分开，M7/M8 才不会把「迁移前的未钉住」读成结果。
    """
    return {
        "allowlist_count": plan.get("allowlist_count"),
        "base_url": plan.get("base_url"),
        "discover_models": False,
        "discover_models_before": plan.get("discover_models"),
        "env_var_name": plan.get("env_var_name"),
        "from_id": plan.get("from_id"),
        "key_disposition": plan.get("key_disposition"),
        "model": plan.get("model"),
        "models": list(plan.get("models") or []),
        "name": plan.get("name"),
        "removed_from_custom_providers": True,
        "to_id": endpoint_id,
    }


def _precheck_payload(plan: Dict[str, Any]) -> Dict[str, Any]:
    """``precheck_only=true`` 的响应（**零写入**；M6.3 的确认弹窗读这一段）。

    ``discover_models`` / ``discover_models_before`` 的分工与 :func:`_migrate_summary` 一致
    （前者 = 迁移后必然是 ``false``，后者 = 该 legacy 条目此刻的钉住状态）。
    """
    return {
        "allowlist_count": plan.get("allowlist_count"),
        "base_url": plan.get("base_url"),
        "current": {},
        "discover_models": False,
        "discover_models_before": plan.get("discover_models"),
        "endpoints": [],
        "env_var_name": plan.get("env_var_name"),
        "id": plan.get("from_id"),
        "key_disposition": plan.get("key_disposition"),
        "model": plan.get("model"),
        "models": list(plan.get("models") or []),
        "name": plan.get("name"),
        "ok": True,
        "precheck_only": True,
        "precheck_warnings": list(plan.get("warnings") or []),
        "target_exists": bool(plan.get("target_exists")),
        "target_id": plan.get("endpoint_id"),
    }


@router.post(MIGRATE_PATH)
def migrate_endpoint(body: Optional[EndpointMigrateRequest] = None,
                     profile: Optional[str] = None):
    """把一条 ``custom_providers:``（cc-switch）条目搬到 ``providers:``（UC-10 / §7.6）。

    签名：``POST /endpoints/migrate``，body 见 :class:`EndpointMigrateRequest`，
    ``profile`` 形参进 ``_config_profile_scope``（R8；官方 :func:`_write_custom_endpoint`
    本身不吃 profile，所以本模块**没有**走任何官方 route —— 走了就是两次 load/save）。

    写序（M6.1 的四拍，一次 ``save_config``）：``load_config()`` → :func:`_migrate_plan`
    （纯读；边界①② 在这里就抛错，**零写入**）→ :func:`_migrate_body` +
    ``_write_custom_endpoint(cfg, body)``（**不落盘**，但明文那一支会当场 ``save_env_value``
    写 ``.env``）→ :func:`_carry_unrepresentable_legacy_fields` +
    :func:`_apply_migrated_key_reference` → 从 ``cfg["custom_providers"]`` 摘掉原条目
    → **一次** ``save_config(cfg)``。

    成功响应：:func:`_write_response` 的 ``{ok, id, endpoints, current}``（``id`` = **新的**
    ``providers:`` key；``endpoints`` = M1 归一化行、无密钥材料）+ ``migrated{…}`` +
    ``precheck_warnings[<code>]``。``precheck_only=true`` → :func:`_precheck_payload`，
    且 ``save_config`` **一次都不调**。
    拒绝面：无定位键 / 只有前缀 → 400；查无此条目 → 404；同名多条、id 撞 ``providers:`` → 409；
    条目没有可用 ``model:``（官方 :451-452 同样是 400）→ 400。四种**都不写 config**。
    """
    # 改动记录：2026-09-25 R3（M10.11，设计 §2.4 表 B · B6）：这两条 400 的 detail 只把动作名
    # 统一成 R3 的新词（设计 §7 术语表），请求契约的用词与「列表未改动」承诺原样保留；
    # **状态码与判据语义一字未动。**
    if body is None:
        raise HTTPException(
            status_code=400,
            detail="收编请求至少要给出 id 或 name —— 空 body 无法定位条目，列表未改动。")
    identity = (str(getattr(body, "id", "") or "").strip()
                or str(getattr(body, "name", "") or "").strip())
    if not identity:
        raise HTTPException(
            status_code=400,
            detail="收编请求的 id 与 name 都是空的，无法定位条目（列表未改动）。")
    base_url = str(getattr(body, "base_url", "") or "").strip()
    precheck_only = bool(getattr(body, "precheck_only", False))

    with _config_profile_scope(profile):
        cfg = load_config()
        raw_cfg = read_raw_config()
        plan = _migrate_plan(cfg, raw_cfg, identity, base_url, strict=not precheck_only)

        if precheck_only:                       # M6.3：弹窗前置检查，一个字节都不写
            return _precheck_payload(plan)

        if not str(plan.get("model") or "").strip():
            # 官方 `_write_custom_endpoint:451-452` 对空 model 抛 400。这里提前拦：detail 对
            # 用户更有用，而且这条路上我们连官方函数都没进过（.env 自然也没碰）。
            # 改动记录：2026-09-25 R3（M10.11，表 B · B7）：detail 改成使用者语言（存储字段名
            # 与「×× 条目」的说法去掉），动作对象 = 条目名仍写进原文；**400 与判据未动。**
            raise HTTPException(
                status_code=400,
                detail=(f"该供应商 {plan.get('name')} 既没有默认模型也没有已添加模型，"
                        "无法确定收编后的默认模型——请先在 cc-switch 里给它选一个模型。列表未改动。"))

        _stored, new_entry = _write_custom_endpoint(cfg, _migrate_body(plan))   # 不落盘
        endpoint_id = str(_stored or plan.get("endpoint_id") or "")
        if not isinstance(new_entry, dict):      # 官方契约是 (id, entry)，兜一手防御
            raise HTTPException(status_code=500,
                                detail="迁移失败：官方写入器没有返回条目，未保存。")

        _carry_unrepresentable_legacy_fields(new_entry, plan)
        _apply_migrated_key_reference(new_entry, plan)

        sequence = (cfg or {}).get("custom_providers")
        index = int(plan.get("legacy_index") or 0)
        if (isinstance(sequence, list) and 0 <= index < len(sequence)
                and sequence[index] is plan.get("legacy_entry")):
            del sequence[index]                  # 决议 9 派生：迁移后**不保留**原条目
        else:                                    # 只应在极端并发改盘时走到这里
            # 改动记录：2026-09-25 R3（M10.11，表 B · B6）：detail 的存储段名与动作名按批准
            # 口径替换；「不猜位置、不写盘」与「刷新后重试」两条原样保留。**409 与判据未动。**
            raise HTTPException(
                status_code=409,
                detail=("收编中止：由 cc-switch 管理的供应商里原条目的位置已变动（并发写入？）—— "
                        "不猜位置、不写盘。请刷新页面后重试。"))

        save_config(cfg)                         # ← **单次原子写**（M6.7 的计数判据）

    return _write_response(endpoint_id, profile, {
        "migrated": _migrate_summary(plan, endpoint_id),
        "precheck_warnings": list(plan.get("warnings") or []),
    })


# ═══ 区块 M7（追加）：危险动作 —— POST /endpoints/{id}/activate + DELETE /endpoints/{id} ═
#
# 设计依据：§3.2 UC-04（启用）/ UC-05（删除）、§5.3（危险动作走 ConfirmDialog）、
# §6.3 路由表（这两条都是**薄包装**：官方 `activate_custom_endpoint` /
# `delete_custom_endpoint`）、§6.4（写完失效 `['supplier-models','endpoints']`）、
# §10.2 决议 12（cc-switch 条目**不开放**这两个动作）、§9.1 F8 / §9.2 T14。
# 本区块只**追加**：M1 的 ``CC_ID_PREFIX``、M4 的 ``_write_response``、
# M5 的 ``_locate_providers_entry``（progress §四：共享 helper 只调用不修改）、
# M6 的 ``_custom_endpoint_id`` / ``custom_endpoint_key_env``，全部原样复用。
#
# ── 为什么这两个动作只对 `providers:` 行开放（决议 12）────────────────────────
# 官方两个包装函数的定位语句都是 ``find_provider_entry(cfg.get("providers"), …)``
# （``config_env.py:566`` 与 ``:608``）—— 条目住在 ``custom_providers:`` 时**必然 404**。
# 插件若自建第二套「启用 / 删除」语义去搬 legacy 条目，就要自己写顶层 ``model:``，
# 而那份状态归 cc-switch 的 live snapshot 所有 → 双头写（设计 §3.2 UC-10 动作矩阵）。
# 所以这里**不信前端**：``cc:`` 前缀的 id 在后端就被拒（400 + 中文 detail，说清楚
# 「不是找不到，是不该由本插件动手」+ 指出「高级 → 迁移到标准形态」这条正路），
# 前端同样不渲染这两个按钮（M6↔M7：迁移成功后行的 ``source`` 变成 ``providers``，
# 从此**才**拿到这两个动作 —— 判据继续吃 ``row["source"]``，见 :func:`_danger_gate`）。
#
# ── 官方 delete 的密钥与镜像语义（读源码所得，M7.4 文案的事实来源）─────────────
# ``delete_custom_endpoint``（``:598-618``）在一次 ``save_config`` 里做三件事：
#   ① ``providers.pop(stored_key)`` —— 条目消失；
#   ② ``remove_env_value(custom_endpoint_key_env(provider_key))`` —— **`.env` 里那份密钥被清**；
#   ③ :func:`_detach_main_model_from_provider`（``:418-435``）—— **仅当**顶层
#      ``model.provider``（strip + lower）**恰等于**该条目的 slug 时，才摘掉
#      ``model`` 里的 ``provider`` / ``base_url`` / ``api_key`` / ``key_env`` 四个键
#      （docstring 引 #62269：不摘的话 agent 会拿着「已删主机的已删密钥」继续鉴权）。
#      指向别处时**一个字节都不动** —— 所以「删除会不会顺手改掉当前供应商」的答案是
#      「只有删的就是当前供应商时才会」，M7.4 的二次确认文案按这个口径写，
#      ``tests/test_activate_delete.py`` 两种情形各钉一条。
# ``custom_endpoint_key_env`` 只要**变量名**，与 M6 的 ``env_var_name`` 同量级信息；
# 密钥内容 / 展开值 / 哈希在任何响应字段与日志里都不出现（M1.5 的密钥铁律）。
#
# ── profile（R8 / 停止点 C 结案口径）──────────────────────────────────────────
# 两条路由都显式声明 ``profile: Optional[str] = None`` 并**原样透传**给官方函数
# （官方签名：``activate_custom_endpoint(endpoint_id, profile=None)`` :557、
# ``delete_custom_endpoint(endpoint_id, profile=None)`` :598）。⚠️ DELETE 没有请求体，
# 所以官方与本插件的 ``profile`` 都是 **query 参数**（FastAPI 按形参位置判），
# 前端一律**不**手工拼 ``?profile=``（`ctx.rest` 已 profile-aware，prompt §2.6 C2 已结案）。
# 官方 route 外层套着 ``http_failure``：``HTTPException``（404 / 400）原样穿透，
# 其它异常统一折成 500 —— 本区块不吞、不改写这些状态码。

from hermes_cli.web_routers.config_env import (  # noqa: E402
    activate_custom_endpoint,
    delete_custom_endpoint,
)

ACTIVATE_PATH = "/endpoints/{endpoint_id}/activate"
DELETE_PATH = "/endpoints/{endpoint_id}"
# 拒绝面 detail 里的动词（两条路由共用同一套措辞）
DANGER_LABEL_ACTIVATE = "启用"
DANGER_LABEL_DELETE = "删除"
# 官方 `_detach_main_model_from_provider` 唯动的那四个顶层 `model:` 键（:433）；
# 测试用它当「镜像摘净 / 未指向时一个都不动」的比对清单。
MAIN_MODEL_MIRROR_FIELDS = ("provider", "base_url", "api_key", "key_env")


def _cc_refusal_detail(identity: str, action: str) -> str:
    """``cc:`` 条目要求危险动作时的 detail（决议 12）：说清「必 404」的根因与正路。"""
    bare = identity[len(CC_ID_PREFIX):].strip()
    who = (f"{identity}（条目名 {bare}）" if bare
           else f"{identity}（只有 cc: 前缀、没有条目名）")
    # 改动记录：2026-09-25 R3（M10.10，设计 §2.4 表 B · B5）：detail 改使用者语言——去掉源码
    # 路径与行号、存储字段名、live snapshot 与设计编号引用，正路改成「收编 → 收编进本插件」。
    # 四要素照旧：动作对象（`who` 那份身份组合）、拒绝原因、正路指引、「列表未改动」承诺。
    # 函数名 / 形参 / 返回的都是措辞而已，**判据与状态码在 `_danger_gate` 那里，一字未动**。
    return (f"{who} 由 cc-switch 管理：官方的{action}只认本插件管理的供应商，对它必然失败；"
            f"本插件自建第二套{action}语义会与 cc-switch 互相覆盖。"
            "要先交给本插件管，请走「收编 → 收编进本插件」；否则请回 cc-switch 操作。列表未改动。")


def _danger_gate(endpoint_id: str, action: str,
                 profile: Optional[str]) -> Dict[str, Any]:
    """启用 / 删除共用的**写前闸门**：三种拒绝面都发生在任何写动作之前，一律**零写入**。

    * 空 id → **400**；
    * ``cc:`` 前缀 → **400**（见 :func:`_cc_refusal_detail`，决议 12）；
    * ``providers:`` 里查无此条目 → **404**，复用 M5 的 :func:`_locate_providers_entry`
      （它把「其实在 ``custom_providers:`` 里」与「真不存在」分成两句 detail，且绝不静默新建）。

    定位一律喂**规整后的 slug**（``_custom_endpoint_id(identity)``），与官方两条 route 内部的
    写法逐字一致 —— 闸门比官方宽松就会把「官方必 404」的请求放到写盘那一步，
    比官方严格就会把官方能办的条目拦下来，两种都是行为漂移。

    返回的是**快照**（不是 cfg 里的活引用）：``delete`` 会把条目整个摘掉，
    响应的 ``deleted{name, model}`` 必须在动手之前取好。``main_model_mirrors_entry``
    是「顶层 ``model.provider`` 此刻是否指向本条目」，判据与官方
    :func:`_detach_main_model_from_provider` 完全同一条（strip + lower 比 slug），
    所以回执里的「镜像摘了 / 没摘」与真正落盘的事不可能分叉。
    """
    identity = str(endpoint_id or "").strip()
    if not identity:
        raise HTTPException(
            status_code=400,
            detail=f"缺少供应商标识（路径里的 {{id}} 是空的）—— {action}未执行，列表未改动。")
    if identity.startswith(CC_ID_PREFIX):
        raise HTTPException(status_code=400, detail=_cc_refusal_detail(identity, action))

    provider_key = _custom_endpoint_id(identity)
    with _config_profile_scope(profile):
        cfg = load_config()
    stored, entry = _locate_providers_entry(cfg, provider_key)
    model_cfg = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    return {
        "identity": identity,
        "main_model_mirrors_entry": (
            str(model_cfg.get("provider") or "").strip().lower() == provider_key),
        "model": str(entry.get("model") or entry.get("default_model") or "").strip(),
        "name": str(entry.get("name") or "").strip() or provider_key,
        "provider_key": provider_key,
        "stored": str(stored if stored is not None else provider_key),
    }


@router.post(ACTIVATE_PATH)
def activate_endpoint(endpoint_id: str, profile: Optional[str] = None):
    """「启用」= 把这个 ``providers:`` 条目设成 Hermes 当前主模型（M7.1 / UC-04 / F8）。

    薄包装官方 ``activate_custom_endpoint(endpoint_id, profile)``（``config_env.py:557``）：
    它自己 load/save（**一次** ``save_config``），把条目的 ``model`` / ``base_url`` /
    ``key_env`` 经 ``_validated_main_model_selection`` + ``_apply_main_model_assignment``
    写进顶层 ``model:``，成功返回 ``{ok, provider, model}``（``provider`` = 规整后的 slug）。

    本路由在其外面做三件事：① :func:`_danger_gate`（空 id / ``cc:`` / 查无此条目的三条拒绝面，
    全部**零写入**）；② ``profile`` 原样透传（R8）；③ 复用 :func:`_write_response` 把
    M1 的归一化行与 ``current`` 一起回给前端 —— 前端因此**不需要**再跑一次
    ``GET /endpoints`` 就能看到「●使用中」转移到哪张卡（§6.4 的失效路径仍然照跑）。

    响应：``{ok, id, provider, model, endpoints: [...], current: {...}}``。
    ``ok`` / ``id`` 由 :func:`_write_response` 出，``provider`` / ``model`` 是**官方回执原值**
    （官方那两份就是 ``{ok, provider, model}``，本路由不重算、不改写）。
    其余拒绝面沿用官方：条目缺 ``model`` / ``base_url`` → 400（``:573-574``）、
    主模型选择被 catalog 拒 → 400（``_validated_main_model_selection:468-469``）。
    """
    gate = _danger_gate(endpoint_id, DANGER_LABEL_ACTIVATE, profile)
    result = activate_custom_endpoint(gate["identity"], profile)
    payload = result if isinstance(result, dict) else {}
    return _write_response(gate["stored"], profile, {
        "model": str(payload.get("model") or ""),
        "provider": str(payload.get("provider") or gate["provider_key"]),
    })


@router.delete(DELETE_PATH)
def delete_endpoint(endpoint_id: str, profile: Optional[str] = None):
    """「删除」= 摘掉条目 + 清 ``.env`` 密钥 + 按需摘顶层 ``model`` 镜像（M7.2 / UC-05 / T14）。

    薄包装官方 ``delete_custom_endpoint(endpoint_id, profile)``（``config_env.py:598``）；
    它内部一次 ``save_config`` 做三件事（见本区块头注的①②③），语义与本插件一致，
    所以**不**自己重写删条目的逻辑。``profile`` 是 **query 参数**（与官方同形：DELETE 无 body）。

    响应：``{ok, id, endpoints: [...], current: {...}, deleted: {...}}``。
    ``deleted`` 段是**动手之前**从磁盘条目上取好的快照，给前端的回执与二次确认文案用：
    ``name`` / ``model``（条目自己写的字段）、``provider``（官方比对镜像用的 slug）、
    ``env_var``（被清的 ``.env`` **变量名**，不是值）、``main_model_detached``
    （``true`` = 顶层 ``model`` 镜像原本指向本条目、已随删除摘掉；
    ``false`` = 它指向别处，官方一个字节都没动 —— 这正是 UC-05 文案要区分的那两种后果）。
    拒绝面（全部**零写入**）：空 id → 400、``cc:`` 前缀 → 400、查无此条目 → 404。
    """
    gate = _danger_gate(endpoint_id, DANGER_LABEL_DELETE, profile)
    delete_custom_endpoint(gate["identity"], profile)
    return _write_response(gate["stored"], profile, {
        "deleted": {
            "env_var": custom_endpoint_key_env(gate["provider_key"]),
            "id": gate["stored"],
            "main_model_detached": bool(gate["main_model_mirrors_entry"]),
            "model": gate["model"],
            "name": gate["name"],
            "provider": gate["provider_key"],
        },
    })


# ═══ 区块 M12（追加）：官方密钥供应商「克隆转正」—— GET /endpoints/builtin-candidates
#     + POST /endpoints/{slug}/adopt + POST /endpoints/{id}/release ══════════════════
#
# 设计依据：v0.0.3 §4 全文（§4.2 数据源、§4.3.1/4.3.2/4.3.3 三条路由、§4.3.4 拒绝面表、
# §4.5 状态机、§4.7 测试表 a–k）；需求 proposal §5.2.2（终拍形态）+ §7 拍板 #11 +
# 设计 §9 的 D3（退回走插件直写，不走官方 delete）/ D4（克隆 id 钉死 managed-<slug>）。
# 本区块只**追加**：M1 的 ``endpoints`` / M4 的 ``_write_response`` / ``SAVE_CHANNEL_*`` /
# M5 的 ``_locate_providers_entry`` / M6 的 ``_write_custom_endpoint`` 与 ``_custom_endpoint_id``
# 全部原样复用（progress §三：只调用不修改）。M1–M7 区块与 M9 的 ``save_endpoint`` 一字未动。
#
# ── 为什么必须是「克隆」而不是「给内置供应商写一条覆盖条目」────────────────────────
# F0 裁定（proposal §5.2.1）：规范内置名被 ``_shadowed_by_builtin``
# （``hermes_cli/runtime_provider_custom.py:94-110``，2026-09-24 实测）在扫 ``providers:``
# **之前**短路 —— 请求名解析回规范内置自身就直接 ``return None``，覆盖条目全无效；
# 唯一还生效的字段是 ``providers.<slug>.enabled``（``runtime_provider.py:752-760``）。
# 所以接管写的是**非规范 id** ``managed-<slug>``（拍板 D4）的条目，外加把内置那份关掉。
#
# ── 三条本模块自己的硬判据（设计 §4.3，测试逐条钉）───────────────────────────────
#   ① **.env 一个字节都不写**：``body.api_key=None`` 且新条目没有明文 ``api_key`` 时，
#      官方 ``_write_custom_endpoint``（``config_env.py:496-512`` 的三支）里
#      ``save_env_value`` / ``remove_env_value`` **两支都不进**（实测见
#      :func:`adopt_builtin_candidate` 的第 ③ 拍）。回执里 ``env_written`` 恒 ``False``
#      = 机器可读的自证（拍板 #11「改密钥请回官方」在后端的落点）。
#   ② **克隆条目的 ``key_env`` 指向官方既有变量名**：官方省略 key 时根本不写 ``key_env``
#      （实测同一处），所以本模块自己补这一笔 —— 指向 ``.env`` 里**已经存在**的那个
#      官方变量，官方换钥匙插件自动跟上，不出现第二份真相。
#   ③ **一次 ``save_config``**：克隆 + 停用内置写在同一次原子落盘里（强于提案的「有回执
#      顺序的单一动作」，proposal §5.3 双开关窗口因此不存在）；退回的三拍同理。
#      半途失败如实面 = 「这一次写失败 → 500，列表未改动」。
#
# ── 数据源（设计 §4.2；M12.0 于 2026-09-25 在隔离副本实测复核）───────────────────
#   目录枚举      ``hermes_cli/provider_catalog.py:67 provider_catalog()``（descriptor 字段
#                 实测：slug / label / description / auth_type / tab / api_key_env_vars /
#                 base_url_env_var / signup_url / order / keyless）
#   密钥只判存在  ``hermes_cli/config.py load_env()`` —— 与官方「API 密钥」页同一个读盘口
#                 （``web_routers/config_env.py:232-244`` 的 ``is_set = bool(value)``），
#                 **只判存在性、永不取值**（密钥铁律 §0.2-2）
#   端点快照      ``d.base_url_env_var`` 有值且已设 → 取该 env 值；否则
#                 ``auth.py:252 PROVIDER_REGISTRY[slug].inference_base_url``；再否则兜底表
#   只读模型目录  ``hermes_cli/models.py _PROVIDER_MODELS``（进程内静态快照，实测值是
#                 **字符串列表**；跨模块按私有名 import 是仓库既有惯例，
#                 ``model_setup_flows.py`` 六处先例，与本文件 M6 的
#                 ``_write_custom_endpoint`` 同一口径）；默认模型兜底
#                 ``models.py:475 get_default_model_for_provider(slug)``
#
# ── M12.0 P0 首验结论（隔离副本、只读；原始输出进 M12 汇报）───────────────────────
# openrouter 的 descriptor **在** ``provider_catalog()`` 里（54 条之一），
# ``api_key_env_vars`` **非空**（``OPENROUTER_API_KEY``）→ 密钥判据走主路，兜底表只用来
# 补**端点**那一格：它 ``base_url_env_var`` 为空且**不在** ``auth.PROVIDER_REGISTRY``
# （``auth.py:259 _REGISTRY_PLUGIN_SKIP`` 明载）→ ``inference_base_url`` 无从取，落
# :data:`BUILTIN_BASE_URL_FALLBACK`。它**在** ``CANONICAL_PROVIDERS``
# （``models_catalog_static.py:314-317``，实测按 slug 命中；那份常量在本机构成是
# ``ProviderEntry`` 列表而非字符串列表）。``_PROVIDER_MODELS`` 里**没有** openrouter 这一项
# （实测），故其只读模型目录为空、默认模型走 ``get_default_model_for_provider`` 兜底。
# 与设计 §4.2 末行的假设一致，未触发「停下回报」条件。
#
# ── 本模块对 M1 行契约的依赖：**第三处「只加字段」补丁**（编排裁定 2026-09-26）────────
# 接管把来源标记写在 ``providers:`` **条目**上（``CLONE_ORIGIN_FIELD``），但官方行构造器
# ``_endpoint_row``（``web_routers/config_env.py:366-376``）是**白名单**——它逐键手拼 11 个字段，
# ``_custom_endpoint_response`` 也只把整条条目当 ``key_entry`` 交给 ``_api_key_display``，
# 未知字段一概不进 ``GET /endpoints`` 的行。不透传的后果是前端三处判据同时落空：
# ``isAdoptedCloneRow``（徽章 + 退回钮）、``formFromRow`` 的 ``keyReadonly``（拍板 #11 的置灰）、
# 弹窗取的那格来源名。故 :func:`_apply_clone_origin` 挂在 M1 ``endpoints()`` 的补丁链尾。
# 它是**只读派生视图**：磁盘真相仍是条目上的 ``managed_from``，``release`` 的判据
# （:func:`_release_plan`）照旧直接读磁盘条目、不经行，两者不可能各说一套。
from hermes_cli.auth import PROVIDER_REGISTRY  # noqa: E402
from hermes_cli.config import load_env  # noqa: E402
from hermes_cli.models import _PROVIDER_MODELS, get_default_model_for_provider  # noqa: E402
from hermes_cli.provider_catalog import provider_catalog  # noqa: E402

BUILTIN_CANDIDATES_PATH = "/endpoints/builtin-candidates"
BUILTIN_ROW_SOURCE = "builtin-catalog"          # 前端只读卡的判源（不并入 M1 行契约）
# ``providers:`` 行的 ``source`` 字面值（官方 ``_custom_endpoint_response:400`` 写死）——
# 第三处契约补丁在行上判源用它，与 M1 的 ``CC_SOURCE`` 同一风格（不看 id 长什么样）
PROVIDERS_ROW_SOURCE = "providers"
CLONE_ID_PREFIX = "managed-"                    # 拍板 D4：克隆 id 钉死 managed-<slug>
CLONE_ORIGIN_FIELD = "managed_from"             # 克隆条目上的来源标记（canonical slug）
ADOPT_PATH = "/endpoints/{slug}/adopt"
RELEASE_PATH = "/endpoints/{endpoint_id}/release"

# 「官方密钥」那一页的目录判据：accounts 页的 OAuth 登录态与匿名端点都不在范围
# （proposal §5.2-5 + §5.2.2 范围收紧条款）
BUILTIN_KEYS_TAB = "keys"
# 候选行的状态（设计 §4.5 状态机里**可枚举**的两态；「有克隆但内置没关」那一态实测不可达，
# 且克隆行在场时它压根不进这张表）
BUILTIN_STATUS_UNMANAGED = "official_key_unmanaged"
BUILTIN_STATUS_HALF_ADOPTED = "official_key_half_adopted"
# 接管回执的两种结果（设计 §4.3.2 ② 的幂等半边）
ADOPT_CREATED = "created"
ADOPT_ALREADY = "already"
# 兜底表（M12.0 实测见上）：**只补「descriptor / 注册表给不出这一项」的缺口**，不扩大兜底面
BUILTIN_KEY_ENV_FALLBACK: Dict[str, Tuple[str, ...]] = {"openrouter": ("OPENROUTER_API_KEY",)}
BUILTIN_BASE_URL_FALLBACK: Dict[str, str] = {"openrouter": "https://openrouter.ai/api/v1"}
# 克隆条目显示名的后缀（设计 §4.3.2 的 ``f"{descriptor.label}（已管理）"``）
CLONE_NAME_SUFFIX = "（已管理）"
# ``release`` precheck 的提示码（弹窗据此组句；不是错误，是「将会顺带动到谁」的预告）
RELEASE_NOTE_TOP_MODEL = "top_model_points_to_clone"
# 顶层 ``model:`` 里退回时要摘掉的镜像键（``provider`` 不在其中——它是**改指回内置**）
RELEASE_TOP_MODEL_CLEARED_FIELDS = tuple(k for k in MAIN_MODEL_MIRROR_FIELDS if k != "provider")


class EndpointReleaseRequest(BaseModel):
    """``POST /endpoints/{id}/release`` 的**可选**请求体（M12.5，与 M6 的 precheck 同型）。

    ``precheck_only=true`` 走纯读分支：一次 ``save_config`` 都不发生，返回
    ``{blocks, plan}`` 让弹窗按实况组句（设计 §4.4-5：「退回后当前使用中的供应商会切回
    官方 {name}」那一句必须来自真实判据，不许前端猜）。
    """

    precheck_only: bool = False


# ------------------------------------------------------------- 纯读：目录侧的取数原语

def _builtin_descriptor(slug: Any) -> Optional[Any]:
    """按 slug 取内置目录描述符（大小写不敏感）；没有 → ``None``。纯读。"""
    wanted = str(slug or "").strip().lower()
    if not wanted:
        return None
    for descriptor in (provider_catalog() or []):
        if str(getattr(descriptor, "slug", "") or "").strip().lower() == wanted:
            return descriptor
    return None


def _builtin_is_adoptable_target(descriptor: Any) -> bool:
    """目录侧的两条入册资格：坐在「API 密钥」那一页 + 不是匿名端点。"""
    if descriptor is None:
        return False
    if str(getattr(descriptor, "tab", "") or "").strip() != BUILTIN_KEYS_TAB:
        return False
    return not bool(getattr(descriptor, "keyless", False))


def _builtin_env_var_names(slug: Any, descriptor: Any) -> List[str]:
    """该供应商在 ``.env`` 里的**变量名清单**（名字不是秘密，与 M6 的 ``env_var_name`` 同量级）。

    descriptor 给不出时才落 :data:`BUILTIN_KEY_ENV_FALLBACK`（M12.0 实测 openrouter 的
    descriptor 本来就有 ``OPENROUTER_API_KEY`` ⇒ 主路就够；兜底表是「为空」那一档的
    已裁定形状，绝不拿它去覆盖 descriptor 的非空结果）。
    """
    names = [str(item).strip() for item in (getattr(descriptor, "api_key_env_vars", ()) or ())
             if str(item).strip()]
    if names:
        return names
    return [str(item) for item in (BUILTIN_KEY_ENV_FALLBACK.get(str(slug or "").strip().lower()) or ())]


def _builtin_configured_key_env(slug: Any, descriptor: Any, env: Dict[str, Any]) -> str:
    """**只判存在性**取第一个已配的官方变量名；一个都没配 → 空串（永不返回密钥值）。"""
    for name in _builtin_env_var_names(slug, descriptor):
        if (env or {}).get(name):
            return name
    return ""


def _builtin_snapshot_base_url(slug: Any, descriptor: Any, env: Dict[str, Any]) -> str:
    """官方端点的**快照**取值链（设计 §4.2）：env 覆盖 → 注册表 → 兜底表。"""
    env_var = str(getattr(descriptor, "base_url_env_var", "") or "").strip()
    if env_var and str((env or {}).get(env_var) or "").strip():
        return str((env or {}).get(env_var)).strip()
    wanted = str(slug or "").strip().lower()
    registered = (PROVIDER_REGISTRY or {}).get(wanted)
    url = str(getattr(registered, "inference_base_url", "") or "").strip()
    if url:
        return url
    return str(BUILTIN_BASE_URL_FALLBACK.get(wanted) or "").strip()


def _builtin_catalog_models(slug: Any) -> List[str]:
    """只读模型目录 = ``_PROVIDER_MODELS`` 的静态快照（没有 → 空列表，卡上另有说明）。"""
    snapshot = (_PROVIDER_MODELS or {}).get(str(slug or "").strip().lower())
    if not isinstance(snapshot, (list, tuple)):
        return []
    return [str(item).strip() for item in snapshot if str(item).strip()]


def _builtin_default_model(slug: Any, models: List[str]) -> str:
    """默认模型：静态快照首项，否则官方 ``get_default_model_for_provider`` 兜底。"""
    if models:
        return models[0]
    try:
        return str(get_default_model_for_provider(str(slug or "").strip().lower()) or "").strip()
    except Exception:            # 手工改出来的陌生来源名不该把动作搞成 500
        return ""


def _builtin_official_label(descriptor: Any, slug: Any) -> str:
    return str(getattr(descriptor, "label", "") or "").strip() or str(slug or "").strip()


def _builtin_release_model(slug: Any, models: List[str]) -> str:
    """退回第 1 拍的默认模型：先问官方默认，再落静态目录首项。

    与 :func:`_builtin_default_model` 的次序**故意相反**，两边各自的判据不一样：
      · 接管 = 「给一条新条目挑默认模型」→ 设计 §4.3.2 要 ``_PROVIDER_MODELS`` 首项优先
        （目录里有就用它，白名单与默认同源）；
      · 退回 = 「顶层那条引用要落到内置身上，拿不准就换」→ 设计 §4.3.3 第 1 拍写的是
        ``get_default_model_for_provider(origin)`` 优先（内置的官方默认最不可能服务不了）。
    """
    try:
        official = str(get_default_model_for_provider(str(slug or "").strip().lower()) or "").strip()
    except Exception:
        official = ""
    return official or (models[0] if models else "")


# --------------------------------------------------- 纯读：``providers:`` 侧的占用判据

def _builtin_is_usable_entry(entry: Any) -> bool:
    """``providers:`` 里的一条**可用**条目 = dict 且带端点。

    判据与官方出行判据同一条（``web_routers/config_env.py:390-394``：没有 ``base_url``
    的条目**不出行**）。这件事对本模块是决定性的：接管半途写的
    ``providers.<slug> = {enabled: false}`` 最小开关**不算占用名字**，所以
    「无克隆 + 已被停用」那一态仍然列得出来（设计 §4.5 第四行），用户点接管可修复。
    """
    return isinstance(entry, dict) and bool(_entry_base_url(entry))


def _builtin_entry_disabled(entry: Any) -> bool:
    """``enabled`` 是否被**显式**关掉（``config_providers.py:552 is_provider_enabled``：
    缺省 True，只有显式 false 才隐藏）。字符串写法一并容忍（手改盘的兜底）。"""
    if not isinstance(entry, dict) or "enabled" not in entry:
        return False
    value = entry.get("enabled")
    if isinstance(value, str):
        return value.strip().lower() in {"false", "no", "0"}
    return value is False


def _providers_map(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """取 ``cfg["providers"]`` 的**活引用**；没有就当场建一个空 dict 并挂回去。

    挂回去是必要的：``save_config(cfg)`` 序列化的是 ``cfg``，一个只活在局部变量里的
    dict 永远不会落盘（官方 ``_write_custom_endpoint`` 末尾 ``cfg["providers"] = providers``
    同一手法）。
    """
    providers = (cfg or {}).get("providers")
    if not isinstance(providers, dict):
        providers = {}
        cfg["providers"] = providers
    return providers


# ------------------------------------- 纯读：M1 行的第三处契约补丁（只加字段，不透传密钥）

def _providers_clone_origin(raw_cfg: Dict[str, Any], endpoint_id: Any) -> str:
    """``providers:`` 条目**磁盘上**的来源标记；不是「strip 后非空的字符串」→ 空串。

    定位复用官方 :func:`find_provider_entry`（与 M1 两处补丁同一条 raw 链，:2022 写在哪、
    这里就从哪读），值一律 strip：``7`` / ``None`` / ``"   "`` 都算「没有来源」，
    免得手工写坏的盘把一个非来源名当来源渲染出「已接管」徽章 —— 徽章一旦出，
    退回钮也跟着出，而 :func:`release_clone` 会按磁盘判据拒它（口径分叉）。
    """
    try:
        _stored, entry = find_provider_entry((raw_cfg or {}).get("providers"), endpoint_id)
    except Exception:      # 派生字段绝不把只读列表打成 500（与 M1 的判据同一降级口径）
        return ""
    origin = entry.get(CLONE_ORIGIN_FIELD) if isinstance(entry, dict) else None
    return origin.strip() if isinstance(origin, str) else ""


def _apply_clone_origin(rows: List[Dict[str, Any]],
                        raw_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """就地给克隆行补 ``managed_from``；**没有来源标记的行不写这个键**（不写 ``None``、不写空串）。

    形状与 :func:`_apply_is_current` 一致（原样返回同一个 rows），判源只认
    ``source == "providers"``：cc-switch legacy 行（``CC_ID_PREFIX``）与官方合成的
    ``direct-config`` 行背后都不是接管写出来的条目，一律不碰。
    纯读、零新增 import：``raw_cfg`` 由调用方（M1 的只读列表）在 profile 作用域里备好。
    """
    for row in rows:
        if str(row.get("source") or "") != PROVIDERS_ROW_SOURCE:
            continue
        origin = _providers_clone_origin(raw_cfg, row.get("id"))
        if origin:
            row[CLONE_ORIGIN_FIELD] = origin
    return rows


# ═════════════════════════════ 1. 发现（只读）═════════════════════════════════════

@router.get(BUILTIN_CANDIDATES_PATH)
def builtin_candidates(profile: Optional[str] = None):
    """「官方密钥 · 未接管」的只读候选（设计 §4.3.1；M12.2 / M12.3）。**纯读，零写入**。

    五段过滤（顺序即判据顺序，全部在 profile 作用域内）：
      ① ``d.tab == "keys"`` ② ``not d.keyless`` ③ 官方密钥**已配**（只判 env 存在性）
      ④ ``managed-<slug>`` 不在 ``providers:``（已接管 → 主列表那条克隆行就是它，不重复列）
      ⑤ ``<slug>`` 本体没有被**可用**条目占用（用户自建同名条目 = 占用，不猜）。
    未配密钥的目录项**一概不出现**（proposal §5.4-1）；④⑤ 之外「无克隆 + 已被停用」的
    半途态**仍然列出**并带 ``status="official_key_half_adopted"``。

    为什么是独立 GET 而不是并入 ``GET /endpoints``：M1 的行契约被 ``test_endpoints.py``
    逐字段钉死（「既有字段与语义一律冻结」），独立路由令 M12 的测试与 M1 的钉彻底隔离
    （设计 §4.3.1 的设计取舍）。前端把两份结果混排渲染（拍板 D5）。

    密钥铁律：本响应没有任何字段能带出密钥值 —— ``has_api_key`` 恒 True 就是**入册门槛**
    本身，密钥侧只出**变量名**（``api_key_env_names``）。
    信封 ``{candidates, current}``：``current`` 直接取 M1 的同一份语义（顶层摘要）。
    """
    payload = endpoints(profile) or {}
    current = payload.get("current") if isinstance(payload.get("current"), dict) else {}

    with _config_profile_scope(profile):
        cfg = load_config() or {}
        env = load_env() or {}
        catalog = provider_catalog() or []

    providers = cfg.get("providers") if isinstance(cfg.get("providers"), dict) else {}
    rows: List[Dict[str, Any]] = []
    for descriptor in catalog:
        slug = str(getattr(descriptor, "slug", "") or "").strip().lower()
        if not slug or not _builtin_is_adoptable_target(descriptor):
            continue
        if not _builtin_configured_key_env(slug, descriptor, env):
            continue
        clone_stored, clone_entry = find_provider_entry(providers, CLONE_ID_PREFIX + slug)
        if clone_stored is not None or isinstance(clone_entry, dict):
            continue
        _slug_stored, slug_entry = find_provider_entry(providers, slug)
        if _builtin_is_usable_entry(slug_entry):
            continue
        rows.append({
            "adoptable": True,
            "api_key_env_names": _builtin_env_var_names(slug, descriptor),
            "base_url": _builtin_snapshot_base_url(slug, descriptor, env),
            "has_api_key": True,
            "id": slug,
            "models": _builtin_catalog_models(slug),
            "name": _builtin_official_label(descriptor, slug),
            "source": BUILTIN_ROW_SOURCE,
            "status": (BUILTIN_STATUS_HALF_ADOPTED if _builtin_entry_disabled(slug_entry)
                       else BUILTIN_STATUS_UNMANAGED),
        })
    return {"candidates": rows, "current": current}


# ═════════════════════════ 2. 接管（克隆转正）═════════════════════════════════════

@router.post(ADOPT_PATH)
def adopt_builtin_candidate(slug: str, profile: Optional[str] = None):
    """接管一条**已配官方密钥**的内置供应商 = 克隆转正（设计 §4.3.2；M12.4）。

    五拍，**磁盘上没有中间态**（③④ 合并在同一次 ``save_config`` 里）：

      ① **纯读复核**（后端不信前端）：目录资格 + 密钥已配 → 否则 **404**；
         ``<slug>`` 本体被可用条目占用 → **409**（「不猜」）；端点快照或默认模型取不到 →
         **400**。三条拒绝面都发生在任何写动作之前 ⇒ 一律**零写入**。
      ② **幂等**：``managed-<slug>`` 已在 → 跳过创建（半途重试的正路），回执
         ``adopted="already"``，但 ④ 照做（把没关掉的内置关掉）。
      ③ **内存内调官方写入器**（与 :func:`migrate_endpoint` 同型，**不落盘**）：
         ``api_key=None`` 是关键一笔 —— 官方那三支里 ``save_env_value`` 与
         ``remove_env_value`` 都不进（``config_env.py:496-512`` 实测），于是
         **.env 一个字节都不写**；name/base_url 规整、slug 规整、键搬家、
         ``make_default`` 顶层镜像这五件官方行为因此保持官方一份实现。随后本模块补两笔
         官方给不出的：``key_env`` 指向 .env 里既有的官方变量名、``managed_from`` 记来源
         （兼作前端「密钥只读」的判据，拍板 #11）。
      ④ **停用内置**：命中 dict 条目 → 就地 ``enabled=False``；无条目 → 造
         ``providers[slug] = {"enabled": False}`` 最小开关（实测该条目没有 base_url，
         官方 ``_custom_endpoint_response:390-394`` 不认它 ⇒ 不会冒出幽灵行）。
      ⑤ **单次原子写** + :func:`_write_response`（写后重跑 M1 只读列表，密钥不出网关）。

    回执 ``{adopted, disabled_builtin, key_env, snapshot_base_url, env_written}```：
    ``env_written`` **恒 False**，是「.env 零写入」的机器可读自证（测试 c 钉这条）。
    """
    identity = str(slug or "").strip().lower()
    descriptor = _builtin_descriptor(identity)

    with _config_profile_scope(profile):
        cfg = load_config() or {}
        env = load_env() or {}

    key_env = _builtin_configured_key_env(identity, descriptor, env)
    if not _builtin_is_adoptable_target(descriptor) or not key_env:
        raise HTTPException(
            status_code=404,
            detail=(f"接管 {identity or '（空标识）'} 未执行：该供应商不在可接管列表里"
                    "（要么没配官方密钥，要么不是内置供应商）。列表未改动。"))

    providers = cfg.get("providers") if isinstance(cfg.get("providers"), dict) else {}
    clone_id = CLONE_ID_PREFIX + identity
    clone_stored, clone_entry = find_provider_entry(providers, clone_id)
    _slug_stored, slug_entry = find_provider_entry(providers, identity)
    if _builtin_is_usable_entry(slug_entry):
        raise HTTPException(
            status_code=409,
            detail=(f"接管 {identity} 未执行：本插件管理的供应商里已经有同名条目，接管会撞上它"
                    "——请先处理掉那条重名的。列表未改动。"))

    snapshot_base_url = _builtin_snapshot_base_url(identity, descriptor, env)
    catalog_models = _builtin_catalog_models(identity)
    default_model = _builtin_default_model(identity, catalog_models)
    if not snapshot_base_url or not default_model:
        raise HTTPException(
            status_code=400,
            detail=(f"接管 {identity} 未执行：该供应商没有可用的官方端点或默认模型，"
                    "无法为它建立自有条目。列表未改动。"))

    adopted = ADOPT_ALREADY if (clone_stored is not None
                                or isinstance(clone_entry, dict)) else ADOPT_CREATED
    adopted_key_env = key_env
    if adopted == ADOPT_ALREADY:
        # 幂等支：克隆已在，`key_env` 以磁盘现值为准（官方换了变量名时用户重跑接管也不改它）。
        adopted_key_env = str((clone_entry or {}).get("key_env") or "").strip() or key_env
    else:
        body = CustomEndpointUpdate(
            api_key=None,                                   # ← .env 零写入的那一笔
            base_url=snapshot_base_url,
            context_length=None,
            discover_models=False,                          # 铁律 1（恒为白名单模式）
            id=clone_id,
            make_default=False,                             # 接管不顺手切换当前供应商
            model=default_model,
            models=list(catalog_models),
            name=f"{_builtin_official_label(descriptor, identity)}{CLONE_NAME_SUFFIX}",
        )
        _stored, entry = _write_custom_endpoint(cfg, body)  # 不落盘（同 M6 的内存调用）
        if isinstance(entry, dict):
            entry.pop("api_key", None)                      # 官方没写，也不许旁路带进来
            entry["key_env"] = adopted_key_env              # 指向 .env 里既有的官方变量
            entry[CLONE_ORIGIN_FIELD] = identity            # 来源标记 = 前端只读判据

    # ④ 停用内置（与 ③ **同一次**落盘；proposal §5.3「双开关窗口」在这里退化为更强保证）
    live_providers = _providers_map(cfg)
    _prov_stored, prov_entry = find_provider_entry(live_providers, identity)
    if isinstance(prov_entry, dict):
        prov_entry["enabled"] = False
    else:
        live_providers[identity] = {"enabled": False}
    # 落盘必须在**同一个** profile 作用域里（M5/M6 同口径）：官方 save_config 的路径
    # 由作用域解析，写在作用域外会让带 profile 的请求落到默认配置上。仍是一次原子写。
    with _config_profile_scope(profile):
        save_config(cfg)                                        # ← 单次原子写

    return _write_response(clone_id, profile, {
        "adopted": adopted,
        "disabled_builtin": True,
        "env_written": False,
        "key_env": adopted_key_env,
        "snapshot_base_url": snapshot_base_url,
    })


# ══════════════════════════ 3. 退回官方管理（插件直写）════════════════════════════

def _release_plan(cfg: Dict[str, Any], stored: Any, entry: Dict[str, Any]) -> Dict[str, Any]:
    """退回的**判据**（纯读；precheck 与真跑同一个函数 ⇒ 弹窗所见即所做）。设计 §4.3.3。

    ``moved_top_model`` = 顶层 ``model.provider`` 此刻是否正指着这条克隆（判据与官方
    :func:`_detach_main_model_from_provider` 同一条：strip + lower 比 slug），所以回执里的
    「顶层安置了 / 没安置」与真正落盘的事不可能分叉。
    ``model`` 是安置之后顶层要留下的模型：现值仍在内置可服务集合（静态快照 ∪ 空）里就
    **保留**，拿不准就换 ``get_default_model_for_provider(origin)``（保守替换，绝不留一个
    内置不认的模型在顶层）。
    ``origin_is_self`` 是手改盘才会出现的病态（来源指向克隆自己）：真跑据此**拒绝**，
    否则第 1 拍会把顶层指到一条刚被删掉的条目上。
    """
    origin = str(entry.get(CLONE_ORIGIN_FIELD) or "").strip().lower()
    clone_key = str(stored or "").strip().lower()
    model_cfg = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    moved_top = bool(clone_key) and str(model_cfg.get("provider") or "").strip().lower() == clone_key
    served = _builtin_catalog_models(origin)
    keep_model = str(model_cfg.get("default") or "").strip()
    model_replaced = False
    if moved_top and keep_model not in served:
        fallback = _builtin_release_model(origin, served)
        if fallback and fallback != keep_model:
            keep_model = fallback
            model_replaced = True
    _prov_stored, prov_entry = find_provider_entry((cfg or {}).get("providers"), origin)
    disabled_now = _builtin_entry_disabled(prov_entry)
    self_referred = bool(origin) and origin == clone_key
    return {
        "builtin_entry_will_vanish": (
            isinstance(prov_entry, dict) and disabled_now and len(prov_entry) == 1),
        "clone_id": str(stored or ""),
        "model": keep_model,
        "model_replaced": model_replaced,
        "moved_top_model": bool(moved_top),
        "origin": origin,
        "origin_is_self": self_referred,
        "reenabled_builtin": bool(disabled_now and not self_referred),
    }


@router.post(RELEASE_PATH)
def release_clone(endpoint_id: str, body: Optional[EndpointReleaseRequest] = None,
                  profile: Optional[str] = None):
    """退回官方管理 = 摘掉克隆 + 恢复内置（设计 §4.3.3；M12.5）。**不走官方 delete**（拍板 D3）。

    为什么不走官方 delete：它 ``remove_env_value(custom_endpoint_key_env(id))`` 删的是按
    克隆 id 算出来的 ``HERMES_CUSTOM_MANAGED_<SLUG>_API_KEY`` —— 那把变量**从来就不在 .env**
    （接管时一个字节都没写过），删不存在的键只是「空转不重写文件」（``config.py:2594-2612``
    实测 ``found=False`` 分支），靠巧合保平安；而且官方 delete 不认 ``managed_from``、
    不会恢复内置。所以本路由是插件直写（与 M5 清空 / M6 迁移同一条通道）。

    **三拍顺序不可反**（proposal §5.2.2-3，防「无人接盘的窗口期」），且合**一次**
    ``save_config``，config 维度同样没有中间态：
      第 1 拍 安置顶层引用：``model.provider`` 正指着克隆 → provider 改指回内置 slug、
             默认模型按 :func:`_release_plan` 的保守判据留或换、摘掉三条镜像
             （端点 / 明文 / 变量名——内置自己从 env 解析密钥，镜像留着反而会把
             「官方换钥匙」这件事冻成一份旧值）→ ``moved_top_model: true``。
      第 2 拍 删克隆条目。
      第 3 拍 撤停用：``pop("enabled")``；撤完只剩空 dict → 整条摘掉（不留幽灵条目）。

    拒绝面（**全部零写入**）：非克隆条目（没有来源标记，或来源指向它自己）→ **400**；
    条目不存在 → **404**（复用 M5 的 :func:`_locate_providers_entry`，它把「其实在由
    cc-switch 管理的那段里」与「真不存在」分成两句原文）。
    ``body.precheck_only=true`` → 纯读返回 ``{blocks, plan}``，一次都不写盘。
    """
    identity = str(endpoint_id or "").strip()
    if not identity:
        raise HTTPException(
            status_code=400,
            detail="退回未执行：缺少供应商标识（路径里的 id 是空的）。列表未改动。")

    with _config_profile_scope(profile):
        cfg = load_config() or {}
    stored, entry = _locate_providers_entry(cfg, identity)
    plan = _release_plan(cfg, stored, entry)
    if not plan["origin"] or plan["origin_is_self"]:
        raise HTTPException(
            status_code=400,
            detail=(f"退回 {identity} 未执行：该供应商不是接管来的，没有可退回的官方入口。"
                    "列表未改动。"))

    if body is not None and body.precheck_only:
        blocks = [RELEASE_NOTE_TOP_MODEL] if plan["moved_top_model"] else []
        return {"blocks": blocks, "id": str(stored or identity), "ok": True,
                "plan": plan, "precheck_only": True}

    # 第 1 拍 · 安置顶层引用
    if plan["moved_top_model"]:
        model_cfg = cfg.get("model") if isinstance(cfg.get("model"), dict) else None
        if isinstance(model_cfg, dict):
            model_cfg["provider"] = plan["origin"]
            if plan["model"]:
                model_cfg["default"] = plan["model"]
            for mirror in RELEASE_TOP_MODEL_CLEARED_FIELDS:
                model_cfg.pop(mirror, None)
    # 第 2 拍 · 删克隆
    providers = _providers_map(cfg)
    if stored is not None:
        providers.pop(stored, None)
    # 第 3 拍 · 撤停用（只剩空开关就连条目一起摘掉）
    _prov_stored, prov_entry = find_provider_entry(providers, plan["origin"])
    if isinstance(prov_entry, dict) and prov_entry.pop("enabled", None) is not None:
        if not prov_entry and _prov_stored is not None:
            providers.pop(_prov_stored, None)
    # 落盘必须在**同一个** profile 作用域里（与 :func:`adopt_builtin_candidate` 同一笔
    # 更正）：写在作用域外会让带 profile 的请求落到默认配置上。三拍合一次原子写不变。
    with _config_profile_scope(profile):
        save_config(cfg)                                        # ← 单次原子写

    return _write_response(str(stored or identity), profile, {
        "released": {
            "builtin_reenabled": bool(plan["reenabled_builtin"]),
            "clone_removed": True,
            "moved_top_model": bool(plan["moved_top_model"]),
            "to_builtin": plan["origin"],
        },
    })
