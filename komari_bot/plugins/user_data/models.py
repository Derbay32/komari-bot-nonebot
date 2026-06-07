from pydantic import BaseModel


class UserAttribute(BaseModel):
    """用户属性模型。"""

    user_id: str
    attribute_name: str
    attribute_value: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class FavorabilityStage(BaseModel):
    """当前好感度阶段。"""

    index: int
    min_score: int
    max_score: int
    name: str
    prompt: str


FAVORABILITY_STAGES: tuple[FavorabilityStage, ...] = (
    FavorabilityStage(
        index=1,
        min_score=0,
        max_score=99,
        name="疏离戒备",
        prompt="当前关系偏疏离和戒备，回复应克制、保持距离，不主动表现亲昵。",
    ),
    FavorabilityStage(
        index=2,
        min_score=100,
        max_score=199,
        name="普通熟人",
        prompt="当前关系是普通熟人，正常交流即可，不额外亲昵，也不刻意疏远。",
    ),
    FavorabilityStage(
        index=3,
        min_score=200,
        max_score=299,
        name="亲近朋友",
        prompt="当前关系较亲近，更愿意接话、吐槽和关心对方，语气可以更自然。",
    ),
    FavorabilityStage(
        index=4,
        min_score=300,
        max_score=400,
        name="高度信任",
        prompt="当前关系高度信任，语气可以更柔软坦诚，但仍需保持小鞠知花的人设边界。",
    ),
)


def get_favorability_stage(score: int) -> FavorabilityStage:
    """根据当前好感度返回统一阶段定义。"""
    normalized_score = max(0, min(400, score))
    for stage in FAVORABILITY_STAGES:
        if stage.min_score <= normalized_score <= stage.max_score:
            return stage
    return FAVORABILITY_STAGES[-1]


class UserFavorability(BaseModel):
    """用户当前好感度模型。"""

    user_id: str
    favorability: int
    stage_index: int
    stage_name: str
    stage_prompt: str
    updated_at: str

    @classmethod
    def from_score(
        cls,
        *,
        user_id: str,
        favorability: int,
        updated_at: str,
    ) -> "UserFavorability":
        stage = get_favorability_stage(favorability)
        return cls(
            user_id=user_id,
            favorability=favorability,
            stage_index=stage.index,
            stage_name=stage.name,
            stage_prompt=stage.prompt,
            updated_at=updated_at,
        )


class FavorabilityAdjustmentResult(BaseModel):
    """好感度调整结果。"""

    user_id: str
    before: int
    delta: int
    after: int
    stage_index: int
    stage_name: str
    updated_at: str

    @classmethod
    def from_values(
        cls,
        *,
        user_id: str,
        before: int,
        delta: int,
        after: int,
        updated_at: str,
    ) -> "FavorabilityAdjustmentResult":
        stage = get_favorability_stage(after)
        return cls(
            user_id=user_id,
            before=before,
            delta=delta,
            after=after,
            stage_index=stage.index,
            stage_name=stage.name,
            updated_at=updated_at,
        )
