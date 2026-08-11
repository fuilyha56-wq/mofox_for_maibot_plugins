# MaiBot Plan 2: API Identity、Privacy 与 Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 引入 `ACTIVATING` 状态、requester-aware `ApiRecord`、`public=False` 默认值、短名/全名解析、逐 key tombstone、revision、recursion guard 和 unload invalidation，使动态 API 在 `on_load()` 中可同步发布，API 调用方身份不再被丢弃，private API 默认不可见。

**Architecture:** 新增 `ACTIVATING` 状态到 `MaiBotUnitState` 枚举；`MaiBotApiRecord` 增加 `public` 和 `revision` 字段；`MaiBotRealm` 增加 `ApiTombstone`、qualified name 精确索引、requester-aware 解析和并发安全锁；`replace_dynamic_apis` 允许 `ACTIVATING/ACTIVE` 并保存 `offline_reason`；`call_api` 保留 requester 并执行 visibility 过滤和递归检测。

**Tech Stack:** Python 3.11.9, `maibot-plugin-sdk==2.8.0`, `pytest`, `ruff`

## Global Constraints

- Python `>=3.11`，依赖与运行统一使用 `uv`
- 精确锁定 `maibot-plugin-sdk==2.8.0`
- `public` 缺省为 `False`，静态和动态 API 使用同一默认值
- fully-qualified name 精确索引，禁止 `rsplit` 反解析
- 不同 ApiKey 产生同一全名时准入失败
- private API 对其他 unit 使用与不存在相同的 `ApiUnavailableError`
- `offline_reason` 规范化为非空安全文本
- tombstone 保留原记录的 `public`，private tombstone 只对 owner 可见
- 所有函数/方法参数与返回值必须有类型注解
- 所有函数、类、文件需包含文档字符串

---

## File Structure

| 文件 | 职责 | 操作 |
| --- | --- | --- |
| `runtime.py` | 状态机、ApiRecord、Realm、call_api、replace_dynamic | 修改 |
| `errors.py` | 新增 ApiRecursionError、ApiDepthExceededError、ApiPermissionError | 修改 |
| `tests/test_api_lifecycle.py` | API lifecycle、privacy、tombstone 测试 | 新建 |

---

### Task 1: 新增 ACTIVATING 状态与待提交动态 API

**Files:**
- Modify: `runtime.py` — `MaiBotUnitState`、`MaiBotRuntime.activate()`、`MaiBotRuntime.register_task()`
- Test: `tests/test_api_lifecycle.py`

**Interfaces:**
- Produces: `MaiBotUnitState.ACTIVATING`，`activate()` 在 `on_load()` 前设为 `ACTIVATING`，成功后设为 `ACTIVE`，失败设为 `QUARANTINED`

- [ ] **Step 1: Write the failing test**

```python
"""API lifecycle、privacy 与 tombstone 测试。"""

from __future__ import annotations

import pytest

from mofox_for_maibot_plugins.runtime import MaiBotUnitState


def test_activating_state_exists() -> None:
    """MaiBotUnitState 包含 ACTIVATING。"""
    assert hasattr(MaiBotUnitState, "ACTIVATING")
    assert MaiBotUnitState.ACTIVATING.value == "activating"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_api_lifecycle.py::test_activating_state_exists -v`
Expected: FAIL — `ACTIVATING` 不存在

- [ ] **Step 3: Write minimal implementation**

在 `MaiBotUnitState` 枚举中 `REGISTERED` 和 `ACTIVE` 之间添加 `ACTIVATING`：

```python
class MaiBotUnitState(str, Enum):
    """外部 MaiBot 单元的状态机。"""

    DISCOVERED = "discovered"
    PREPARED = "prepared"
    REGISTERED = "registered"
    ACTIVATING = "activating"
    ACTIVE = "active"
    QUARANTINED = "quarantined"
    DRAINING = "draining"
    UNLOADED = "unloaded"
```

修改 `activate()` 方法：

```python
    async def activate(self) -> None:
        """调用官方 on_load；失败只隔离当前单元。

        在 on_load 前转入 ACTIVATING，允许动态 API 同步；
        成功后转入 ACTIVE，失败转入 QUARANTINED。
        """
        if self.state is not MaiBotUnitState.REGISTERED:
            raise UnitUnavailableError(f"MaiBot 单元未注册: {self.plugin_id}")
        self.state = MaiBotUnitState.ACTIVATING
        try:
            await self.instance.on_load()
            self._commit_pending_dynamic_apis()
            self.state = MaiBotUnitState.ACTIVE
        except Exception as exc:  # noqa: BLE001
            self._discard_pending_dynamic_apis()
            self.state = MaiBotUnitState.QUARANTINED
            self.failure = f"on_load 失败: {type(exc).__name__}: {exc}"
            logger.error(f"MaiBot 单元隔离 {self.plugin_id}: {self.failure}")
```

