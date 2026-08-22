from __future__ import annotations

from dataclasses import dataclass, field

from .models import AnomalyFlag, PayrollResult


@dataclass
class AnomalyRouter:
    queue: list[AnomalyFlag] = field(default_factory=list)

    def route_anomaly(self, flag: AnomalyFlag) -> AnomalyFlag:
        self.queue.append(flag)
        return flag

    def route_all(self, result: PayrollResult) -> list[AnomalyFlag]:
        return [self.route_anomaly(flag) for flag in result.anomaly_flags if flag.requires_review]


def can_publish(result: PayrollResult, resolved_codes: set[str] | None = None) -> bool:
    resolved = resolved_codes or set()
    return not any(flag.requires_review and flag.code not in resolved for flag in result.anomaly_flags)
