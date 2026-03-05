"""UnifiedCandidateRerankService 单元测试。"""

from komari_bot.plugins.komari_memory.services.unified_candidate_rerank import (
    UnifiedCandidateRerankService,
)


def test_detect_alias_case_insensitive_hit() -> None:
    message = "我想问一下KoMaRi怎么看这个设定"
    aliases = ["小鞠", "komari"]
    assert UnifiedCandidateRerankService.detect_alias(message, aliases) is True


def test_detect_alias_miss() -> None:
    message = "今天吃什么好呢"
    aliases = ["小鞠", "komari"]
    assert UnifiedCandidateRerankService.detect_alias(message, aliases) is False


def test_cosine_similarity_basic() -> None:
    assert (
        UnifiedCandidateRerankService._cosine_similarity([1.0, 0.0], [1.0, 0.0])
        == 1.0
    )
    assert (
        UnifiedCandidateRerankService._cosine_similarity([1.0, 0.0], [0.0, 1.0])
        == 0.0
    )


def test_cosine_similarity_invalid_vectors() -> None:
    assert UnifiedCandidateRerankService._cosine_similarity([], []) == 0.0
    assert UnifiedCandidateRerankService._cosine_similarity([1.0], [1.0, 2.0]) == 0.0
