"""komari_custom 群归属生命周期准入消费接缝。

ADR-0012 要求：群归属数据与持久群工作在受限期间休眠，不参与业务读取、写
入、投票加工或通知投递；只作用效果层。低层 Repository / Redis Adapter
策略无感、不猜群；归属与裁决一律在此业务编排层完成。

本模块只承载「业务效果前是否获准」的统一入口，不解释策略、不持有快照，也
不缓存裁决结果当作永久通行证。受限时调用方应跳过效果并保存安全进度。
"""

from __future__ import annotations

from komari_bot.plugins.group_admission import (
    AdmissionIntent,
    AdmissionQualification,
    adjudicate,
)


def business_admitted(group_id: int) -> bool:
    """目标群开展业务资格：全部关联群获准才为 True。

    缺席或非法群归属按隔离的群未获准处理（故障关闭），不猜群、不伪装系统行
    为。只为紧随其后的单个不可分业务效果授权一次，不跨效果复用。
    """
    result = adjudicate((group_id,), intent=AdmissionIntent.BUSINESS)
    return result.qualification is AdmissionQualification.BUSINESS


def finalization_granted(group_id: int) -> bool:
    """既成事实收尾资格：受限后仍允许冻结前的承诺完成对账。

    不扩张解释为可开展普通业务；只有获得既成事实收尾资格才返回 True。
    """
    result = adjudicate((group_id,), intent=AdmissionIntent.FACT_FINALIZATION)
    return result.qualification is AdmissionQualification.FACT_FINALIZATION


__all__ = ["business_admitted", "finalization_granted"]
