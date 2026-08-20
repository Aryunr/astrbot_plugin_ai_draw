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
    - OpenAI 官方 API 等。

    支持生图和识图使用不同 API endpoint（如 GPT 生图 + Gemini 识图）。
    """

    def __init__(self, api_base: str, api_key: str,
                 default_gen_model: str, default_vision_model: str,
                 default_size: str, save_images: bool = False,
                 gen_api_base: Optional[str] = None,
                 gen_api_key: Optional[str] = None,
                 vision_api_base: Optional[str] = None,
                 vision_api_key: Optional[str] = None):
        """
        Args:
            api_base: 共享 API 基础地址（未单独配置 gen/vision 时的 fallback）
            api_key: 共享 API Key
            default_gen_model: 默认文生图模型名
            default_vision_model: 默认识图模型名
            default_size: 默认图片尺寸
            save_images: 是否在本地保存生成图片
            gen_api_base: 文生图专用 API 地址（可选，不填则用 api_base）
            gen_api_key: 文生图专用 API Key（可选，不填则用 api_key）
            vision_api_base: 识图专用 API 地址（可选，不填则用 api_base）
            vision_api_key: 识图专用 API Key（可选，不填则用 api_key）
        """
        self.default_gen_model = default_gen_model
        self.default_vision_model = default_vision_model
        self.default_size = default_size
        self.save_images = save_images

        # 生图 endpoint（优先专用配置，fallback 到共享配置）
        gen_base = (gen_api_base or api_base).rstrip('/')
        gen_key = gen_api_key or api_key
        self._gen_base = gen_base
        self._gen_session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {gen_key}"},
            timeout=aiohttp.ClientTimeout(total=120)
        )

        # 识图 endpoint（优先专用配置，fallback 到共享配置）
        vision_base = (vision_api_base or api_base).rstrip('/')
        vision_key = vision_api_key or api_key
        self._vision_base = vision_base
        self._vision_session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {vision_key}"},
            timeout=aiohttp.ClientTimeout(total=120)
        )

    @property
    def name(self) -> str:
        return f"gen={self._gen_base} | vision={self._vision_base}"

    async def text_to_image(self, prompt: str, model: Optional[str] = None,
                            size: Optional[str] = None) -> str:
        """文生图。使用 _gen_session 发送请求。"""
        url = f"{self._gen_base}/images/generations"
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
                     f"size={payload['size']}, base={self._gen_base}, prompt_len={len(prompt)}")

        async with self._gen_session.post(url, json=payload) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                raise Exception(f"文生图 API 错误 ({resp.status}): {error_text}")
            result = await resp.json()
            logger.debug(f"AiDraw text_to_image 响应成功")

            data = result.get("data", [])
            if not data:
                raise Exception("文生图 API 返回数据为空")
            item = data[0]
            if "url" in item:
                return item["url"]
            elif "b64_json" in item:
                # 把 base64 保存成文件并返回路径
                import base64
                import uuid
                img_data = base64.b64decode(item["b64_json"])
                path = f"data/temp/ai_draw/{uuid.uuid4().hex}.jpg"
                with open(path, "wb") as f:
                    f.write(img_data)
                return path

    async def image_to_text(self, image_url: str, prompt: str = "",
                            model: Optional[str] = None) -> str:
        """识图。使用 _vision_session 发送请求。"""
        url = f"{self._vision_base}/chat/completions"
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
                     f"base={self._vision_base}, prompt_len={len(prompt)}")

        async with self._vision_session.post(url, json=payload) as resp:
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
        """测试 API 连通性。分别测试生图和识图 endpoint。"""
        results = []

        # 测试生图 endpoint
        try:
            url = f"{self._gen_base}/models"
            async with self._gen_session.get(url) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    models = result.get("data", [])
                    first = models[0].get("id", "unknown") if models else "unknown"
                    results.append(f"[生图] ✅ {self._gen_base} ({len(models)}个模型, 首: {first})")
                else:
                    results.append(f"[生图] ❌ {self._gen_base} (HTTP {resp.status})")
        except Exception as e:
            results.append(f"[生图] ❌ {self._gen_base} ({e})")

        # 如果识图 endpoint 和生图不同，也测一下
        if self._vision_base != self._gen_base:
            try:
                url = f"{self._vision_base}/models"
                async with self._vision_session.get(url) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        models = result.get("data", [])
                        first = models[0].get("id", "unknown") if models else "unknown"
                        results.append(f"[识图] ✅ {self._vision_base} ({len(models)}个模型, 首: {first})")
                    else:
                        results.append(f"[识图] ❌ {self._vision_base} (HTTP {resp.status})")
            except Exception as e:
                results.append(f"[识图] ❌ {self._vision_base} ({e})")
        else:
            results.append("[识图] 共用生图 endpoint")

        return "\n".join(results)

    async def close(self):
        """关闭所有 HTTP 会话"""
        for session in (self._gen_session, self._vision_session):
            if session and not session.closed:
                await session.close()