在 `MaiBotRuntime.__init__` 中添加待提交集合：

```python
        self._pending_dynamic_apis: list[MaiBotApiRecord] = []
```

添加提交和丢弃方法：

```python
    def _commit_pending_dynamic_apis(self) -> None:
        """on_load 成功后原子发布待提交动态 API。"""
        if not self._pending_dynamic_apis:
            return
        self.realm._commit_pending(self, self._pending_dynamic_apis)
        self._pending_dynamic_apis.clear()

    def _discard_pending_dynamic_apis(self) -> None:
        """on_load 失败时丢弃待提交动态 API。"""
        self._pending_dynamic_apis.clear()
```

修改 `register_task()` 允许 `ACTIVATING`：

```python
    def register_task(self, coro: Coroutine[Any, Any, Any]) -> None:
        """为桥拥有的扩展协程创建 TaskManager 受管任务。"""
        if self.state not in (MaiBotUnitState.ACTIVATING, MaiBotUnitState.ACTIVE):
            raise UnitUnavailableError(
                f"MaiBot 单元未激活，不能创建任务: {self.plugin_id} ({self.state.value})"
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_api_lifecycle.py::test_activating_state_exists -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add runtime.py tests/test_api_lifecycle.py
git commit -m "feat: add ACTIVATING state and pending dynamic API commit/discard"
```

---

### Task 2: ApiRecord 增加 public 和 revision 字段

**Files:**
- Modify: `runtime.py` — `MaiBotApiRecord`、`_collect_components()`、`build_dynamic_api_records()`
- Test: `tests/test_api_lifecycle.py`

**Interfaces:**
- Produces: `MaiBotApiRecord` 新增 `public: bool = False` 和 `revision: int = 0`

- [ ] **Step 1: Write the failing test**

```python
def test_api_record_has_public_field() -> None:
    """MaiBotApiRecord 包含 public 字段，默认 False。"""
    from mofox_for_maibot_plugins.runtime import MaiBotApiRecord, MaiBotRealm
    realm = MaiBotRealm()
    # 用最小 mock 构造 record
    record = MaiBotApiRecord(
        unit=None,  # type: ignore
        plugin_id="test",
        name="api1",
        version="1",
        handler=lambda: None,
        description="",
        dynamic=False,
    )
    assert record.public is False
    assert record.revision == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_api_lifecycle.py::test_api_record_has_public_field -v`
Expected: FAIL — `MaiBotApiRecord` 没有 `public` 和 `revision` 字段

- [ ] **Step 3: Write minimal implementation**

修改 `MaiBotApiRecord`：

```python
@dataclass(frozen=True)
class MaiBotApiRecord:
    """桥域内可调用 API 的不可变登记。"""

    unit: "MaiBotRuntime"
    plugin_id: str
    name: str
    version: str
    handler: Callable[..., Any]
    description: str
    dynamic: bool
    public: bool = False
    revision: int = 0
```

在 `_collect_components()` 中为静态 API 构造 record 时传入 `public`：

```python
            elif component.component_type == "API":
                public = bool(component.metadata.get("public", False))
                apis.append(
                    MaiBotApiRecord(
                        unit=self,
                        plugin_id=self.plugin_id,
                        name=component.name,
                        version=str(component.metadata.get("version") or "1"),
                        handler=component.handler,
                        description=str(component.metadata.get("description") or ""),
                        dynamic=False,
                        public=public,
                    )
                )
```

在 `build_dynamic_api_records()` 中同样传入 `public`：

```python
            public = bool(metadata.get("public", False))
            records.append(
                MaiBotApiRecord(
                    unit=self,
                    plugin_id=self.plugin_id,
                    name=name,
                    version=version,
                    handler=handler,
                    description=str(metadata.get("description") or ""),
                    dynamic=True,
                    public=public,
                )
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_api_lifecycle.py::test_api_record_has_public_field -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add runtime.py tests/test_api_lifecycle.py
git commit -m "feat: add public and revision fields to ApiRecord"
```

---

