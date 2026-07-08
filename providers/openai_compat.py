import aiohttp
from typing import Optional

from astrbot.api import logger

from .base import BaseImageProvider


class OpenAICompatProvider(BaseImageProvider):
    """通用 OpenAI 兼容格式的图片服务提供商。
    
    支持任何兼容 OpenAI API 格式的服务：
    - 硅基流动 (https://api.siliconflow.cn/v1)
    - OneAPI 代理
    - 本地推理服务 (如 vLLM)
    - OpenAI 官方 API 等
    """

    def __init__(self, api_base: str, api_key: str,
                 default_gen_model: str, default_vision_model: str,
                 default_size: str, save_images: bool = False):
        """
        Args:
            api_base: API 基础地址，如 "https://api.siliconflow.cn/v1"。
            api_key: API Key。
            default_gen_model: 默认文生图模型名。
            default_vision_model: 默认识图模型名。
            default_size: 默认图片尺寸，如 "1024x1024"。
            save_images: 是否在本地保存生成图片。
        """
        self.api_base = api_base.rstrip('/')
        self.api_key = api_key
        self.default_gen_model = default_gen_model
        self.default_vision_model = default_vision_model
        self.default_size = default_size
        self.save_images = save_images

        self.session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=aiohttp.ClientTimeout(total=120)
        )

    @property
    def name(self) -> str:
        return self.api_base

    async def text_to_image(self, prompt: str, model: Optional[str] = None,
                            size: Optional[str] = None) -> str:
        """文生图。
        
        API: POST {api_base}/images/generations
        
        Request (OpenAI 格式):
        {
            "model": "...",
            "prompt": "...",
            "n": 1,
            "size": "1024x1024"
        }
        
        Response:
        {
            "data": [{ "url": "https://..." }]
        }
        """
        url = f"{self.api_base}/images/generations"
        model_name = model or self.default_gen_model
        if not model_name:
            raise Exception("未配置文生图模型，请在 WebUI 中设置 gen_model 后重试。")

        payload = {
            "model": model_name,
            "prompt": prompt,
            "n": 1,
            "size": size or self.default_size
        }

        logger.debug(f"AiDraw text_to_image 请求: model={payload['model']}, "
                     f"size={payload['size']}, prompt_len={len(prompt)}")

        async with self.session.post(url, json=payload) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                raise Exception(f"文生图 API 错误 ({resp.status}): {error_text}")
            result = await resp.json()
            logger.debug(f"AiDraw text_to_image 响应成功")

            # OpenAI 格式: result.data[0].url
            data = result.get("data", [])
            if not data:
                raise Exception("文生图 API 返回数据为空")
            return data[0]["url"]

    async def image_to_text(self, image_url: str, prompt: str = "",
                            model: Optional[str] = None) -> str:
        """识图：给定图片和文字提示，返回文字描述。
        
        API: POST {api_base}/chat/completions
        
        Request (OpenAI 多模态格式):
        {
            "model": "...",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "..."}},
                    {"type": "text", "text": "..."}
                ]
            }],
            "max_tokens": 1024
        }
        """
        url = f"{self.api_base}/chat/completions"
        model_name = model or self.default_vision_model
        if not model_name:
            raise Exception("未配置识图模型，请在 WebUI 中设置 vision_model 后重试。")

        payload = {
            "model": model_name,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_url}},
                    {"type": "text", "text": prompt or "请详细描述这张图片的内容"}
                ]
            }],
            "max_tokens": 1024
        }

        logger.debug(f"AiDraw image_to_text 请求: model={payload['model']}, "
                     f"prompt_len={len(prompt)}")

        async with self.session.post(url, json=payload) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                raise Exception(f"识图 API 错误 ({resp.status}): {error_text}")
            result = await resp.json()
            logger.debug(f"AiDraw image_to_text 响应成功")

            choices = result.get("choices", [])
            if not choices:
                raise Exception("识图 API 返回数据为空")
            return choices[0]["message"]["content"]

    async def test_connection(self) -> str:
        """测试 API 连通性。

        API: GET {api_base}/models
        返回模型列表中的第一个 model id 作为连通性证明。
        """
        url = f"{self.api_base}/models"
        async with self.session.get(url) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                raise Exception(f"API 连通性测试失败 ({resp.status}): {error_text}")
            result = await resp.json()
            models = result.get("data", [])
            if not models:
                raise Exception("API 连通性测试失败：未返回模型列表")
            first_model = models[0].get("id", "unknown")
            total = len(models)
            return f"✅ 连接成功！可用模型 {total} 个，首个: {first_model}"

    async def close(self):
        """关闭 HTTP 会话"""
        if self.session and not self.session.closed:
            await self.session.close()
