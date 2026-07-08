"""

AI 文生图/识图插件 for AstrBot



支持 OpenAI 兼容 API（硅基流动、OneAPI 等），提供：

- 命令模式: /draw, /describe 等

- LLM Agent 模式: 自然语言触发画图/识图

- 权限控制: 管理员无限制 / 普通用户每日限额

- 临时文件自动清理

"""

import os

import sys

import json

import time

import uuid

import asyncio

import base64
import re

import logging

from datetime import datetime

from typing import Optional, List



import aiohttp




_plugin_dir = os.path.dirname(os.path.abspath(__file__))

if _plugin_dir not in sys.path:

    sys.path.insert(0, _plugin_dir)



from astrbot.api.event import AstrMessageEvent, MessageEventResult, MessageChain

from astrbot.api.star import Context, Star, register

from astrbot.api.all import command as filter

from astrbot.api import AstrBotConfig, logger

import astrbot.api.message_components as Comp




try:

    from astrbot.core.star.register import register_on_llm_request, register_on_agent_begin

    from astrbot.api.provider import ProviderRequest

    _HAS_LLM_HOOK = True

except ImportError:

    _HAS_LLM_HOOK = False

    logger.warning("AiDraw: 当前 AstrBot 版本不支持 register_on_llm_request 钩子")



from providers.openai_compat import OpenAICompatProvider

from context_memory import ContextMemory





class _SuppressImageCaptionError(logging.Filter):




    def filter(self, record: logging.LogRecord) -> bool:

        msg = record.getMessage()

        if "处理图片描述失败" in msg:

            return False

        if "处理引用图片失败" in msg:

            return False

        if "No valid file or URL provided" in msg:

            return False

        if "unknown variant `image_url`" in msg:

            return False

        return True






_suppress_filter = _SuppressImageCaptionError()

_suppress_filter_applied = False





def _apply_suppress_filter():


    global _suppress_filter_applied

    if _suppress_filter_applied:

        return

    target_loggers = [

        "astrbot",              # v4.25+ 主 logger 名称

        "core.astr_main_agent",  # 兼容旧版本

        "astrbot.core.astr_main_agent",

        "astrbot.agent_sub_stages",  # 引用消息处理

    ]

    for name in target_loggers:

        lg = logging.getLogger(name)

        if _suppress_filter not in lg.filters:

            lg.addFilter(_suppress_filter)

            logger.info(f"AiDraw: 已挂载日志过滤器到 {name}")

    _suppress_filter_applied = True





class PermissionManager:




    def __init__(self, config: AstrBotConfig):

        self.config = config

        self.usage_data: dict = {}

        self.usage_file: str = ""

        self._loaded = False



    def _ensure_loaded(self):

        """延迟加载，确保 data 目录已就绪"""

        if not self._loaded:

            data_dir = os.path.join("data", "ai_draw")

            os.makedirs(data_dir, exist_ok=True)

            self.usage_file = os.path.join(data_dir, "usage.json")

            self.usage_data = self._load()

            self._loaded = True



    def is_enabled(self) -> bool:

        """权限控制是否开启"""

        return self.config.get("enable_permission", False)



    def is_admin(self, user_id: str) -> bool:

        """判断用户是否为管理员"""

        if not self.is_enabled():

            return True  # 权限关闭时，全员视为管理员

        admin_ids = self.config.get("admin_ids", [])

        return str(user_id) in [str(a) for a in admin_ids]



    def can_call(self, user_id: str) -> bool:

        """判断用户是否还有调用额度



        权限关闭 → 所有人都可用

        管理员  → 无限制

        普通用户 → 检查每日限额

        """

        if not self.is_enabled():

            return True

        if self.is_admin(user_id):

            return True

        self._ensure_loaded()

        usage = self._get_today_usage(user_id)

        limit = self.config.get("daily_limit", 10)

        return usage < limit



    def record_call(self, user_id: str):

        """记录一次调用（管理员不记入）"""

        if not self.is_enabled():

            return

        if self.is_admin(user_id):

            return

        self._ensure_loaded()

        self._increment(user_id)



    def reset_user(self, admin_user_id: str, target_user_id: str) -> bool:

        """管理员重置某个用户的今日额度"""

        if not self.is_admin(admin_user_id):

            return False

        self._ensure_loaded()

        today = datetime.now().strftime("%Y-%m-%d")

        if today in self.usage_data and str(target_user_id) in self.usage_data[today]:

            del self.usage_data[today][str(target_user_id)]

            self._save()

            return True

        return False



    def get_today_usage(self, user_id: str) -> int:

        """查询用户今日使用量"""

        if not self.is_enabled():

            return 0

        self._ensure_loaded()

        return self._get_today_usage(user_id)






    def _get_today_usage(self, user_id: str) -> int:

        today = datetime.now().strftime("%Y-%m-%d")

        return self.usage_data.get(today, {}).get(str(user_id), 0)



    def _increment(self, user_id: str):

        today = datetime.now().strftime("%Y-%m-%d")

        if today not in self.usage_data:

            self.usage_data[today] = {}

        uid = str(user_id)

        self.usage_data[today][uid] = self.usage_data[today].get(uid, 0) + 1

        self._save()

        self._cleanup_old()



    def _cleanup_old(self):

        """只保留最近 7 天数据"""

        keys = sorted(self.usage_data.keys(), reverse=True)

        for k in keys[7:]:

            del self.usage_data[k]



    def _load(self) -> dict:

        if os.path.exists(self.usage_file):

            try:

                with open(self.usage_file, "r", encoding="utf-8") as f:

                    return json.load(f)

            except (json.JSONDecodeError, OSError) as e:

                logger.warning(f"AiDraw 读取用量文件失败: {e}，将使用空数据")

                return {}

        return {}



    def _save(self):

        try:

            with open(self.usage_file, "w", encoding="utf-8") as f:

                json.dump(self.usage_data, f, indent=2, ensure_ascii=False)

        except OSError as e:

            logger.error(f"AiDraw 保存用量文件失败: {e}")