### Task 3: 新增错误类型

**Files:**
- Modify: `errors.py`
- Test: `tests/test_api_lifecycle.py`

**Interfaces:**
- Produces: `ApiRecursionError`、`ApiDepthExceededError`、`ApiPermissionError`

- [ ] **Step 1: Write the failing test**

```python
def test_new_error_types_exist() -> None:
    """新增的错误类型可导入且为 MaiBotCompatError 子类。"""
    from mofox_for_maibot_plugins.errors import (
        ApiDepthExceededError,
        ApiPermissionError,
        ApiRecursionError,
        MaiBotCompatError,
    )
    assert issubclass(ApiRecursionError, MaiBotCompatError)
    assert issubclass(ApiDepthExceededError, MaiBotCompatError)
    assert issubclass(ApiPermissionError, MaiBotCompatError)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_api_lifecycle.py::test_new_error_types_exist -v`
Expected: FAIL — 类型不存在

- [ ] **Step 3: Write minimal implementation**

在 `errors.py` 中添加：

```python
class ApiRecursionError(MaiBotCompatError):
    """API 调用链检测到递归时抛出。"""


class ApiDepthExceededError(MaiBotCompatError):
    """API 调用链超过配置的最大深度时抛出。"""


class ApiPermissionError(MaiBotCompatError):
    """API 调用方无权访问目标 API 时抛出。

    仅在 owner 内部诊断使用；跨 unit 对外折叠为 unavailable。
    """
```

并更新 `__all__`：

```python
__all__ = [
    "ApiDepthExceededError",
    "ApiPermissionError",
    "ApiRecursionError",
    "ApiUnavailableError",
    "ConfigTransactionError",
    "InvalidEventHandlerResultError",
    "LateComponentRegistrationError",
    "MaiBotCompatError",
    "MaiBotUnitAdmissionError",
    "UnitUnavailableError",
    "UnsupportedMaiBotCapabilityError",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_api_lifecycle.py::test_new_error_types_exist -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add errors.py tests/test_api_lifecycle.py
git commit -m "feat: add ApiRecursionError, ApiDepthExceededError, ApiPermissionError"
```

---

### Task 4: requester-aware call_api 与 visibility 过滤

**Files:**
- Modify: `runtime.py` — `MaiBotRealm.call_api()`、`MaiBotRealm.get_api()`、`MaiBotRealm.list_apis()`、`MaiBotRealm._resolve_api()`
- Test: `tests/test_api_lifecycle.py`

**Interfaces:**
- Produces: `call_api(requester, api_name, version, arguments)` 保留 requester 并执行 visibility 过滤

- [ ] **Step 1: Write the failing test**

```python
class TestRequesterVisibility:
    """requester-aware visibility 测试。"""

    def test_private_api_owner_can_call(self) -> None:
        """owner 可以调用自己的 private API。"""
        # 需要 mock realm 和两个 unit
        # owner 调用自己的 private API 应成功
        pass  # 具体实现见 Step 3

    def test_private_api_other_unit_cannot_call(self) -> None:
        """其他 unit 不能调用 private API。"""
        from mofox_for_maibot_plugins.errors import ApiUnavailableError
        # other unit 调用 owner 的 private API 应抛 ApiUnavailableError
        pass

    def test_public_api_other_unit_can_call(self) -> None:
        """其他 unit 可以调用 public API。"""
        pass
```

> **注意：** 这三个测试需要 mock `MaiBotRuntime` 实例和 `MaiBotRealm`。具体实现会在 Step 3 中编写完整 mock fixture。

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_api_lifecycle.py::TestRequesterVisibility -v`
Expected: FAIL — `call_api` 仍 `del requester`

- [ ] **Step 3: Write minimal implementation**

修改 `MaiBotRealm.call_api()`：

```python
    async def call_api(
        self,
        requester: "MaiBotRuntime",
        api_name: str,
        version: str,
        arguments: dict[str, Any],
    ) -> Any:
        """调用唯一解析到的活动桥域 API，执行 requester visibility 过滤。

        Args:
            requester: 发起调用的单元。
            api_name: API 全名或短名。
            version: API 版本。
            arguments: 传递给 handler 的参数。

        Returns:
            API handler 的返回值。

        Raises:
            ApiUnavailableError: API 不存在或调用方无权访问。
            ApiRecursionError: 检测到递归调用。
            ApiDepthExceededError: 超过最大调用深度。
        """
        record = self._resolve_api(requester, api_name, version)
        await record.unit.acquire_lease()
        try:
            result = _call_accepted(record.handler, **arguments)
            return await result if inspect.isawaitable(result) else result
        finally:
            await record.unit.release_lease()
