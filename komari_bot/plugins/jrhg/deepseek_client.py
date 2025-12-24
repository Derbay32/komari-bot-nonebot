import asyncio
import json
import time
from typing import Optional, Union

import aiohttp
from nonebot import logger

from .config import Config
from .config_schemas import DynamicConfigSchema

# 配置兼容性
ConfigType = Union[Config, DynamicConfigSchema]


class DeepSeekClient:
    """DeepSeek API客户端"""

    def __init__(self, config: ConfigType):
        self.config = config
        self.session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """获取或创建HTTP会话"""
        if self.session is None or self.session.closed:
            headers = {
                "Authorization": f"Bearer {self.config.deepseek_api_token}",
                "Content-Type": "application/json"
            }
            timeout = aiohttp.ClientTimeout(total=30.0)
            self.session = aiohttp.ClientSession(headers=headers, timeout=timeout)
        return self.session

    async def close(self):
        """关闭HTTP会话"""
        if self.session and not self.session.closed:
            await self.session.close()

    def _build_favor_prompt(self, daily_favor: int, user_nickname: str) -> str:
        """根据好感度构建系统提示词"""
        base_prompt = self.config.deepseek_default_prompt

        # 根据好感度添加具体的态度指导
        if daily_favor <= 20:
            attitude_guide = f"你对{user_nickname}的好感度很低({daily_favor}/100)，请用非常冷淡、疏远的语气回应。"
        elif daily_favor <= 40:
            attitude_guide = f"你对{user_nickname}的好感度较低({daily_favor}/100)，请用冷淡、有距离感的语气回应。"
        elif daily_favor <= 60:
            attitude_guide = f"你对{user_nickname}的好感度一般({daily_favor}/100)，请用中性、礼貌的语气回应。"
        elif daily_favor <= 80:
            attitude_guide = f"你对{user_nickname}的好感度较高({daily_favor}/100)，请用友好、热情的语气回应。"
        else:
            attitude_guide = f"你对{user_nickname}的好感度非常高({daily_favor}/100)，请用非常热情、亲密的语气回应。"

        return f"{base_prompt}\n\n{attitude_guide}\n\n请直接生成打招呼的内容，不要提及好感度数值。"

    async def generate_greeting(
        self,
        user_nickname: str,
        daily_favor: int,
        custom_message: Optional[str] = None
    ) -> str:
        """生成问候语

        Args:
            user_nickname: 用户昵称
            daily_favor: 每日好感度 (1-100)
            custom_message: 自定义消息，如果提供则会在问候中包含此内容

        Returns:
            生成的问候语
        """
        try:
            session = await self._get_session()

            # 构建系统提示词
            system_prompt = self._build_favor_prompt(daily_favor, user_nickname)

            #
            now_time = time.strftime("%A %Y-%m-%d %H-%M", time.localtime())
            # 构建用户消息
            if custom_message:
                user_message = f"现在的时间是{now_time}。用户{user_nickname}对你说：{custom_message}，请回应他。"
            else:
                user_message = f"现在的时间是{now_time}。请向用户{user_nickname}打个招呼。"

            # 构建请求数据
            request_data = {
                "model": self.config.deepseek_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message}
                ],
                "temperature": self.config.deepseek_temperature,
                "frequency_penalty": self.config.deepseek_frequency_penalty,
                "max_tokens": 200,
                "stream": False
            }

            # 发送API请求
            async with session.post(
                self.config.deepseek_api_url,
                json=request_data
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"DeepSeek API请求失败: {response.status} - {error_text}")
                    return self._get_fallback_response(daily_favor, user_nickname)

                response_data = await response.json()

                # 解析响应
                if "choices" in response_data and len(response_data["choices"]) > 0:
                    content = response_data["choices"][0]["message"]["content"]
                    return content.strip()
                else:
                    logger.error(f"DeepSeek API响应格式异常: {response_data}")
                    return self._get_fallback_response(daily_favor, user_nickname)

        except asyncio.TimeoutError:
            logger.error("DeepSeek API请求超时")
            return self._get_fallback_response(daily_favor, user_nickname)
        except aiohttp.ClientError as e:
            logger.error(f"DeepSeek API网络错误: {e}")
            return self._get_fallback_response(daily_favor, user_nickname)
        except json.JSONDecodeError as e:
            logger.error(f"DeepSeek API响应解析错误: {e}")
            return self._get_fallback_response(daily_favor, user_nickname)
        except Exception as e:
            logger.error(f"DeepSeek API未知错误: {e}")
            return self._get_fallback_response(daily_favor, user_nickname)

    def _get_fallback_response(self, daily_favor: int, user_nickname: str) -> str:
        """获取备用回复（当API调用失败时使用）"""
        if daily_favor <= 20:
            return f"咦！？去、去死！"
        elif daily_favor <= 40:
            return f"唔诶，{user_nickname}！？怎、怎么是你…!?（后退）。"
        elif daily_favor <= 60:
            return f"不、不过是区区{user_nickname}，可、可别得意忘形了。"
        elif daily_favor <= 80:
            return f"{user_nickname}，你、你来啦，今天要不要，一、一起看书……？"
        else:
            return f"只、只是有一点点在意你哦……唔，{user_nickname}，你就是这点不、不行啦！"

    async def test_connection(self) -> bool:
        """测试API连接

        Returns:
            连接是否成功
        """
        try:
            session = await self._get_session()

            # 发送简单的测试请求
            request_data = {
                "model": self.config.deepseek_model,
                "messages": [
                    {"role": "user", "content": "你好"}
                ],
                "temperature": 0.1,
                "max_tokens": 10
            }

            async with session.post(
                self.config.deepseek_api_url,
                json=request_data
            ) as response:
                return response.status == 200

        except Exception as e:
            logger.error(f"DeepSeek API连接测试失败: {e}")
            return False


# 全局客户端实例
_client: Optional[DeepSeekClient] = None


def get_client(config: ConfigType) -> DeepSeekClient:
    """获取DeepSeek客户端实例"""
    global _client
    if _client is None:
        _client = DeepSeekClient(config)
    return _client


async def close_client():
    """关闭DeepSeek客户端"""
    global _client
    if _client:
        await _client.close()
        _client = None