@register("ai_draw", "Aryun",

          "AI 文生图/识图插件，支持 OpenAI 兼容 API",

          "",

          "1.1.0")

class AiDrawPlugin(Star):




    def __init__(self, context: Context, config: AstrBotConfig):

        super().__init__(context)

        self.config = config

        self.provider: Optional[OpenAICompatProvider] = None

        self.perm_mgr = PermissionManager(config)

        self.temp_dir: Optional[str] = None



        # 静默主代理的"处理图片描述失败"日志噪音

        _apply_suppress_filter()



        # 存储暂存的图片，供 on_llm_request 使用（从 on_agent_begin 传入）

        self._pending_images: dict = {}



        # 图片消息缓存：message_id -> URL（用于引用/回复消息图片兜底）

        self._image_cache: dict = {}

        self._load_image_cache()



        # 会话识图结果缓存：session_id -> analysis_text（用于后续无图片消息的上下文注入）

        self._recent_analyses: dict[str, str] = {}



        # 已在 Scene 1（on_llm_request）处理过的会话 ID 集合

        self._scene1_handled_sessions: set[str] = set()



        # 最近画图记录: session_id -> (prompt, timestamp)，防止追问被误判为画图

        self._last_draw: dict[str, tuple] = {}

        # 上下文记忆（v1.1.0）
        if self.config.get("context_memory_enabled", True):
            db_path = os.path.join(self.config.get("temp_dir", "data/temp/ai_draw"),
                                   "context_memory.db")
            self.context_memory = ContextMemory(
                db_path,
                max_entries=self.config.get("context_memory_max_entries", 20),
                max_age_hours=self.config.get("context_memory_max_age_hours", 24),
            )
            logger.info("AiDraw 上下文记忆已启用")
        else:
            self.context_memory = None






        # 初始化 Provider

        self._init_provider()



        logger.info(

            "AiDraw 插件已加载。"

            f" 权限控制: {'开启' if config.get('enable_permission', False) else '关闭'}"

            f"  LLM Agent: {'开启' if config.get('enable_llm_agent', True) else '关闭'}"

        )




    # 初始化




    def _init_provider(self):

        """从配置初始化图片服务提供商"""

        api_base = self.config.get("api_base", "").strip()

        api_key = self.config.get("api_key", "").strip()

        if not api_base or not api_key:

            logger.warning("AiDraw: 未配置 API 地址或 API Key，部分功能不可用。"

                           "请在 WebUI 中配置后使用 /test_draw_api 测试连通性。")

            return



        gen_model = self.config.get("gen_model", "").strip()

        vision_model = self.config.get("vision_model", "").strip()



        self.provider = OpenAICompatProvider(

            api_base=api_base,

            api_key=api_key,

            default_gen_model=gen_model or None,

            default_vision_model=vision_model or None,

            default_size=self.config.get("image_size", "1024x1024"),

        )

        logger.info(f"AiDraw Provider 已初始化: {api_base}")




    # 临时文件管理




    def _ensure_temp_dir(self) -> str:

        """确保临时目录存在，返回绝对路径"""

        if self.temp_dir and os.path.exists(self.temp_dir):

            return self.temp_dir



        temp_dir_config = self.config.get("temp_dir", "data/temp/ai_draw")

        if not os.path.isabs(temp_dir_config):

            temp_dir_config = os.path.abspath(temp_dir_config)



        os.makedirs(temp_dir_config, exist_ok=True)

        self.temp_dir = temp_dir_config

        return self.temp_dir



    @staticmethod

    def _make_chain(components: list) -> MessageChain:

        """从组件列表构造 MessageChain"""

        chain = MessageChain()

        chain.chain = list(components)

        return chain



    async def _download_image(self, url: str) -> str:

        """从 URL 下载图片到本地临时目录，返回本地路径"""

        temp_dir = self._ensure_temp_dir()

        ext = ".png"

        filename = f"{uuid.uuid4().hex}{ext}"

        local_path = os.path.join(temp_dir, filename)



        async with aiohttp.ClientSession() as session:

            async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:

                resp.raise_for_status()



                # 根据 Content-Type 修正扩展名

                content_type = resp.headers.get("Content-Type", "")

                if "jpeg" in content_type or "jpg" in content_type:

                    local_path = local_path.replace(".png", ".jpg")

                elif "webp" in content_type:

                    local_path = local_path.replace(".png", ".webp")

                elif "gif" in content_type:

                    local_path = local_path.replace(".png", ".gif")



                with open(local_path, "wb") as f:

                    f.write(await resp.read())



        # 异步触发过期文件清理（不等待）

        asyncio.create_task(self._cleanup_temp_files())



        logger.debug(f"AiDraw 图片已下载: {local_path}")

        return local_path



    async def _save_base64_image(self, b64_data: str) -> str:

        """将 base64 图片数据解码并保存到本地临时目录，返回本地路径"""

        temp_dir = self._ensure_temp_dir()

        filename = f"{uuid.uuid4().hex}.png"

        local_path = os.path.join(temp_dir, filename)



        # 去掉可能的 data:image/...;base64, 前缀

        if b64_data.startswith("data:"):

            b64_data = b64_data.split(",", 1)[1]



        image_bytes = base64.b64decode(b64_data)

        with open(local_path, "wb") as f:

            f.write(image_bytes)



        asyncio.create_task(self._cleanup_temp_files())

        logger.debug(f"AiDraw base64 图片已保存: {local_path}")

        return local_path



    async def _cleanup_temp_files(self):

        """清理超过 TTL 的临时文件"""

        temp_dir = self._ensure_temp_dir()

        ttl = self.config.get("temp_ttl", 3600)

        if ttl <= 0:

            return

        now = time.time()

        cleaned = 0

        for f in os.listdir(temp_dir):

            fpath = os.path.join(temp_dir, f)

            if os.path.isfile(fpath) and now - os.path.getmtime(fpath) > ttl:

                try:

                    os.remove(fpath)

                    cleaned += 1

                except OSError:

                    pass

        if cleaned > 0:

            logger.debug(f"AiDraw 已清理 {cleaned} 个过期临时文件")




    # 图片组件检测与URL提取工具




    def _is_image_comp(self, comp) -> bool:

        """检测消息组件是否为图片类型"""

        # 方式1：通过 type 名称判断（最可靠）

        type_name = getattr(comp, 'type', None)

        if type_name and 'image' in type_name.lower():

            return True

        # 方式2：通过 isinstance 判断

        try:

            return isinstance(comp, Comp.Image)

        except Exception:

            pass

        return False



    def _comp_to_image_url(self, comp) -> Optional[str]:

        """从图片组件中提取可访问的 URL

        

        优先级: url (CDN) > file (本地路径) > convert_to_file_path()

        """

        if not self._is_image_comp(comp):

            return None

        # 优先使用 CDN URL

        url = getattr(comp, 'url', None) or getattr(comp, 'file', None)

        if url:

            # 将 file:// 转为普通路径

            if isinstance(url, str) and url.startswith('file:///'):

                url = url[8:]

            return url

        # 最后尝试 convert_to_file_path

        try:

            if hasattr(comp, 'convert_to_file_path') and callable(comp.convert_to_file_path):

                import inspect

                result = comp.convert_to_file_path()

                if inspect.isawaitable(result):

                    # 同步方法中无法 await，跳过

                    return None

                return str(result)

        except Exception:

            pass

        return None




    # 命令注册（仅用于管理面板显示，实际命令处理在 on_llm_request）

    # 使用 (event, *args, **kwargs) 避免 StarRequestSubStage 传参 TypeError




    @filter("/my_usage")

    async def _ui_cmd_my_usage(self, event, *args, **kwargs):

        """查看今日画图使用统计"""
        pass



    @filter("/admin_reset")

    async def _ui_cmd_admin_reset(self, event, *args, **kwargs):

        """重置指定用户今日额度(管理员)"""
        pass



    @filter("/test_draw_api")

    async def _ui_cmd_test_draw(self, event, *args, **kwargs):

        """测试绘图API连通性，不生成图片(管理员)"""
        pass



    @filter("/draw")

    async def _ui_cmd_draw(self, event, *args, **kwargs):

        """根据文字描述生成图片"""
        pass



    @filter("/describe")

    async def _ui_cmd_describe(self, event, *args, **kwargs):

        """描述引用的图片内容"""
        pass




    # Agent 开始钩子 — 在 main agent 提取图片之前拦截




    @register_on_agent_begin(priority=99)

    async def on_agent_begin(self, event, *args, **kwargs):

        if not _HAS_LLM_HOOK:

            return

        if not hasattr(event, 'message_obj') or not event.message_obj:


            return

        if not hasattr(event.message_obj, 'message') or not event.message_obj.message:


            return



        # 检查此会话是否已在 on_llm_request (Scene 1) 处理过

        try:

            sess = str(event.get_sender_id())

            if sess in self._scene1_handled_sessions:


                found, remaining = self._extract_images_recursive(event.message_obj.message)

                if found:

                    event.message_obj.message = remaining



                    await self._process_quote_images(event, found)

                    return



                return

        except Exception:

            pass

        found_images, remaining = self._extract_images_recursive(

            event.message_obj.message

        )



        if not found_images:

            return




        event.message_obj.message = remaining



        event_id = str(id(event.message_obj))

        self._pending_images[event_id] = found_images





        await self._process_quote_images(event, found_images)



        try:

            sess = str(event.get_sender_id())

            self._scene1_handled_sessions.add(sess)

            self._clear_tool_images_cache()

        except Exception:

            pass



    async def _process_quote_images(self, event, found_images):

        """在 on_agent_begin 中直接处理引用图片的识图"""

        if not found_images:

            return



        # 取第一张图片

        image_comp = found_images[0]

        image_url = self._comp_to_image_url(image_comp)



        # 如果组件字段为空，尝试从缓存查找（兜底 OneBot 不返回图片地址）

        if not image_url:

            logger.warning("AiDraw on_agent_begin: 无法从图片组件提取 URL，尝试缓存...")

            quoted_msg_id = self._get_quoted_message_id(event)

            if quoted_msg_id and quoted_msg_id in self._image_cache:

                image_url = self._image_cache[quoted_msg_id]




        if not image_url:

            logger.warning("AiDraw on_agent_begin: 无法从图片组件提取 URL，缓存也未命中")

            return



        # 转为可访问的 URL

        final_url = await self._convert_to_url(image_url)

        if not final_url:

            return



        # 识图

        try:

            vision_model = self.config.get("vision_model") or None

            image_desc = await self.provider.image_to_text(

                final_url,

                prompt="请详细描述这张图片的内容",

                model=vision_model,

            )

            reply = f"{image_desc}"

            await event.send(self._make_chain([Comp.Plain(text=reply)]))

            # 保存到会话缓存，供后续无图片的消息使用

            try:

                session_id = str(event.get_sender_id())

                self._recent_analyses[session_id] = image_desc


            except Exception:

                pass

        except Exception as e:

            logger.error(f"AiDraw 引用图片识图失败: {e}")

            return



        # 阻止后续处理，避免 Agent 重复回复

        event.stop_event()

        logger.info("AiDraw _process_quote_images: 已阻止 Agent 后续处理")




    # 图片提取与转换（增强版：优先使用缓存）




    def _load_image_cache(self) -> None:

        """从 JSON 文件加载图片消息缓存"""

        cache_file = os.path.join(os.path.dirname(__file__), "image_cache.json")

        if os.path.exists(cache_file):

            try:

                with open(cache_file, "r", encoding="utf-8") as f:

                    data = json.load(f)

                if isinstance(data, dict):

                    self._image_cache = data

                    logger.info(f"AiDraw: 已加载 {len(self._image_cache)} 条图片缓存")

            except Exception as e:

                logger.warning(f"AiDraw: 加载图片缓存失败: {e}")



    def _save_image_cache(self) -> None:

        """将图片消息缓存保存到 JSON 文件"""

        cache_file = os.path.join(os.path.dirname(__file__), "image_cache.json")

        try:

            with open(cache_file, "w", encoding="utf-8") as f:

                json.dump(self._image_cache, f, ensure_ascii=False, indent=2)

        except Exception as e:

            logger.warning(f"AiDraw: 保存图片缓存失败: {e}")



    @staticmethod

    def _get_quoted_message_id(event) -> Optional[str]:

        """从事件中提取被引用消息的 message_id"""

        try:

            if hasattr(event, 'message_obj') and event.message_obj:

                for comp in event.message_obj.message:

                    if isinstance(comp, Comp.Reply):

                        return str(getattr(comp, 'id', ''))

            return None

        except Exception:

            return None



    def _clear_tool_images_cache(self) -> int:


        try:

            from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

            cache_dir = os.path.join(get_astrbot_temp_path(), "tool_images")

        except ImportError:

            cache_dir = os.path.join("data", "temp", "tool_images")

        if not os.path.isdir(cache_dir):

            return 0

        cleaned = 0

        for fname in os.listdir(cache_dir):

            fpath = os.path.join(cache_dir, fname)

            if os.path.isfile(fpath):

                try:

                    os.remove(fpath)

                    cleaned += 1

                except OSError:

                    pass

        if cleaned > 0:

            logger.info(f"AiDraw: 已清理 {cleaned} 个工具图片缓存文件")

        return cleaned



    def _clear_temp_image_files(self, temp_dir: str) -> int:


        if not os.path.isdir(temp_dir):

            return 0

        cleaned = 0

        for fname in os.listdir(temp_dir):

            # 匹配 Agent 可能读取的压缩图片文件

            if fname.startswith("compressed_") and fname.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):

                fpath = os.path.join(temp_dir, fname)

                if os.path.isfile(fpath):

                    try:

                        os.remove(fpath)

                        cleaned += 1

                    except OSError:

                        pass

        if cleaned > 0:

            logger.info(f"AiDraw: 已清理 {cleaned} 个临时图片文件")

        return cleaned



    def _format_description(self, text: str) -> str:

        """根据配置格式化识别结果：去除 Markdown + 字数限制"""

        if not text:

            return text

        original = text

        if self.config.get("no_markdown", True):

            import re as _re

            text = _re.sub(r'#{1,6}\s+', '', text)

            text = _re.sub(r'\*\*(.+?)\*\*', r'\1', text)

            text = _re.sub(r'\*(.+?)\*', r'\1', text)

            text = _re.sub(r'```[\s\S]*?```', '', text)

            text = _re.sub(r'`(.+?)`', r'\1', text)

            text = _re.sub(r'!\[.*?\]\(.*?\)', '', text)

            text = _re.sub(r'\[(.+?)\]\(.*?\)', r'\1', text)

            text = _re.sub(r'^\s*[-*+]\s+', '', text, flags=_re.MULTILINE)

            text = _re.sub(r'^\s*\d+\.\s+', '', text, flags=_re.MULTILINE)

            text = _re.sub(r'^>\s+', '', text, flags=_re.MULTILINE)

        max_len = self.config.get("max_description_length", 500)

        if max_len > 0 and len(text) > max_len:

            text = text[:max_len] + "..."

        text = text.strip()

        if not text:

            logger.warning(f"AiDraw _format_description: 格式化后结果为空，使用原始文本 (len={len(original)})")

            return original.strip()

        return text



    def _extract_images_recursive(self, components: list):

        """递归提取组件列表中的所有图片，包括 Reply/Quote 嵌套的。



        Returns: (images_list, remaining_list)

        """

        found = []

        remaining = []

        for comp in components:

            if self._is_image_comp(comp):

                found.append(comp)

                # 图片组件不加入 remaining

            else:

                # 检查是否包含嵌套消息链（Reply/Quote/Forward 的 message/chain/content 属性）

                nested = None

                for attr in ('message', 'chain', 'content', 'items'):

                    if hasattr(comp, attr):

                        val = getattr(comp, attr)

                        if isinstance(val, list) and val:

                            nested = val

                            break

                if nested:

                    sub_images, sub_remaining = self._extract_images_recursive(nested)

                    found.extend(sub_images)

                    # 将提取后的嵌套列表写回组件

                    try:

                        for attr in ('message', 'chain', 'content', 'items'):

                            if hasattr(comp, attr) and getattr(comp, attr) is nested:

                                setattr(comp, attr, sub_remaining)

                                break

                    except Exception:

                        pass

                remaining.append(comp)

        return found, remaining



    def _extract_image_from_event(self, event: AstrMessageEvent) -> Optional[str]:

        """从事件中提取图片，返回本地路径或 URL。



        优先使用 on_agent_begin 中保存的缓存图片（因为 on_agent_begin

        已从事件消息链中移除了图片以防止主代理报错）。

        如果缓存中没有，则从事件消息链中递归查找（包括引用/回复嵌套的图片）。

        """

        event_id = str(id(event.message_obj))

        cached_images = self._pending_images.pop(event_id, None)

        if cached_images:

            for comp in cached_images:

                result = self._comp_to_image_url(comp)

                if result:

                    return result

            return None



        # 回退：递归查找消息链中的所有图片（包括引用/回复嵌套的）

        if hasattr(event, 'message_obj') and event.message_obj:

            found, _ = self._extract_images_recursive(event.message_obj.message)

            if found:

                return self._comp_to_image_url(found[0])

        return None


    # 工具：将 URL 转为 data URI（用于 API 调用）




    async def _convert_to_url(self, image_path: str) -> str:

        """将图片路径转为可直接使用的 URL（或 data URI）"""

        if image_path.startswith(("http://", "https://")):

            return image_path

        if os.path.exists(image_path):

            import base64

            with open(image_path, "rb") as f:

                data = base64.b64encode(f.read()).decode("utf-8")

            ext = os.path.splitext(image_path)[1].lower()

            mime = "image/png"

            if ext in (".jpg", ".jpeg"):

                mime = "image/jpeg"

            elif ext == ".webp":

                mime = "image/webp"

            elif ext == ".gif":

                mime = "image/gif"

            return f"data:{mime};base64,{data}"

        return image_path




    # LLM Agent 模式：处理图片识别 & 文生图




    @register_on_llm_request(priority=99)

    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest, *args, **kwargs):

        if not _HAS_LLM_HOOK:

            return






        # 检查是否启用 LLM Agent 模式

        if not self.config.get("enable_llm_agent", True):

            return



        user_id = event.get_sender_id()

        if not self.perm_mgr.can_call(user_id):

            return



        # 提取图片（优先：on_agent_begin 缓存 > req.image_urls > 消息链直接查找）

        image_comp = self._extract_image_from_event(event)



        # 补充：检查 req.image_urls（主代理已从引用/回复消息中提取了图片）

        if not image_comp and hasattr(req, 'image_urls') and req.image_urls:

            image_comp = req.image_urls[0]

            logger.debug(f"AiDraw on_llm_request: 从 req.image_urls 获取引用图片")



        # 获取用户消息（去除命令前缀）

        user_message = event.message_str.strip()

        # 去除 [At:xxx] 前缀，使 @ 机器人发命令也能被识别

        import re as _re_cmd

        user_message = _re_cmd.sub(r"^\[At:\d+\]\s*", "", user_message).strip()




        # 注意：wake_prefix（如"/"）可能已被AstrBot剥离，不能只用startswith("/")

        _cmd_raw = user_message.split()[0].lower() if user_message.split() else ""

        _is_cmd = _cmd_raw.startswith("/") or _cmd_raw in ("my_usage", "用法", "usage", "admin_reset", "test_draw_api")

        if _is_cmd:

            # 统一补"/"，兼容wake_prefix已剥离"/"的情况

            cmd_lower = ("/" + _cmd_raw) if not _cmd_raw.startswith("/") else _cmd_raw



            # /my_usage 或 /usage

            if cmd_lower in ("/my_usage", "/用法", "/usage"):

                if not self.perm_mgr.can_call(user_id):

                    return

                usage_info = f"📊 用户 {user_id} 的使用统计:\n"

                if self.perm_mgr.is_enabled():

                    usage = self.perm_mgr.get_today_usage(user_id)

                    limit = self.config.get("daily_limit", 10)

                    if self.perm_mgr.is_admin(user_id):

                        usage_info += f"管理员模式，今日已使用: {usage} 次\n不限额度"

                    else:

                        usage_info += f"今日已使用: {usage}/{limit} 次"

                else:

                    usage_info += "权限控制未开启"

                await event.send(self._make_chain([Comp.Plain(text=usage_info)]))

                req.prompt = "[SYSTEM: System command handled, do not reply.]"

                event.stop_event()

                return



            # /admin_reset

            if cmd_lower == "/admin_reset":

                if not self.perm_mgr.is_admin(user_id):

                    await event.send(self._make_chain([Comp.Plain(text="❌ 你没有权限执行此操作")]))

                    req.prompt = "[SYSTEM: System command handled, do not reply.]"

                    event.stop_event()

                    return

                parts = user_message.split(None, 2)

                if len(parts) < 2:

                    await event.send(self._make_chain([Comp.Plain(text="用法: /admin_reset <用户ID>")]))

                    req.prompt = "[SYSTEM: System command handled, do not reply.]"

                    event.stop_event()

                    return

                target = parts[1].strip()

                if self.perm_mgr.reset_user(user_id, target):

                    await event.send(self._make_chain([Comp.Plain(text=f"✅ 已重置用户 {target} 的今日额度")]))

                else:

                    await event.send(self._make_chain([Comp.Plain(text=f"❌ 用户 {target} 今日无使用记录或重置失败")]))

                req.prompt = "[SYSTEM: System command handled, do not reply.]"

                event.stop_event()

                return



            # /test_draw_api（仅测试 API 连通性，不生成图片）

            if cmd_lower == "/test_draw_api":

                if not self.perm_mgr.is_admin(user_id):

                    await event.send(self._make_chain([Comp.Plain(text="❌ 你没有权限执行此操作")]))

                    req.prompt = "[SYSTEM: System command /test_draw_api handled, do NOT generate images, do NOT reply.]"

                    event.stop_event()

                    return

                if not self.provider:

                    await event.send(self._make_chain([Comp.Plain(text="❌ API 未配置")]))

                    req.prompt = "[SYSTEM: System command /test_draw_api handled, do NOT generate images, do NOT reply.]"

                    event.stop_event()

                    return

                await event.send(self._make_chain([Comp.Plain(text="🔧 正在测试 API 连接...")]))

                try:

                    api_base = self.provider.api_base.rstrip("/")

                    test_url = f"{api_base}/models"

                    headers = {"Authorization": f"Bearer {self.provider.api_key}"}

                    async with aiohttp.ClientSession() as session:

                        async with session.get(test_url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:

                            if resp.status == 200:

                                data = await resp.json()

                                model_count = len(data.get("data", []))

                                await event.send(self._make_chain([Comp.Plain(

                                    text=f"✅ API 连接正常（{api_base}）\n状态码: {resp.status}\n可用模型数: {model_count}"

                                )]))

                            else:

                                body = await resp.text()

                                await event.send(self._make_chain([Comp.Plain(

                                    text=f"⚠️ API 返回异常状态码: {resp.status}\n{body[:200]}"

                                )]))

                except asyncio.TimeoutError:

                    await event.send(self._make_chain([Comp.Plain(text=f"❌ API 连接超时（{api_base}）")]))

                except Exception as e:

                    await event.send(self._make_chain([Comp.Plain(text=f"❌ API 连接失败: {e}")]))

                req.prompt = "[SYSTEM: System command /test_draw_api handled, API test result already sent. Do NOT generate ANY images. Do NOT reply. Ignore this message completely.]"


                event.stop_event()


                return



            # /draw 和 /describe 由场景1/2自动处理，此处不做额外拦截





        _scene0_injected = False

        if not image_comp:
            session_id = str(user_id)
            group_id_str = str(group_id) if group_id else ""

            if self.context_memory:
                # 优先检查 #N 引用
                user_msg_lower = user_message.strip() if user_message else ""
                seq_match = re.search(r'#(\d+)', user_msg_lower) if user_msg_lower else None

                if seq_match:
                    seq = int(seq_match.group(1))
                    record = self.context_memory.get_record(str(user_id), group_id_str, seq)
                    if record:
                        record_type_label = "生成" if record.get("record_type") == "draw" else "识别"
                        record_type_emoji = "🎨" if record.get("record_type") == "draw" else "📷"
                        record_parts = [f"[上下文记忆 - 引用记录 #{seq}]"]
                        record_parts.append(f"类型: {record_type_emoji} {record_type_label}")
                        record_parts.append(f"提示词: {record.get('prompt', '')}")
                        if record.get("result_text"):
                            record_parts.append(f"结果描述: {record['result_text']}")
                        record_parts.append("[请基于以上记录的内容回答用户问题]")

                        full_record = "\n".join(record_parts)
                        if user_message:
                            req.prompt = f"{user_message}\n\n{full_record}"
                        else:
                            req.prompt = full_record
                        logger.info(f"AiDraw on_llm_request: 注入上下文记录 #{seq} (session={user_id}:{group_id_str})")
                        _scene0_injected = True
                else:
                    # 模糊关键词匹配（排除绘图/识图指令）
                    search_text = user_msg_lower
                    if search_text and not any(kw in search_text for kw in ["画", "生成", "/draw", "/describe", "识图", "描述"]):
                        search_result = self.context_memory.search(str(user_id), group_id_str, search_text)
                        if search_result:
                            record_type_emoji2 = "🎨" if search_result.get("record_type") == "draw" else "📷"
                            record_type_label2 = "生成" if search_result.get("record_type") == "draw" else "识别"
                            record_parts2 = [f"[上下文记忆 - 匹配相关记录 #{search_result.get('seq', '?')}]"]
                            record_parts2.append(f"类型: {record_type_emoji2} {record_type_label2}")
                            record_parts2.append(f"提示词: {search_result.get('prompt', '')}")
                            if search_result.get("result_text"):
                                record_parts2.append(f"结果描述: {search_result['result_text']}")
                            record_parts2.append("[以上记录与用户问题高度相关，请基于此回答]")

                            full_record2 = "\n".join(record_parts2)
                            if user_message:
                                req.prompt = f"{user_message}\n\n{full_record2}"
                            else:
                                req.prompt = full_record2
                            logger.info(f"AiDraw on_llm_request: 模糊匹配注入记录 (session={user_id}:{group_id_str})")
                            _scene0_injected = True

                # 如果上面都没有匹配，且启用了摘要注入
                if not _scene0_injected and self.config.get("context_memory_inject_summary", True):
                    summary = self.context_memory.get_summary(str(user_id), group_id_str)
                    if summary:
                        if user_message:
                            req.prompt = f"{user_message}\n\n{summary}"
                        else:
                            req.prompt = summary
                        logger.info(f"AiDraw on_llm_request: 注入上下文摘要 (session={user_id}:{group_id_str})")
                        _scene0_injected = True
            else:
                # 回退到旧有的 _recent_analyses 模式（仅识图缓存，限时120秒）
                if session_id in self._recent_analyses:
                    cached = self._recent_analyses[session_id]
                    if cached and cached.strip():
                        analysis_note = f"[系统提示：用户之前发送了一张图片，图片分析结果如下]\n{cached.strip()}\n[请严格基于以上分析结果回答用户的问题。注意：用户当前发送的是纯文本消息，没有附加图片，请勿尝试查找或描述任何图片文件]"
                        if user_message:
                            req.prompt = f"{user_message}\n\n{analysis_note}"
                        else:
                            req.prompt = analysis_note
                        logger.info(f"AiDraw on_llm_request: 已注入识图结果缓存 (session={session_id}, len={len(cached.strip())})")
                        _scene0_injected = True
                    else:
                        logger.warning(f"AiDraw on_llm_request: 会话缓存内容为空，跳过注入 (session={session_id})")




                else:

                    logger.warning(f"AiDraw on_llm_request: 会话缓存内容为空，跳过注入 (session={session_id})")




        if image_comp:

            if not self.provider:

                logger.warning("AiDraw: Provider 未初始化，无法识图")

                return



            # 提取用户对图片的问题（如果有）

            describe_prompt = user_message if user_message else "请详细描述这张图片的内容"



            logger.debug(f"AiDraw on_llm_request: 开始识图")



            try:

                # 确保图片路径可用

                if not image_comp.startswith(("http://", "https://", "data:")):

                    if os.path.exists(image_comp):

                        image_comp = await self._convert_to_url(image_comp)

                    else:

                        req.prompt = f"[系统提示：无法访问用户发送的图片文件，请告知用户重新发送]\n\n{user_message}"

                        return



                result = await self.provider.image_to_text(image_comp, describe_prompt)

                if result:

                    self.perm_mgr.record_call(user_id)

                    # 缓存当前图片 URL，用于引用消息图片兜底

                    try:

                        msg_id = str(getattr(event.message_obj, 'message_id', None) or '')

                        if msg_id and isinstance(image_comp, str) and image_comp.startswith(("http://", "https://", "data:")):

                            self._image_cache[msg_id] = image_comp

                            self._save_image_cache()

                            logger.info(f"AiDraw on_llm_request: cached image msg_id={msg_id}")

                    except Exception:

                        pass

                    # 格式化结果（去 markdown + 截断），供缓存使用

                    cached_text = self._format_description(result)

                    # 保存到会话缓存，供后续无图片的消息使用

                    session_id = str(user_id)

                    self._recent_analyses[session_id] = cached_text
                    if self.context_memory:
                        try:
                            self.context_memory.add(
                                str(user_id), str(group_id) if group_id else "",
                                "describe", user_message, cached_text
                            )
                        except Exception:
                            pass

                    logger.info(f"AiDraw on_llm_request: 已缓存识图结果 (session={session_id})")

                    # 追踪此会话已在 Scene 1 处理过

                    self._scene1_handled_sessions.add(session_id)

                    # 直接发送格式化回复，然后 stop_event() 阻止 LLM 调用和 Agent 子阶段

                    try:

                        reply = f"{result.strip()}"

                        reply = self._format_description(reply)

                        await event.send(self._make_chain([Comp.Plain(text=reply)]))

                        logger.info("AiDraw on_llm_request: 已直接发送识图结果")

                        # 阻止后续处理：LLM 调用、Agent 子阶段

                        event.stop_event()

                        logger.info("AiDraw on_llm_request: 已阻止 LLM 和 Agent 后续处理")

                    except Exception as e:

                        logger.warning(f"AiDraw on_llm_request: 直接发送失败 ({e})，降级为 LLM 注入")

                        # 降级方案：注入到 req.prompt 让 LLM 回复

                        analysis_note = (

                            f"[系统提示：用户发送了一张图片，图片分析结果如下]\n"

                            f"{result.strip()}\n"

                            f"[请基于以上分析结果直接回答用户的问题，不要重复分析过程，"

                            f"不要使用markdown格式，回答控制在100字以内，"

                            f"不要编造与图片分析结果不符的内容]"

                        )

                        if user_message:

                            req.prompt = f"{user_message}\n\n{analysis_note}"

                        else:

                            req.prompt = analysis_note

                        logger.info(f"AiDraw on_llm_request: 已注入识图结果到 LLM 请求 (len={len(result.strip())})")

                    # 清理原始压缩图片文件，防止 Agent 工具通过文件路径直接读取

                    try:

                        from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

                        temp_dir = get_astrbot_temp_path()

                    except ImportError:

                        temp_dir = os.path.join("data", "temp")

                    self._clear_temp_image_files(temp_dir)

                    # 清理工具图片缓存，防止 Agent 读取旧图片

                    try:

                        self._clear_tool_images_cache()

                    except Exception:

                        pass

                else:

                    logger.warning("AiDraw 识图结果为空，将交由 LLM 处理")

            except Exception as e:

                logger.error(f"AiDraw 识图失败: {e}")



            return


        draw_keywords = [

            "画", "生成", "绘制", "创作", "画一张", "生成一张",

            "draw", "generate", "create", "make a picture",

            "帮我画", "给我画", "帮我生成",

        ]

        # 关键守卫：命令类消息不触发画图（含"/"或被wake_prefix剥离后的命令名）

        _first_word = user_message.split()[0].lower() if user_message.split() else ""

        _cmd_names = ("my_usage", "用法", "usage", "admin_reset", "test_draw_api")

        if "/" in user_message or _first_word in _cmd_names:


            should_draw = False

        else:

            should_draw = any(kw in user_message.lower() for kw in draw_keywords)



        # 追问检测：刚画完图后的短追问（"生成什么"/"画了什么"等），不重复画图

        _session_id = str(user_id)

        if _session_id in self._last_draw:

            _, _last_time = self._last_draw[_session_id]

            _elapsed = time.time() - _last_time

            if _elapsed < 120:

                _question_words = ("什么", "啥", "吗", "呢", "吧", "哪个", "怎么", "?")

                if any(q in user_message for q in _question_words):

                    should_draw = False

                    logger.info(f'AiDraw: 追问检测命中（elapsed={_elapsed:.0f}s），跳过画图: {user_message[:30]}')

                elif len(user_message) < 15 and any(kw in user_message for kw in draw_keywords):

                    should_draw = False

                    logger.info(f'AiDraw: 极短追问检测命中，跳过画图: {user_message[:30]}')



        if should_draw:

            if not self.provider:

                logger.warning("AiDraw: Provider 未初始化，无法画图")

                return



            logger.debug(f"AiDraw on_llm_request: 开始文生图")



            # [新方案] 注入等待提示到 req.prompt，不阻止事件传播

            # 不再使用 event.stop_event()（stop_event 会导致后续发送阶段被跳过）

            original_content = req.prompt

            req.prompt = f"[系统提示：用户请求文生图，正在生成中，请稍候...]\n\n{user_message}"



            try:

                image_data = await self.provider.text_to_image(user_message)

                if image_data:

                    self.perm_mgr.record_call(user_id)

                    # 将图片数据转为本地文件路径

                    if image_data.startswith("data:"):

                        local_path = await self._save_base64_image(image_data)

                    elif image_data.startswith(("http://", "https://")):

                        local_path = await self._download_image(image_data)

                    else:

                        local_path = image_data

                    chain = [Comp.Plain(text="🎨 生成完成："), Comp.Image(local_path)]

                    await event.send(self._make_chain(chain))

                    # 更新 req.prompt 告知 LLM 图片已发送

                    req.prompt = f"[系统提示：用户请求文生图，图片已生成并发送给用户。请根据用户的问题回复。]\n\n{user_message}"

                    self._last_draw[_session_id] = (user_message, time.time())
                    if self.context_memory:
                        try:
                            self.context_memory.add(
                                str(sender_id), str(group_id) if group_id else "",
                                "draw", user_message, "", [local_path]
                            )
                        except Exception:
                            pass

                else:

                    req.prompt = f"[系统提示：文生图失败，请告知用户检查 API 配置]\n\n{user_message}"

            except Exception as e:

                logger.error(f"AiDraw 文生图失败: {e}")

                req.prompt = f"[系统提示：文生图出错: {e}]\n\n{original_content}"



            return

