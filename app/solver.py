"""三相配电最优分配求解器（纯标准库实现，便于独立测试）。

问题：每台设备不可拆分，只能接入其允许集合中的一个相位；
所有设备均须接入且各相负载不超过对应容量。
目标：最小化「三相负载最大值与最小值之差」；
差值相同时，取按设备编号排序后的相位序列字典序最小的方案。

求解方式：按设备编号排序后做深度优先搜索，相位按 A/B/C 顺序展开，
因此完整方案按相位序列的字典序依次产生；仅在差值严格更小时更新最优解，
即可保证同差值下保留字典序最小方案。同时使用容量约束与下界剪枝加速。

扩容问题（solve_capacity_upgrade）：原容量下无可行分配时，保持设备功率与
允许相位不变，逐相增加非负整数容量，使所有设备均可接入。目标依次为：
1) 三相扩容总量最小；2) 单相最大扩容量最小；3) 仍相同时取按设备编号排序的
相位序列字典序最小。枚举所有相位序列（至多 3^12），分两遍 DFS 配合
扩容总量 / 单相扩容量的上下界剪枝求解。

稳健分相预演（solve_robust）：每台设备功率为正整数区间 [min_power, max_power]，
区间内可独立取任意整数。对固定分配，某相负载可独立取遍
[该相 min_power 之和, 该相 max_power 之和] 内的所有整数，因此：
- 稳健可行（任意功率组合下各相均不超容量）⟺ 各相负载上界不超容量；
- 任意功率组合可能产生的最坏负载差
  = max_{L}(max_p L_p - min_p L_p) = max_{p≠q}(上界_p - 下界_q)
  （注意不是「上界最大值 - 下界最小值」：同一相不可能同时取到上界与下界）。
据此归约为与 solve 同构的 DFS：容量约束作用于负载上界，目标函数为上述
有序相异相位对差值的最大值，同值时取按设备编号排序的相位序列字典序最小者。

启动顺序预演（solve_startup_plan）：沿用一份可行记录中每台设备的固定相位，
设备逐台启动——启动瞬间该相负载为「此前稳定负载 + 启动功率」，启动完成后该
设备按额定功率持续运行，该相稳定负载变为「此前稳定负载 + 额定功率」。所有
启动瞬间与稳定状态均不得超过该相容量。目标为最小化全过程各时刻
「三相最大负载 - 最小负载」的峰值，同峰值时取设备编号序列字典序最小者。

单相安全顺序判定：同相设备序列 (s_j, p_j) 需满足「此前额定功率累计 + s_j
≤ 容量」。相邻交换可证按浪涌 s_j - p_j 降序排列可行性最优，故某相位在当前
累计负载下是否还存在安全顺序，只需按浪涌降序模拟即可。整体安全顺序存在
当且仅当各相位独立存在安全顺序（不同相设备可任意交错）。

全局搜索：对 n ≤ 12 台设备做「下一启动设备」的排列 DFS（按设备编号升序
展开，严格更优才更新，保证同峰值保留字典序最小顺序），配合：
- 运行峰值剪枝：已达峰值不可能随后续启动下降；
- 终态下界：最终稳定状态必经历，其三相负载差固定；
- 启动瞬间下界：设备 i 在 q 相启动时 q 相负载至少为「当前 q 相稳定负载 +
  s_i」，而其余相负载永不超过其最终稳定负载，且该瞬间总负载不超过
  「最终总负载 + (s_i - p_i)」，据此给出两个合法下界；
- 逐相位浪涌降序模拟的可行性剪枝。

实现上不做排列枚举，而用子集动态规划：「已启动设备集合」唯一确定当前各
相稳定负载（与到达顺序无关），以位掩码为状态（至多 2^12=4096 个），
f(mask)=min_j max(g_j(mask), f(mask∪{j})) 直接求出最优峰值；再按设备
编号升序沿「恰能达到最优峰值」的转移重构字典序最小完整顺序。
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Iterable, Optional

PHASES: tuple[str, str, str] = ("A", "B", "C")


@dataclass(frozen=True)
class Device:
    """待分配设备。"""

    code: str  # 设备唯一编号
    power: int  # 正整数功率
    allowed: tuple[str, ...]  # 可接入相位集合（PHASES 的非空子集）


@dataclass(frozen=True)
class SolveResult:
    """求解结果。feasible 为 False 时其余字段为 None。"""

    feasible: bool
    assignment: Optional[dict[str, str]]  # 设备编号 -> 相位
    loads: Optional[dict[str, int]]  # 相位 -> 负载
    imbalance: Optional[int]  # 最大相负载 - 最小相负载


def solve(devices: Iterable[Device], capacities: dict[str, int]) -> SolveResult:
    """求解最优分配方案。

    :param devices: 设备集合（编号唯一、功率为正整数、允许相位非空）。
    :param capacities: 各相容量，形如 {"A": int, "B": int, "C": int}。
    :return: SolveResult；无可行分配时 feasible=False。
    """
    ordered = sorted(devices, key=lambda d: d.code)
    n = len(ordered)
    powers = [d.power for d in ordered]
    # 每个设备的可接入相位按 A/B/C 固定顺序排列，保证 DFS 按字典序枚举方案
    allowed = [tuple(p for p in PHASES if p in d.allowed) for d in ordered]
    caps = {p: capacities[p] for p in PHASES}

    # suffix_addable[i][p]：第 i..n-1 台设备中可接入相位 p 的功率和（剪枝用）
    suffix_addable = [{p: 0 for p in PHASES} for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        prev, cur = suffix_addable[i + 1], suffix_addable[i]
        for p in PHASES:
            cur[p] = prev[p] + (powers[i] if p in allowed[i] else 0)

    # suffix_total[i]：第 i..n-1 台设备的功率总和
    suffix_total = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        suffix_total[i] = suffix_total[i + 1] + powers[i]

    loads = {p: 0 for p in PHASES}
    current: list[Optional[str]] = [None] * n
    best_imbalance: Optional[int] = None
    best_assignment: Optional[list[str]] = None

    def dfs(i: int, used: int) -> None:
        nonlocal best_imbalance, best_assignment
        if best_imbalance == 0:
            return  # 已达成理论最优（差值为 0），无需继续搜索
        if i == n:
            values = loads.values()
            imbalance = max(values) - min(values)
            # 仅严格更优才更新：同差值时先枚举到的方案字典序更小
            if best_imbalance is None or imbalance < best_imbalance:
                best_imbalance = imbalance
                best_assignment = current.copy()
            return
        if best_imbalance is not None:
            # 下界剪枝：
            #   最终最大负载 >= max(当前最大负载, 总功率平均值上取整)
            #   最终最小负载 <= min(当前各相负载 + 该相剩余可接入功率)
            remaining = suffix_total[i]
            lb_max = max(max(loads.values()), ceil((used + remaining) / 3))
            ub_min = min(loads[p] + suffix_addable[i][p] for p in PHASES)
            if lb_max - ub_min >= best_imbalance:
                return
        for phase in allowed[i]:
            if loads[phase] + powers[i] <= caps[phase]:
                loads[phase] += powers[i]
                current[i] = phase
                dfs(i + 1, used + powers[i])
                loads[phase] -= powers[i]
                current[i] = None

    dfs(0, 0)

    if best_assignment is None:
        return SolveResult(feasible=False, assignment=None, loads=None, imbalance=None)

    final_loads = {p: 0 for p in PHASES}
    for idx, phase in enumerate(best_assignment):
        final_loads[phase] += powers[idx]
    assignment = {ordered[i].code: best_assignment[i] for i in range(n)}
    return SolveResult(
        feasible=True,
        assignment=assignment,
        loads=final_loads,
        imbalance=best_imbalance,
    )


@dataclass(frozen=True)
class UpgradeResult:
    """扩容求解结果。

    original_capacities：原各相容量；upgrades：各相增加的非负整数容量；
    suggested_capacities：原容量 + 扩容量；assignment：设备编号 -> 相位；
    loads：相位 -> 实际负载。
    """

    original_capacities: dict[str, int]
    upgrades: dict[str, int]
    suggested_capacities: dict[str, int]
    assignment: dict[str, str]
    loads: dict[str, int]
    total_upgrade: int  # 三相扩容总量
    max_upgrade: int  # 单相最大扩容量


def _upgrade_bounds(
    i: int,
    loads: dict[str, int],
    caps: dict[str, int],
    suffix_forced: list[dict[str, int]],
    suffix_free_total: list[int],
) -> tuple[int, int]:
    """返回当前部分分配状态下扩容总量与单相扩容的乐观下界。

    返回 (lb_total, lb_max)：
    lb_total —— 三相扩容总量的下界；
    lb_max   —— 单相扩容量的下界，取 max(被迫超载量, ceil(lb_total/3))。

    后续只允许单一相位的设备所落相位已确定（被迫负载 forced），其超载必须
    扩容承担；其余自由设备（允许集合至少两个相位）的功率总和 F 忽略相位
    限制（视为可任意切分），先填充各相被迫负载之外的空闲容量，填不满的
    部分才必须扩容。忽略自由设备的相位限制只会低估扩容，故为合法下界。
    """
    forced_deficit = 0  # 被迫负载造成的必扩容总量
    spare = 0  # 各相可接纳自由设备功率的空闲容量之和
    forced_max = 0
    for p in PHASES:
        forced = loads[p] + suffix_forced[i][p]
        if forced > caps[p]:
            deficit = forced - caps[p]
            forced_deficit += deficit
            if deficit > forced_max:
                forced_max = deficit
        else:
            spare += caps[p] - forced
    lb_total = forced_deficit + max(0, suffix_free_total[i] - spare)
    return lb_total, max(forced_max, ceil(lb_total / 3))


def solve_capacity_upgrade(
    devices: Iterable[Device], capacities: dict[str, int]
) -> UpgradeResult:
    """在保持设备功率与允许相位不变的前提下，求解最小扩容方案。

    目标依次为：三相扩容总量最小 → 单相最大扩容量最小 →
    按设备编号排序的相位序列字典序最小。扩容后总有可行方案
    （每台设备至少存在一个允许相位，各相扩至足够大即可接入）。

    :param devices: 设备集合（编号唯一、功率为正整数、允许相位非空）。
    :param capacities: 各相原容量，形如 {"A": int, "B": int, "C": int}。
    :return: UpgradeResult。
    """
    ordered = sorted(devices, key=lambda d: d.code)
    n = len(ordered)
    powers = [d.power for d in ordered]
    # 每个设备的可接入相位按 A/B/C 固定顺序排列，保证 DFS 按字典序枚举方案
    allowed = [tuple(p for p in PHASES if p in d.allowed) for d in ordered]
    caps = {p: capacities[p] for p in PHASES}

    # suffix_forced[i][p]：第 i..n-1 台设备中只允许相位 p 的设备功率和
    # suffix_free_total[i]：第 i..n-1 台设备中允许集合非单一的设备功率总和
    suffix_forced = [{p: 0 for p in PHASES} for _ in range(n + 1)]
    suffix_free_total = [0] * (n + 1)
    for idx in range(n - 1, -1, -1):
        prev_f, cur_f = suffix_forced[idx + 1], suffix_forced[idx]
        for p in PHASES:
            cur_f[p] = prev_f[p]
            if p in allowed[idx] and len(allowed[idx]) == 1:
                cur_f[p] += powers[idx]
        suffix_free_total[idx] = suffix_free_total[idx + 1] + (
            powers[idx] if len(allowed[idx]) > 1 else 0
        )

    loads = {p: 0 for p in PHASES}
    current: list[Optional[str]] = [None] * n

    # 第一遍：最小化三相扩容总量
    best_total: Optional[int] = None
    root_lb_total, _ = _upgrade_bounds(
        0, loads, caps, suffix_forced, suffix_free_total
    )
    optimal_total_found = False

    def dfs_total(i: int) -> None:
        nonlocal best_total, optimal_total_found
        if optimal_total_found:
            return  # 已命中空状态理论最优下界，且回溯由各层正常完成
        lb_total, _ = _upgrade_bounds(
            i, loads, caps, suffix_forced, suffix_free_total
        )
        # 已无法严格改善当前最优（下界不会随分配下降，剪枝安全）
        if best_total is not None and lb_total >= best_total:
            return
        if i == n:
            total = sum(max(0, loads[p] - caps[p]) for p in PHASES)
            if best_total is None or total < best_total:
                best_total = total
                if total == root_lb_total:
                    optimal_total_found = True
            return
        for phase in allowed[i]:
            loads[phase] += powers[i]
            current[i] = phase
            dfs_total(i + 1)
            loads[phase] -= powers[i]
            current[i] = None
            if optimal_total_found:
                break

    dfs_total(0)

    assert best_total is not None  # 扩容后必有解
    target_total = best_total

    # 第二遍：在扩容总量固定最小的方案中，以 (单相最大扩容量, 相位序列) 为
    # 键取字典序最小，即先最小化单相最大扩容、再最小化相位序列。
    best_max: Optional[int] = None
    best_assignment: Optional[list[str]] = None
    best_key: Optional[tuple[int, tuple[str, ...]]] = None

    def dfs_max(i: int) -> None:
        nonlocal best_max, best_assignment, best_key
        lb_total, lb_max = _upgrade_bounds(
            i, loads, caps, suffix_forced, suffix_free_total
        )
        if lb_total > target_total:
            return
        # 仅当子树不可能得到「严格更小」的单相最大扩容时剪枝；
        # lb_max == best_max 时子树仍可能含有同值但字典序更小的方案，不可剪枝
        if best_max is not None and lb_max > best_max:
            return
        if i == n:
            extras = {p: max(0, loads[p] - caps[p]) for p in PHASES}
            if sum(extras.values()) != target_total:
                return
            current_max = max(extras.values())
            # 以 (单相最大扩容, 相位序列) 为键严格更优才更新，
            # 同 max 时保留字典序更小的完整方案
            candidate_key = (current_max, tuple(current))
            if best_key is None or candidate_key < best_key:
                best_key = candidate_key
                best_max = current_max
                best_assignment = current.copy()
            return
        for phase in allowed[i]:
            loads[phase] += powers[i]
            current[i] = phase
            dfs_max(i + 1)
            loads[phase] -= powers[i]
            current[i] = None

    dfs_max(0)

    assert best_assignment is not None
    final_loads = {p: 0 for p in PHASES}
    for idx, phase in enumerate(best_assignment):
        final_loads[phase] += powers[idx]
    upgrades = {p: max(0, final_loads[p] - caps[p]) for p in PHASES}
    suggested = {p: caps[p] + upgrades[p] for p in PHASES}
    assignment = {ordered[i].code: best_assignment[i] for i in range(n)}
    return UpgradeResult(
        original_capacities=dict(caps),
        upgrades=upgrades,
        suggested_capacities=suggested,
        assignment=assignment,
        loads=final_loads,
        total_upgrade=sum(upgrades.values()),
        max_upgrade=max(upgrades.values()),
    )


@dataclass(frozen=True)
class RobustDevice:
    """稳健预演设备：功率为正整数区间 [min_power, max_power]。"""

    code: str  # 设备唯一编号
    min_power: int  # 功率下界（正整数）
    max_power: int  # 功率上界（正整数，不小于下界）
    allowed: tuple[str, ...]  # 可接入相位集合（PHASES 的非空子集）


@dataclass(frozen=True)
class RobustResult:
    """稳健预演求解结果。feasible 为 False 时其余字段为 None。

    load_lower / load_upper：各相负载下界 / 上界（该相设备分别取功率
    下界 / 上界之和）；worst_imbalance：任意功率组合下「三相负载最大值
    与最小值之差」可能达到的最大值。
    """

    feasible: bool
    assignment: Optional[dict[str, str]]  # 设备编号 -> 相位
    load_lower: Optional[dict[str, int]]  # 相位 -> 负载下界
    load_upper: Optional[dict[str, int]]  # 相位 -> 负载上界
    worst_imbalance: Optional[int]  # 最坏负载差


def solve_robust(
    devices: Iterable[RobustDevice], capacities: dict[str, int]
) -> RobustResult:
    """求解稳健分相预演：任意功率组合下各相均不超容量，且最坏负载差最小。

    对固定分配，各相负载独立取遍 [下界, 上界] 内的所有整数，故：
    - 稳健可行 ⟺ 各相负载上界（max_power 之和）不超过对应容量；
    - 最坏负载差 = max_{p≠q}(上界_p - 下界_q)
      （max_L(max_p L_p - min_p L_p) = max_{p≠q} max_L(L_p - L_q)，
      同一相不可能同时取到上界与下界，故只取相异相位对）。

    目标：在稳健可行方案中最小化最坏负载差；同值时取按设备编号排序的
    相位序列字典序最小者。搜索结构与 solve 相同：按设备编号排序 DFS、
    相位按 A/B/C 展开、仅严格更优才更新，配合容量约束与下界剪枝。

    :param devices: 设备集合（编号唯一、0 < min_power <= max_power、允许相位非空）。
    :param capacities: 各相容量，形如 {"A": int, "B": int, "C": int}。
    :return: RobustResult；无稳健可行分配时 feasible=False。
    """
    ordered = sorted(devices, key=lambda d: d.code)
    n = len(ordered)
    lows = [d.min_power for d in ordered]
    highs = [d.max_power for d in ordered]
    # 每个设备的可接入相位按 A/B/C 固定顺序排列，保证 DFS 按字典序枚举方案
    allowed = [tuple(p for p in PHASES if p in d.allowed) for d in ordered]
    caps = {p: capacities[p] for p in PHASES}

    # suffix_addable_lo[i][p]：第 i..n-1 台设备中可接入相位 p 的功率下界和（剪枝用）
    suffix_addable_lo = [{p: 0 for p in PHASES} for _ in range(n + 1)]
    # suffix_forced_hi[i][p]：第 i..n-1 台设备中只允许相位 p 的设备功率上界和（剪枝用）
    suffix_forced_hi = [{p: 0 for p in PHASES} for _ in range(n + 1)]
    # suffix_forced_lo[i][p]：同上，功率下界和（剪枝用）
    suffix_forced_lo = [{p: 0 for p in PHASES} for _ in range(n + 1)]
    # suffix_lo[i] / suffix_hi[i]：第 i..n-1 台设备的功率下界 / 上界总和
    suffix_lo = [0] * (n + 1)
    suffix_hi = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        prev_lo, cur_lo = suffix_addable_lo[i + 1], suffix_addable_lo[i]
        prev_fh, cur_fh = suffix_forced_hi[i + 1], suffix_forced_hi[i]
        prev_fl, cur_fl = suffix_forced_lo[i + 1], suffix_forced_lo[i]
        for p in PHASES:
            cur_lo[p] = prev_lo[p] + (lows[i] if p in allowed[i] else 0)
            forced = highs[i] if allowed[i] == (p,) else 0
            cur_fh[p] = prev_fh[p] + forced
            cur_fl[p] = prev_fl[p] + (lows[i] if allowed[i] == (p,) else 0)
        suffix_lo[i] = suffix_lo[i + 1] + lows[i]
        suffix_hi[i] = suffix_hi[i + 1] + highs[i]

    lower = {p: 0 for p in PHASES}
    upper = {p: 0 for p in PHASES}
    current: list[Optional[str]] = [None] * n
    best_imbalance: Optional[int] = None
    best_assignment: Optional[list[str]] = None

    def dfs(i: int, used_lo: int, used_hi: int) -> None:
        nonlocal best_imbalance, best_assignment
        if best_imbalance == 0:
            return  # 已达成理论最优（最坏负载差为 0），无需继续搜索
        if i == n:
            # 最坏负载差 = max_{p≠q}(上界_p - 下界_q)
            imbalance = max(
                upper[p] - lower[q] for p in PHASES for q in PHASES if p != q
            )
            # 仅严格更优才更新：同差值时先枚举到的方案字典序更小
            if best_imbalance is None or imbalance < best_imbalance:
                best_imbalance = imbalance
                best_assignment = current.copy()
            return
        if best_imbalance is not None:
            # 下界剪枝一（相位对）：对任意有序相异相位对 (p, q)，
            #   最终上界_p >= 当前上界_p + 剩余被迫接入 p 的上界和
            #   最终下界_q <= 当前下界_q + 剩余可接入 q 的下界和
            lb = max(
                upper[p] + suffix_forced_hi[i][p] - lower[q] - suffix_addable_lo[i][q]
                for p in PHASES
                for q in PHASES
                if p != q
            )
            # 下界剪枝二（总量平均）：设 p* 为最终上界最大的相位，则
            #   最终上界最大值 >= ceil(上界总和 / 3)
            #   其余两相最终下界的最小值 <= (下界总和 - p* 相下界) / 2
            #     <= (下界总和 - 各相被迫下界的最小值) / 2
            total_lo = used_lo + suffix_lo[i]
            total_hi = used_hi + suffix_hi[i]
            min_forced_lo = min(
                lower[p] + suffix_forced_lo[i][p] for p in PHASES
            )
            lb = max(
                lb,
                ceil(total_hi / 3) - (total_lo - min_forced_lo) // 2,
            )
            if lb >= best_imbalance:
                return
        for phase in allowed[i]:
            # 稳健可行性：任意功率组合下不超容量 ⟺ 负载上界不超容量
            if upper[phase] + highs[i] <= caps[phase]:
                lower[phase] += lows[i]
                upper[phase] += highs[i]
                current[i] = phase
                dfs(i + 1, used_lo + lows[i], used_hi + highs[i])
                lower[phase] -= lows[i]
                upper[phase] -= highs[i]
                current[i] = None

    dfs(0, 0, 0)

    if best_assignment is None:
        return RobustResult(
            feasible=False,
            assignment=None,
            load_lower=None,
            load_upper=None,
            worst_imbalance=None,
        )

    final_lower = {p: 0 for p in PHASES}
    final_upper = {p: 0 for p in PHASES}
    for idx, phase in enumerate(best_assignment):
        final_lower[phase] += lows[idx]
        final_upper[phase] += highs[idx]
    assignment = {ordered[i].code: best_assignment[i] for i in range(n)}
    return RobustResult(
        feasible=True,
        assignment=assignment,
        load_lower=final_lower,
        load_upper=final_upper,
        worst_imbalance=best_imbalance,
    )


@dataclass(frozen=True)
class StartupDevice:
    """启动预演设备：固定相位、额定功率与启动瞬时功率（不小于额定功率）。"""

    code: str  # 设备唯一编号
    phase: str  # 沿用记录的固定相位
    power: int  # 额定功率（正整数，启动完成后持续运行功率）
    startup_power: int  # 启动瞬时功率（不小于额定功率）


@dataclass(frozen=True)
class StartupStep:
    """一次启动的负载快照：启动瞬间与启动完成后的稳定状态。"""

    step: int  # 第几步（从 1 开始）
    device_id: str
    startup_loads: dict[str, int]  # 启动瞬间各相负载（该设备按启动功率计）
    stable_loads: dict[str, int]  # 启动完成后各相稳定负载（该设备按额定功率计）


@dataclass(frozen=True)
class StartupPlanResult:
    """启动顺序预演结果。feasible 为 False 时其余字段为 None。

    order：最优启动顺序（设备编号序列）；steps：逐步负载快照；
    peak：全过程各启动瞬间与稳定状态中「三相最大负载 - 最小负载」的峰值。
    """

    feasible: bool
    order: Optional[list[str]]
    steps: Optional[list[StartupStep]]
    peak: Optional[int]


def solve_startup_plan(
    devices: Iterable[StartupDevice], capacities: dict[str, int]
) -> StartupPlanResult:
    """求解使全过程三相负载差峰值最小的设备启动顺序。

    设备逐台启动：启动瞬间该设备按启动功率计，完成后按额定功率持续运行；
    每个启动瞬间与每个稳定状态各相均不得超过容量。目标为最小化全过程
    「三相最大负载 - 最小负载」的峰值；同峰值时取设备编号序列字典序最小者。

    求解方式（子集动态规划，n ≤ 12，至多 4096 个状态）：

    - 「已启动设备集合」唯一确定当前各相稳定负载（额定功率之和），与到达
      顺序无关，故以已启动集合的位掩码为状态。
    - 设 f(mask) 为从「mask 中设备已启动」的稳定状态出发，启动其余设备的
      全过程还将达到的最小峰值；mask 为全集时 f=0。
    - 对每台下一台可启动设备 j（其启动瞬间不超容量）：
      f(mask) = min_j max(g_j(mask), f(mask∪{j}))，其中 g_j(mask) 为 j
      启动瞬间与启动后稳定状态「最大相负载 - 最小相负载」的较大者。
    - DP 只确定最优峰值；同峰值下设备编号序列字典序最小的完整顺序由一次
      确定性重构得到：从空集合起按设备编号升序选择第一台使
      「目前峰值、本步代价与最优后缀峰值三者之恰等于 dp[0]」的设备——
      该条件保证所选后缀必能完成，逐状态选出的序列即字典序最小最优顺序。
    - f(0) 无解（不存在任何安全顺序）时 feasible=False。

    :param devices: 设备集合（编号唯一、额定功率为正整数、
        启动功率不小于额定功率、相位合法）。
    :param capacities: 各相容量，形如 {"A": int, "B": int, "C": int}。
    :return: StartupPlanResult；不存在安全启动顺序时 feasible=False。
    """
    ordered = sorted(devices, key=lambda d: d.code)
    n = len(ordered)
    phases = [PHASES.index(d.phase) for d in ordered]
    powers = [d.power for d in ordered]
    startups = [d.startup_power for d in ordered]
    caps = [capacities[p] for p in PHASES]

    size = 1 << n
    full = size - 1

    # rated_sum[p][mask]：mask 中分配到 p 相的已启动设备额定功率累计
    rated_sum = [[0] * size for _ in range(3)]
    for mask in range(1, size):
        low = mask & -mask
        idx = low.bit_length() - 1
        prev = mask ^ low
        k = phases[idx]
        for p in range(3):
            rated_sum[p][mask] = rated_sum[p][prev]
        rated_sum[k][mask] += powers[idx]

    # dp[mask]：从 mask 稳定状态出发后续过程的最小峰值；None 表示无安全顺序。
    dp: list[Optional[int]] = [None] * size
    dp[full] = 0
    # 从全满掩码向前递推：dp[mask] 只依赖 popcount 更大的掩码
    for mask in range(full - 1, -1, -1):
        best_value: Optional[int] = None
        current = (
            rated_sum[0][mask],
            rated_sum[1][mask],
            rated_sum[2][mask],
        )
        for idx in range(n):
            bit = 1 << idx
            if mask & bit:
                continue
            k = phases[idx]
            instant_load = current[k] + startups[idx]
            # 容量约束：启动瞬间不超容量（额定功率不大于启动功率，故启动
            # 瞬间安全即可同时保证启动后的稳定状态不超容量）
            if instant_load > caps[k]:
                continue
            future = dp[mask | bit]
            if future is None:
                continue  # 该设备启动后不存在安全的后续顺序
            instant = list(current)
            instant[k] = instant_load
            stable = list(current)
            stable[k] = current[k] + powers[idx]
            cost = max(
                max(instant) - min(instant),
                max(stable) - min(stable),
            )
            value = max(cost, future)
            if best_value is None or value < best_value:
                best_value = value
        dp[mask] = best_value

    if dp[0] is None:
        return StartupPlanResult(feasible=False, order=None, steps=None, peak=None)

    # 最优峰值 dp[0] 已确定。字典序最小顺序不能由各状态局部贪心得到
    # （不同首台设备对应不同后缀状态），故再做一次确定性 DFS：设备按编号
    # 升序尝试，仅当「目前峰值」「最优后缀峰值」与本步代价三者之恰等于
    # 全局最优 target 时进入分支——该条件保证分支必能完成（最优后缀可达），
    # 故每一步都不会走死，第一条完整路径即字典序最小最优顺序。
    target = dp[0]
    selected: list[int] = []
    mask = 0
    peak_so_far = 0
    while mask != full:
        current = (
            rated_sum[0][mask],
            rated_sum[1][mask],
            rated_sum[2][mask],
        )
        for idx in range(n):
            bit = 1 << idx
            if mask & bit:
                continue
            k = phases[idx]
            instant_load = current[k] + startups[idx]
            if instant_load > caps[k]:
                continue
            future = dp[mask | bit]
            if future is None:
                continue
            instant = list(current)
            instant[k] = instant_load
            stable = list(current)
            stable[k] = current[k] + powers[idx]
            cost = max(
                max(instant) - min(instant),
                max(stable) - min(stable),
            )
            if max(peak_so_far, cost, future) == target:
                selected.append(idx)
                mask |= bit
                peak_so_far = max(peak_so_far, cost)
                break
        else:  # pragma: no cover - 正确性保证至少有一个最优转移
            raise RuntimeError("启动顺序重构失败")

    steps: list[StartupStep] = []
    run_loads = {p: 0 for p in PHASES}
    running_peak = 0
    for step_no, idx in enumerate(selected, start=1):
        phase = PHASES[phases[idx]]
        startup_loads = dict(run_loads)
        startup_loads[phase] += startups[idx]
        run_loads[phase] += powers[idx]
        stable_loads = dict(run_loads)
        running_peak = max(
            running_peak,
            max(startup_loads.values()) - min(startup_loads.values()),
            max(stable_loads.values()) - min(stable_loads.values()),
        )
        steps.append(
            StartupStep(
                step=step_no,
                device_id=ordered[idx].code,
                startup_loads=startup_loads,
                stable_loads=stable_loads,
            )
        )
    assert running_peak == dp[0]
    return StartupPlanResult(
        feasible=True,
        order=[ordered[idx].code for idx in selected],
        steps=steps,
        peak=dp[0],
    )
