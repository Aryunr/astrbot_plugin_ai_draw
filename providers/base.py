from abc import ABC, abstractmethod
from typing import Optional


class BaseImageProvider(ABC):
    """图片服务提供商抽象基类。
    
    所有图片服务提供商（硅基流动、OpenAI、StabilityAI 等）都应继承此类。
    """

    @abstractmethod
    async def text_to_image(self, prompt: str, model: Optional[str] = None,
                            size: Optional[str] = None) -> str:
        """文生图。

        Args:
            prompt: 图片描述文本。
            model: 模型名称，为 None 则使用默认模型。
            size: 图片尺寸，如 "1024x1024"，为 None 则使用默认尺寸。

        Returns:
            生成的图片的临时 URL。

        Raises:
            Exception: API 调用失败时抛出。
        """
        raise NotImplementedError

    @abstractmethod
    async def image_to_text(self, image_url: str, prompt: str = "",
                            model: Optional[str] = None) -> str:
        """识图：给定图片和文字提示，返回文字描述。

        Args:
            image_url: 图片的 URL 或 data URL（base64）。
            prompt: 关于图片的提问或指示。
            model: 模型名称，为 None 则使用默认模型。

        Returns:
            图片的描述文字。

        Raises:
            Exception: API 调用失败时抛出。
        """
        raise NotImplementedError

    @abstractmethod
    async def test_connection(self) -> str:
        """测试 API 连通性。

        Returns:
            连通性测试结果描述字符串。

        Raises:
            Exception: 连通性测试失败时抛出。
        """
        raise NotImplementedError

    @property
    @abstractmethod
    def name(self) -> str:
        """提供商名称，用于日志和显示。"""
        raise NotImplementedError
