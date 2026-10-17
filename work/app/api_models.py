"""请求/响应模型（ARCHITECTURE §5.1 与 §3 的写入形状）。

刻意的取舍：**只把“请求体”建模为 Pydantic 模型，响应一律手写 dict**。
理由是响应形状要和 ``docs/contracts/ARCHITECTURE.md`` §5 逐字段对应、且字段随运行状态变化
（``stats``/``warnings``/``result``），用模型反而会多一层需要同步维护的映射。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class Credentials(BaseModel):
    """``/api/auth/setup`` 与 ``/api/auth/login`` 的请求体。"""

    username: str
    password: str


class PasswordChange(BaseModel):
    """``/api/auth/password`` 的请求体。"""

    old_password: str
    new_password: str


class Scope(BaseModel):
    """运行范围（§3.9）。``mode='folder'`` 时 ``folder`` 必须是一级文件夹名。"""

    mode: Literal["full", "folder"] = "full"
    folder: str | None = None


class RunRequest(BaseModel):
    """``POST /api/libraries/{id}/run`` 的请求体；整体可缺省。"""

    scope: Scope = Field(default_factory=Scope)
    force: bool = False


class WorkSelection(BaseModel):
    """作品级操作的请求体：``folders`` = 选中的作品（目标目录一级文件夹名）。

    ``folders`` 里的名字必须是**一级文件夹名**，路由层会用
    ``app/link_records.py::safe_target_path`` 再验一次（越界/带分隔符的一律拒绝）。
    """

    folders: list[str] = Field(default_factory=list)


class WorkDeleteRequest(WorkSelection):
    """``POST /api/libraries/{id}/works/delete`` 的请求体。

    * ``dry_run=True``：只回执"会删什么"（路径、文件数、非硬链接文件数），不动盘；
    * ``confirm=False``（且 ``dry_run=False``）：**拒绝执行** —— 这是删除整个作品文件夹的
      破坏性操作，必须由用户显式确认（WebUI 的确认框）。
    """

    confirm: bool = False
    dry_run: bool = False


class LibraryInput(BaseModel):
    """§5.1 / PROCESSORS §6.3 LibraryInput（``POST``/``PUT /api/libraries``）。

    **不再有枚举约束的 ``type``/``processor``**。

    * ``processor_id`` 是外键（``processors.id``），必须存在 —— 由路由层查注册表校验
      （Pydantic 里查库会把"数据错误"和"形状错误"混成一类，且注册表要读 DB）；
    * ``processor`` 作为 **v1 兼容别名**保留：老客户端/老测试传 ``processor=bangumi`` 时
      会被 ``model_validator`` 提升成 ``processor_id``（v1 的三个取值与内置处理器 id
      逐一相同，§10 就是这么映射的）；
    * ``type`` 变成自由标签（前端展示用），缺省由处理器派生（``defaults`` 里给；
      这里留 ``None`` 表示"按处理器推断"）；
    * ``log_file`` 缺省时由注册表自动生成 ``<slug>cache.log``；只允许文件名。
    * ``rule_override`` 是本库自己的规则覆盖：形如
      ``{"extensions": [...], "traverse": {...}, "naming": {"template": "{title} ..."}}``。
      留 ``None`` / 空对象 = 完全跟随处理器；只填过的字段才覆盖（
      ``app/processors/overrides.py`` 是唯一实现）。
    """

    name: str
    processor_id: str | None = None
    #: v1 兼容别名（与 processor_id 二选一；同时给时以 processor_id 为准）。
    processor: str | None = None
    type: str | None = None
    log_file: str | None = None
    enabled: bool = True
    source_dir: str
    target_dir: str
    match_categories: list[str] = Field(default_factory=list)
    match_tags: list[str] = Field(default_factory=list)
    match_path_prefixes: list[str] = Field(default_factory=list)
    orphan_cleanup: bool = True
    permission_mode: bool = False
    rule_override: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _fill_processor_id(self) -> "LibraryInput":
        """把 v1 的 ``processor`` 别名提升为 ``processor_id``（就地改自身，不改库）。"""
        if not (self.processor_id or "").strip() and (self.processor or "").strip():
            self.processor_id = (self.processor or "").strip()
        elif (self.processor_id or "").strip():
            self.processor_id = (self.processor_id or "").strip()
        return self


class QbTestRequest(BaseModel):
    """``POST /api/settings/test-qbittorrent`` 的可选请求体。

    三个字段都可缺省：缺省即“用已保存的设置”。``password`` 传 ``"***"`` 时用库里存的真值。
    """

    url: str | None = None
    username: str | None = None
    password: str | None = None


class SettingsPatch(BaseModel):
    """``PUT /api/settings``：部分更新。只允许已知键（多余键忽略，避免写进垃圾）。"""

    model_config = {"extra": "allow"}

    def known_updates(self, allowed: set[str]) -> dict[str, Any]:
        """取出白名单内的键值，值统一转成字符串（设置表是纯文本表）。"""
        updates: dict[str, Any] = {}
        for key, value in self.model_dump(exclude_unset=True).items():
            if key in allowed and value is not None:
                updates[key] = value
        return updates


class ProcessorCreate(BaseModel):
    """``POST /api/processors`` 的请求体（PROCESSORS §7）。

    ``template`` ∈ ``movie`` / ``bangumi`` / ``hardlink_only`` / ``blank``：以某个内置规格为
    起点；也可以直接给 ``spec``（整份规格）覆盖模板结果。``schema_version`` 由服务端写。
    """

    id: str
    name: str = ""
    kind: str | None = None
    template: str = "blank"
    spec: dict[str, Any] | None = None


class ProcessorPreset(BaseModel):
    """``POST /api/processors/{id}/reset`` 的请求体。"""

    preset: str = "default"