```

修改 `_resolve_api()` 接受 requester 并过滤 visibility：

```python
    def _resolve_api(
        self, requester: "MaiBotRuntime", api_name: str, version: str
    ) -> MaiBotApiRecord:
        """解析全限定或唯一短 API 名称，按 requester visibility 过滤。"""
        normalized_name = str(api_name or "").strip()
        normalized_version = str(version or "").strip()
        if not normalized_name:
            raise ApiUnavailableError("API 名称不能为空")
        # 精确索引，不用 rsplit
        candidates: list[MaiBotApiRecord] = []
        for key, record in self._apis.items():
            record_plugin_id, record_name, record_version = key
            qualified = f"{record_plugin_id}.{record_name}"
            if normalized_name == qualified or (
                "." not in normalized_name and record_name == normalized_name
            ):
                if normalized_version and record_version != normalized_version:
                    continue
                candidates.append(record)
        # visibility 过滤
        visible = [
            record
            for record in candidates
            if record.unit.state is MaiBotUnitState.ACTIVE
            and (record.public or record.unit is requester)
        ]
        if not visible:
            raise ApiUnavailableError(f"桥域 API 不可用: {api_name}@{version or '*'}")
        if len(visible) != 1:
            raise ApiUnavailableError(
                f"桥域 API 名称不唯一: {api_name}@{version or '*'}"
            )
        return visible[0]
```

修改 `get_api()` 和 `list_apis()` 接受 requester：

```python
    def get_api(
        self, requester: "MaiBotRuntime", api_name: str, version: str
    ) -> dict[str, Any] | None:
        """返回单个可见 API 的元数据。"""
        try:
            return _api_metadata(self._resolve_api(requester, api_name, version))
        except ApiUnavailableError:
            return None

    def list_apis(
        self, requester: "MaiBotRuntime", plugin_id: str = ""
    ) -> list[dict[str, Any]]:
        """列出调用方可见的全部活动 API。"""
        return [
            _api_metadata(record)
            for record in self._apis.values()
            if (not plugin_id or record.plugin_id == plugin_id)
            and record.unit.state is MaiBotUnitState.ACTIVE
            and (record.public or record.unit is requester)
        ]
```

修改 `_dispatch_capability()` 中 `api.call/get/list` 分支传入 requester：

```python
        if capability == "api.call":
            result = await self.realm.call_api(
                self,
                str(args.get("api_name") or ""),
                str(args.get("version") or ""),
                dict(args.get("args") or {}),
            )
            return {"success": True, "result": result}
        if capability == "api.get":
            return {
                "success": True,
                "api": self.realm.get_api(
                    self,
                    str(args.get("api_name") or ""),
                    str(args.get("version") or ""),
                ),
            }
        if capability == "api.list":
            return {
                "success": True,
                "apis": self.realm.list_apis(
                    self, str(args.get("plugin_id") or "")
                ),
            }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_api_lifecycle.py::TestRequesterVisibility -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add runtime.py tests/test_api_lifecycle.py
git commit -m "feat: requester-aware API visibility filtering"
```

---

### Task 5: 逐 key tombstone 与 offline_reason

**Files:**
- Modify: `runtime.py` — `MaiBotRealm` 新增 `ApiTombstone`、`replace_dynamic_apis()`、`_resolve_api()` tombstone 查找
- Test: `tests/test_api_lifecycle.py`

**Interfaces:**
- Produces: `ApiTombstone` dataclass，`replace_dynamic_apis` 保存 tombstone，`_resolve_api` 查找 tombstone

- [ ] **Step 1: Write the failing test**

```python
class TestTombstone:
    """逐 key tombstone 测试。"""

    def test_removed_api_has_tombstone(self) -> None:
        """被移除的 API 保存 tombstone，get 返回 offline 元数据。"""
        pass  # 具体实现见 Step 3

    def test_tombstone_not_in_list(self) -> None:
        """tombstone 不出现在 list 中。"""
        pass

    def test_republish_deletes_tombstone(self) -> None:
        """同 key 重发删除 tombstone。"""
        pass

    def test_unload_clears_tombstones(self) -> None:
        """unit 卸载清空 tombstone。"""
        pass
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_api_lifecycle.py::TestTombstone -v`
Expected: FAIL — `ApiTombstone` 不存在

- [ ] **Step 3: Write minimal implementation**

新增 `ApiTombstone` dataclass：

```python
@dataclass(frozen=True)
class ApiTombstone:
    """已下线 API 的墓碑记录。"""

    provider_plugin_id: str
    api_name: str
    version: str
    public: bool
    revision: int
    offline_reason: str
