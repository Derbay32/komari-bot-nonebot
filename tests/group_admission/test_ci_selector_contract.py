"""TSK-233：``.github/workflows/pr-acceptance.yml`` CI selector 契约验收。

设计契约（TSK-233 总调度裁定）：

- ``static-and-unit`` job 的 pytest 命令必须带 ``-m`` selector，语义为排除
  ``group_admission_service``（无服务环境下绝不执行真实服务用例），且该 job
  不得携带服务 env（``KOMARI_TEST_POSTGRES_URL`` / ``KOMARI_TEST_REDIS_URL`` /
  ``SQLALCHEMY_DATABASE_URL``）；
- ``service-and-migration`` job 的 pytest 命令必须显式带 ``-m`` selector，
  语义为全量（如 ``group_admission_service or not group_admission_service``，
  措辞可等价但必须显式出现 selector 参数），且在真实服务环境下执行；
- 两个 job 保持既有 required 检查名（``name:`` 字段）不变。

selector 语义用 pytest 自带的 ``-m`` 表达式解析器真实求值，不依赖措辞。
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest
import yaml
from _pytest.mark.expression import Expression

pytestmark = pytest.mark.group_admission_acceptance

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "pr-acceptance.yml"

STATIC_JOB_ID = "static-and-unit"
SERVICE_JOB_ID = "service-and-migration"
STATIC_JOB_NAME = "静态与无服务验收"
SERVICE_JOB_NAME = "真实服务与迁移验收"
SERVICE_ENV_VARS = (
    "KOMARI_TEST_POSTGRES_URL",
    "KOMARI_TEST_REDIS_URL",
    "SQLALCHEMY_DATABASE_URL",
)


def _workflow() -> dict[str, object]:
    return yaml.safe_load(WORKFLOW_PATH.read_text("utf-8"))


def _job(job_id: str) -> dict[str, object]:
    jobs = _workflow()["jobs"]
    assert isinstance(jobs, dict)
    job = jobs.get(job_id)
    assert isinstance(job, dict), f"workflow 缺少 job: {job_id}"
    return job


def _pytest_step(job: dict[str, object]) -> dict[str, object]:
    steps = job.get("steps")
    assert isinstance(steps, list), "job 缺少 steps"
    for step in steps:
        assert isinstance(step, dict)
        run = step.get("run")
        if isinstance(run, str) and "pytest" in run:
            return step
    pytest.fail("job 缺少 pytest 步骤")


def _marker_expression(step: dict[str, object]) -> str:
    run = step.get("run")
    assert isinstance(run, str), "pytest 步骤缺少 run 命令"
    tokens = shlex.split(run)
    assert "-m" in tokens, f"pytest 命令缺少 -m selector: {run}"
    index = tokens.index("-m")
    assert index + 1 < len(tokens), f"-m selector 缺少参数: {run}"
    expr = tokens[index + 1]
    assert expr.strip(), f"-m selector 为空: {run}"
    return expr


def _matches(expr: str, marker_names: set[str]) -> bool:
    def matcher(name: str, /, **kwargs: str | int | bool | None) -> bool:
        del kwargs
        return name in marker_names

    return bool(Expression.compile(expr).evaluate(matcher))


def test_required_check_names_are_unchanged() -> None:
    assert _job(STATIC_JOB_ID)["name"] == STATIC_JOB_NAME
    assert _job(SERVICE_JOB_ID)["name"] == SERVICE_JOB_NAME


def test_static_job_pytest_excludes_service_marker() -> None:
    job = _job(STATIC_JOB_ID)
    expr = _marker_expression(_pytest_step(job))
    assert not _matches(expr, {"group_admission_service"}), (
        f"static job selector {expr!r} 未排除 group_admission_service"
    )
    assert _matches(expr, {"group_admission_acceptance"}), (
        f"static job selector {expr!r} 排除了无服务验收测试"
    )
    assert _matches(expr, set()), (
        f"static job selector {expr!r} 排除了未标记测试（非全量无服务）"
    )


def test_static_job_has_no_service_env() -> None:
    env = _job(STATIC_JOB_ID).get("env")
    if env is None:
        return
    assert isinstance(env, dict)
    leaks = [var for var in SERVICE_ENV_VARS if var in env]
    assert not leaks, f"static job 不得携带服务 env: {leaks}"


def test_service_job_pytest_selects_everything_explicitly() -> None:
    job = _job(SERVICE_JOB_ID)
    expr = _marker_expression(_pytest_step(job))
    assert _matches(expr, {"group_admission_service"}), (
        f"service job selector {expr!r} 未选中 service 节点"
    )
    assert _matches(expr, {"group_admission_acceptance"}), (
        f"service job selector {expr!r} 未选中 acceptance 节点"
    )
    assert _matches(expr, set()), f"service job selector {expr!r} 未选中无标记节点"


def test_service_job_runs_in_service_environment() -> None:
    env = _job(SERVICE_JOB_ID).get("env")
    assert isinstance(env, dict), "service job 缺少 env"
    for var in ("KOMARI_TEST_POSTGRES_URL", "KOMARI_TEST_REDIS_URL"):
        value = env.get(var)
        assert isinstance(value, str) and value.strip(), (
            f"service job env 缺少 {var}"
        )
