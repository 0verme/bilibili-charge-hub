from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from app.models import JobKind
from app.routers.jobs import JobInput, ScheduleUpdate


@pytest.mark.parametrize("model", [JobInput, ScheduleUpdate])
def test_interval_schedule_accepts_twenty_seconds(model: type[BaseModel]) -> None:
    payload = {"interval_seconds": 20}
    if model is JobInput:
        payload.update({"kind": JobKind.NOTIFICATION_RETRY})

    assert model.model_validate(payload).interval_seconds == 20


@pytest.mark.parametrize("model", [JobInput, ScheduleUpdate])
def test_interval_schedule_rejects_less_than_twenty_seconds(model: type[BaseModel]) -> None:
    payload = {"interval_seconds": 19}
    if model is JobInput:
        payload.update({"kind": JobKind.NOTIFICATION_RETRY})

    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_dashboard_displays_twenty_second_minimum_interval() -> None:
    script = Path("app/static/dashboard.js").read_text(encoding="utf-8")

    assert "输入运行间隔（秒，至少 20）" in script