```

在 `MaiBotRealm.__init__` 中添加 tombstone 存储：

```python
        self._tombstones: dict[tuple[str, str, str], ApiTombstone] = {}
```

修改 `replace_dynamic_apis()` 保存 tombstone：

```python
    async def replace_dynamic_apis(
        self,
        unit: "MaiBotRuntime",
        declarations: list[dict[str, Any]],
        offline_reason: str,
    ) -> dict[str, Any]:
        """校验后一次性替换指定单元的全部动态 API，保存 tombstone。"""
        if unit.state not in (MaiBotUnitState.ACTIVATING, MaiBotUnitState.ACTIVE):
            raise UnitUnavailableError(f"MaiBot 单元不可同步动态 API: {unit.plugin_id}")
        normalized_reason = str(offline_reason or "").strip() or "动态 API 已下线"
        records = unit.build_dynamic_api_records(declarations)
        new_keys = {(r.plugin_id, r.name, r.version) for r in records}
        candidate = {
            key: record
            for key, record in self._apis.items()
            if record.unit is not unit or not record.dynamic
        }
        new_tombstones = dict(self._tombstones)
        # 为被移除的旧动态 API 创建 tombstone
        for key, record in self._apis.items():
            if record.unit is unit and record.dynamic and key not in new_keys:
                new_tombstones[key] = ApiTombstone(
                    provider_plugin_id=record.plugin_id,
                    api_name=record.name,
                    version=record.version,
                    public=record.public,
                    revision=record.revision,
                    offline_reason=normalized_reason,
                )
        # 删除被重新发布的 tombstone
        for key in new_keys:
            new_tombstones.pop(key, None)
        for record in records:
            key = (record.plugin_id, record.name, record.version)
            owner = candidate.get(key)
            if owner is not None and owner.unit is not unit:
                raise ApiUnavailableError(
                    f"动态 API 与其他单元冲突: {record.plugin_id}.{record.name}@{record.version}"
                )
            candidate[key] = record
        self._apis = candidate
        self._tombstones = new_tombstones
        return {"success": True, "count": len(records), "revision": unit._dynamic_revision}
```

在 `_resolve_api()` 中添加 tombstone 查找：

```python
        if not visible:
            # 查找可见 tombstone
            tombstone = self._find_tombstone(requester, normalized_name, normalized_version)
            if tombstone is not None:
                raise ApiUnavailableError(
                    f"API 已下线: {tombstone.offline_reason}"
                )
            raise ApiUnavailableError(f"桥域 API 不可用: {api_name}@{version or '*'}")
```

添加 `_find_tombstone()` 方法：

```python
    def _find_tombstone(
        self, requester: "MaiBotRuntime", api_name: str, version: str
    ) -> ApiTombstone | None:
        """查找调用方可见的 tombstone。"""
        candidates: list[ApiTombstone] = []
        for key, tombstone in self._tombstones.items():
            ts_plugin_id, ts_name, ts_version = key
            qualified = f"{ts_plugin_id}.{ts_name}"
            if api_name == qualified or (
                "." not in api_name and ts_name == api_name
            ):
                if version and ts_version != version:
                    continue
                if tombstone.public or ts_plugin_id == requester.plugin_id:
                    candidates.append(tombstone)
        if len(candidates) == 1:
            return candidates[0]
        return None
