"""API 请求 / 响应数据模型（Pydantic v2）。"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

Phase = Literal["A", "B", "C"]

_PHASE_ORDER = {"A": 0, "B": 1, "C": 2}


class Capacities(BaseModel):
    """A/B/C 各相容量（非负整数）。"""

    A: int = Field(..., ge=0, description="A 相容量")
    B: int = Field(..., ge=0, description="B 相容量")
    C: int = Field(..., ge=0, description="C 相容量")


class DeviceIn(BaseModel):
    """单台待分配设备。"""

    id: str = Field(..., min_length=1, max_length=64, description="设备唯一编号")
    power: int = Field(..., gt=0, description="设备功率（正整数）")
    allowed_phases: list[Phase] = Field(
        ..., min_length=1, description="可接入相位集合，非空，元素取自 A/B/C"
    )

    @field_validator("allowed_phases")
    @classmethod
    def _dedupe_and_order(cls, value: list[str]) -> list[str]:
        """去重并按 A/B/C 固定顺序排列。"""
        return sorted(set(value), key=_PHASE_ORDER.__getitem__)


class CalculationRequest(BaseModel):
    """创建一次排布计算的请求体。"""

    capacities: Capacities
    devices: list[DeviceIn] = Field(
        ..., min_length=1, max_length=12, description="设备列表，1~12 台"
    )

    @model_validator(mode="after")
    def _unique_device_ids(self) -> "CalculationRequest":
        ids = [d.id for d in self.devices]
        if len(ids) != len(set(ids)):
            raise ValueError("设备编号必须唯一")
        return self


class Loads(BaseModel):
    """A/B/C 各相负载。"""

    A: int
    B: int
    C: int


class AssignmentItem(BaseModel):
    """单台设备的分配结果。"""

    device_id: str
    phase: Phase


class CalculationSummary(BaseModel):
    """计算记录摘要（列表接口使用）。"""

    id: int
    created_at: str
    capacities: Loads
    feasible: bool
    imbalance: Optional[int] = None
    loads: Optional[Loads] = None


class CalculationResult(CalculationSummary):
    """计算结果（创建接口返回）。"""

    message: str
    assignments: list[AssignmentItem]


class DeviceRecord(BaseModel):
    """持久化的设备输入及其分配结果。"""

    device_id: str
    power: int
    allowed_phases: list[str]
    assigned_phase: Optional[Phase] = None


class CalculationDetail(CalculationResult):
    """计算记录详情（含原始输入）。"""

    devices: list[DeviceRecord]


class CapacityUpgradeResult(BaseModel):
    """容量扩容建议结果（原计算记录不做任何修改）。"""

    id: int
    created_at: str
    original_capacities: Loads = Field(..., description="原各相容量")
    capacity_upgrades: Loads = Field(..., description="各相需要增加的非负整数容量")
    suggested_capacities: Loads = Field(..., description="扩容后的建议容量")
    total_upgrade: int = Field(..., ge=0, description="三相扩容总量")
    max_upgrade: int = Field(..., ge=0, description="单相最大扩容量")
    assignments: list[AssignmentItem] = Field(..., description="扩容后的设备分配")
    loads: Loads = Field(..., description="建议容量下各相的实际负载")
    message: str


class RobustDeviceIn(BaseModel):
    """稳健预演的单台设备：功率为正整数区间 [min_power, max_power]。"""

    id: str = Field(..., min_length=1, max_length=64, description="设备唯一编号")
    min_power: int = Field(..., gt=0, description="功率下界（正整数）")
    max_power: int = Field(..., gt=0, description="功率上界（正整数）")
    allowed_phases: list[Phase] = Field(
        ..., min_length=1, description="可接入相位集合，非空，元素取自 A/B/C"
    )

    @field_validator("allowed_phases")
    @classmethod
    def _dedupe_and_order(cls, value: list[str]) -> list[str]:
        """去重并按 A/B/C 固定顺序排列。"""
        return sorted(set(value), key=_PHASE_ORDER.__getitem__)

    @model_validator(mode="after")
    def _min_not_above_max(self) -> "RobustDeviceIn":
        if self.min_power > self.max_power:
            raise ValueError("功率下界不能大于上界")
        return self


class RobustRehearsalRequest(BaseModel):
    """稳健分相预演请求体（仅预演，不写入历史计算记录）。"""

    capacities: Capacities
    devices: list[RobustDeviceIn] = Field(
        ..., min_length=1, max_length=12, description="设备列表，1~12 台"
    )

    @model_validator(mode="after")
    def _unique_device_ids(self) -> "RobustRehearsalRequest":
        ids = [d.id for d in self.devices]
        if len(ids) != len(set(ids)):
            raise ValueError("设备编号必须唯一")
        return self


class RobustRehearsalResult(BaseModel):
    """稳健分相预演结果。"""

    capacities: Loads = Field(..., description="各相容量（回显请求输入）")
    feasible: bool = Field(..., description="是否存在稳健可行分配")
    worst_imbalance: Optional[int] = Field(
        None, description="任意功率组合下三相最大负载与最小负载之差的最大值"
    )
    load_lower_bounds: Optional[Loads] = Field(
        None, description="各相负载下界（该相设备均取功率下界之和）"
    )
    load_upper_bounds: Optional[Loads] = Field(
        None, description="各相负载上界（该相设备均取功率上界之和）"
    )
    assignments: list[AssignmentItem] = Field(
        ..., description="稳健分配方案（按设备编号排序）；无解时为空"
    )
    message: str


class StartupDeviceIn(BaseModel):
    """启动预演请求中的单台设备：须逐台给出启动瞬时功率。"""

    id: str = Field(..., min_length=1, max_length=64, description="设备唯一编号")
    startup_power: int = Field(
        ..., gt=0, description="启动瞬时功率（正整数，且不低于该设备额定功率）"
    )


class StartupPlanRequest(BaseModel):
    """启动顺序预演请求体（仅预演，不写入历史计算记录）。

    设备编号须与原计算记录完全一致（逐台给出、不重不漏、顺序任意）；
    各设备启动瞬时功率是否不低于额定功率需结合记录校验，不满足返回 422。
    """

    devices: list[StartupDeviceIn] = Field(
        ..., min_length=1, max_length=12, description="逐台设备的启动瞬时功率"
    )

    @model_validator(mode="after")
    def _unique_device_ids(self) -> "StartupPlanRequest":
        ids = [d.id for d in self.devices]
        if len(ids) != len(set(ids)):
            raise ValueError("设备编号必须唯一")
        return self


class StartupStepItem(BaseModel):
    """一次启动的负载快照：启动瞬间与启动完成后的稳定状态。"""

    step: int = Field(..., ge=1, description="第几步启动（从 1 开始）")
    device_id: str = Field(..., description="本步启动的设备编号")
    startup_loads: Loads = Field(
        ..., description="启动瞬间各相负载（该设备按启动瞬时功率计）"
    )
    stable_loads: Loads = Field(
        ..., description="启动完成后各相稳定负载（该设备按额定功率计）"
    )


class StartupPlanResult(BaseModel):
    """启动顺序预演结果。"""

    id: int = Field(..., description="原计算记录 id")
    created_at: str
    capacities: Loads = Field(..., description="记录沿用的各相容量")
    feasible: bool = Field(..., description="是否存在安全启动顺序")
    order: Optional[list[str]] = Field(
        None, description="最优启动顺序（设备编号序列）；无解时为 null"
    )
    steps: Optional[list[StartupStepItem]] = Field(
        None,
        description="逐步负载快照（启动瞬间与稳定状态，均不超容量）；无解时为 null",
    )
    peak: Optional[int] = Field(
        None,
        description="全过程各启动瞬间与稳定状态中三相最大负载与最小负载之差的峰值",
    )
    message: str