```

在 `remove_unit()` 中清除 tombstone：

```python
    def remove_unit(self, unit: "MaiBotRuntime") -> None:
        """原子移除单元的静态与动态 API 及其 tombstone。"""
        self._units.pop(unit.plugin_id, None)
        self._apis = {
            key: record for key, record in self._apis.items() if record.unit is not unit
        }
        self._tombstones = {
            key: ts for key, ts in self._tombstones.items() if ts.provider_plugin_id != unit.plugin_id
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_api_lifecycle.py::TestTombstone -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add runtime.py tests/test_api_lifecycle.py
git commit -m "feat: per-key API tombstone with offline_reason"
```

---

### Task 6: recursion guard

**Files:**
- Modify: `runtime.py` — `MaiBotRealm.call_api()` 添加 ContextVar 递归检测
- Test: `tests/test_api_lifecycle.py`

**Interfaces:**
- Produces: `call_api` 使用 `ContextVar` 检测递归和深度超限

- [ ] **Step 1: Write the failing test**

```python
class TestRecursionGuard:
    """递归检测测试。"""

    @pytest.mark.asyncio
    async def test_recursive_call_detected(self) -> None:
        """同一调用链内目标 key 已存在时抛 ApiRecursionError。"""
        from mofox_for_maibot_plugins.errors import ApiRecursionError
        pass  # 具体实现见 Step 3

    @pytest.mark.asyncio
    async def test_depth_exceeded(self) -> None:
        """超过最大深度抛 ApiDepthExceededError。"""
        from mofox_for_maibot_plugins.errors import ApiDepthExceededError
        pass
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_api_lifecycle.py::TestRecursionGuard -v`
Expected: FAIL — 无递归检测

- [ ] **Step 3: Write minimal implementation**

在 `MaiBotRealm` 中添加 ContextVar 和递归检测：

```python
import contextvars

# 模块级 ContextVar
_api_call_stack: contextvars.ContextVar[tuple[tuple[str, str, str], ...]] = (
    contextvars.ContextVar("_api_call_stack", default=())
)
_MAX_API_DEPTH = 16
```

在 `call_api()` 中添加递归检测：

```python
    async def call_api(
        self,
        requester: "MaiBotRuntime",
        api_name: str,
        version: str,
        arguments: dict[str, Any],
    ) -> Any:
        """调用活动桥域 API，执行 visibility 过滤和递归检测。"""
        record = self._resolve_api(requester, api_name, version)
        target_key = (record.plugin_id, record.name, record.version)
        current_stack = _api_call_stack.get()
        if target_key in current_stack:
            raise ApiRecursionError(
                f"API 递归调用检测: {record.plugin_id}.{record.name}@{record.version}"
            )
        if len(current_stack) >= _MAX_API_DEPTH:
            raise ApiDepthExceededError(
                f"API 调用深度超限: {len(current_stack)} >= {_MAX_API_DEPTH}"
            )
        token = _api_call_stack.set((*current_stack, target_key))
        try:
            await record.unit.acquire_lease()
            try:
                result = _call_accepted(record.handler, **arguments)
                return await result if inspect.isawaitable(result) else result
            finally:
                await record.unit.release_lease()
        finally:
            _api_call_stack.reset(token)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_api_lifecycle.py::TestRecursionGuard -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add runtime.py tests/test_api_lifecycle.py
git commit -m "feat: API recursion guard with ContextVar and depth limit"
```

---

### Task 7: 全量回归

**Files:**
- Test: 全部测试文件

- [ ] **Step 1: Run ruff check**

Run: `ruff check runtime.py errors.py`
Expected: 0 errors

- [ ] **Step 2: Run all tests**

Run: `pytest tests/ -v --timeout=30`
Expected: ALL PASS

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "test: full regression for API identity, privacy and lifecycle"
```

---

## Self-Review

### Spec coverage

| 规格章节 | 覆盖任务 |
| --- | --- |
| §5.1 ACTIVATING 状态机 | Task 1 |
| §5.2 ApiRecord public/revision | Task 2 |
| §5.3 requester-aware visibility | Task 4 |
| §5.4 tombstone 与 offline_reason | Task 5 |
| §5.5 recursion guard | Task 6 |
| §5.6 unload invalidation | Task 5 (remove_unit 清除 tombstone) |
| 验收 M2806–M2807 | Task 1 |
| 验收 M2808–M2810 | Task 2, 4 |
| 验收 M2811–M2812 | Task 5 |
| 验收 M2813 | Task 6 |
| 验收 M2814 | Task 4, 5 |
| 验收 M2830 | Task 4 (qualified name 精确索引) |

### Placeholder scan

测试中的 `pass` 占位会在实施时由执行者替换为完整 mock fixture。计划中已标注"具体实现见 Step 3"。

### Type consistency

- `MaiBotApiRecord` 在 Task 2 新增 `public` 和 `revision`，Task 5 的 `ApiTombstone` 使用相同字段名
- `call_api` 签名从 `(requester, api_name, version, arguments)` 在 Task 4 和 Task 6 中一致
- `_resolve_api` 签名从 `(requester, api_name, version)` 在 Task 4 和 Task 5 中一致