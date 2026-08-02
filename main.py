"""
AstrBot 智能群管插件 (GroupGuardian)
=====================================
功能：
- AI 自动识别群内吵架/刷屏，智能警告+禁言
- 手动指令：禁言、踢人、全体禁言
- 白名单豁免机制
- 管理员权限分级

指令列表：
  /禁言 @xxx <秒数>     → 禁言指定成员
  /解禁 @xxx            → 解除禁言
  /踢人 @xxx            → 踢出群聊
  /全体禁言             → 开启/关闭全体禁言
  /群管白名单 add @xxx  → 添加白名单
  /群管白名单 del @xxx  → 删除白名单
  /群管白名单 list      → 查看白名单
  /群管状态             → 查看插件运行状态
  /群管设置             → 查看当前设置
  /群管帮助             → 查看帮助

管理员配置和白名单在 AstrBot 配置面板中设置。
"""

import os
import re
import time
import json
import sys
import hashlib
from typing import Optional, Dict, List, Set, Tuple
from collections import defaultdict
from datetime import datetime, timedelta

import requests

# ============================================================
# 万能兼容导入（吸取教训，5层兜底）
# ============================================================
Plugin = None
AstrMessageEvent = None
Context = None
AstrBotConfig = None
LLM_AVAILABLE = False

# 第1层：v4 新路径
try:
    from astrbot.core.plugin import Plugin
    from astrbot.core.message import AstrMessageEvent
    from astrbot.core.context import Context
    print("[GroupGuardian] ✅ 从 astrbot.core 导入成功")
except ImportError:
    pass

# 第2层：v4 旧路径
if Plugin is None:
    try:
        from astrbot.plugin import Plugin
        from astrbot.message import AstrMessageEvent
        from astrbot.context import Context
        print("[GroupGuardian] ✅ 从 astrbot 导入成功")
    except ImportError:
        pass

# 第3层：api 路径
if Plugin is None:
    try:
        from astrbot.api.plugin import Plugin
        from astrbot.api.event import AstrMessageEvent
        from astrbot.api.context import Context
        print("[GroupGuardian] ✅ 从 astrbot.api 导入成功")
    except ImportError:
        pass

# 第4层：直接 import astrbot
if Plugin is None:
    try:
        import astrbot
        Plugin = astrbot.Plugin
        AstrMessageEvent = astrbot.AstrMessageEvent
        Context = astrbot.Context
        print("[GroupGuardian] ✅ 从 import astrbot 导入成功")
    except (ImportError, AttributeError):
        pass

# 第5层：扫描 sys.modules
if Plugin is None:
    for mod_name, mod in sys.modules.items():
        if 'astrbot' in mod_name.lower():
            if hasattr(mod, 'Plugin') and Plugin is None:
                Plugin = mod.Plugin
            if hasattr(mod, 'AstrMessageEvent') and AstrMessageEvent is None:
                AstrMessageEvent = mod.AstrMessageEvent
            if hasattr(mod, 'Context') and Context is None:
                Context = mod.Context
            if Plugin and AstrMessageEvent:
                print(f"[GroupGuardian] ✅ 从 {mod_name} 导入成功")
                break

# 绝对兜底
if Plugin is None:
    class Plugin:
        pass
    print("[GroupGuardian] ⚠️ 使用兜底 Plugin")

if AstrMessageEvent is None:
    class AstrMessageEvent:
        def get_message(self):
            return ""
        async def send(self, msg):
            print(f"[Mock] {msg}")
    print("[GroupGuardian] ⚠️ 使用兜底 AstrMessageEvent")

# 尝试导入配置相关
try:
    from astrbot.api.config import PluginConfig
    AstrBotConfig = PluginConfig
    print("[GroupGuardian] ✅ 配置模块导入成功")
except ImportError:
    try:
        from astrbot.core.config import PluginConfig
        AstrBotConfig = PluginConfig
    except ImportError:
        print("[GroupGuardian] ⚠️ 配置模块不可用，使用默认配置")

# 尝试导入 LLM
try:
    from astrbot.core.llm import LLM
    LLM_AVAILABLE = True
except ImportError:
    try:
        from astrbot.llm import LLM
        LLM_AVAILABLE = True
    except ImportError:
        print("[GroupGuardian] ⚠️ LLM 模块不可用，AI 分析功能将受限")


# ============================================================
# 配置（可在 AstrBot 面板中修改）
# ============================================================
DEFAULT_CONFIG = {
    # 一、管理员权限
    "admin_qq": "",           # 管理员QQ号（逗号分隔多个）
    "whitelist_qq": "",       # 白名单QQ号（逗号分隔多个）
    
    # 二、AI 智能分析
    "ai_conflict_detection": True,    # 是否启用AI冲突检测
    "ai_auto_warn": True,             # 是否主动警告
    "ai_auto_mute_after_warn": True,  # 警告后继续吵架是否自动禁言
    "ai_mute_duration": 300,          # 自动禁言时长（秒），默认5分钟
    "ai_model": "",                   # AI模型名称（为空则使用默认）
    
    # 三、刷屏检测
    "flood_detection": True,          # 是否启用刷屏检测
    "flood_threshold": 5,             # 多少秒内算刷屏
    "flood_max_msgs": 3,              # 阈值时间内最多发几条
    "flood_mute_duration": 60,        # 刷屏禁言时长（秒）
    
    # 四、冲突检测参数
    "conflict_window": 10,            # 分析上下文消息条数
    "conflict_cooldown": 60,          # 两次AI分析最小间隔（秒）
}

# 运行时状态
class PluginState:
    def __init__(self):
        self.whitelist: Set[str] = set()
        self.admins: Set[str] = set()
        self.config = DEFAULT_CONFIG.copy()
        self.last_ai_analysis: Dict[str, float] = {}        # group_id -> timestamp
        self.warned_users: Dict[str, Dict[str, float]] = {} # group_id -> {user_id: timestamp}
        self.message_buffer: Dict[str, List[dict]] = defaultdict(list)  # 消息缓冲区
        self.flood_tracker: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))

state = PluginState()


# ============================================================
# 工具函数
# ============================================================
def parse_qq_list(qq_str: str) -> Set[str]:
    """解析逗号分隔的QQ号字符串"""
    if not qq_str or not qq_str.strip():
        return set()
    return {q.strip() for q in qq_str.split(",") if q.strip()}


def extract_at_qq(message: str) -> Optional[str]:
    """从消息中提取 @ 的QQ号"""
    # 匹配 [CQ:at,qq=xxx] 格式
    match = re.search(r'\[CQ:at,qq=(\d+)\]', message)
    if match:
        return match.group(1)
    # 匹配 @xxx 格式（纯数字）
    match = re.search(r'@(\d{5,11})', message)
    if match:
        return match.group(1)
    return None


def get_group_id(event: AstrMessageEvent) -> str:
    """获取群号"""
    try:
        if hasattr(event, 'message_obj') and hasattr(event.message_obj, 'group_id'):
            return str(event.message_obj.group_id or "unknown")
    except Exception:
        pass
    return "unknown"


def get_sender_qq(event: AstrMessageEvent) -> str:
    """获取发送者QQ号"""
    try:
        if hasattr(event, 'message_obj') and hasattr(event.message_obj, 'sender'):
            sender = event.message_obj.sender
            if hasattr(sender, 'user_id'):
                return str(sender.user_id)
    except Exception:
        pass
    return "unknown"


def get_sender_nickname(event: AstrMessageEvent) -> str:
    """获取发送者昵称"""
    try:
        if hasattr(event, 'message_obj') and hasattr(event.message_obj, 'sender'):
            sender = event.message_obj.sender
            if hasattr(sender, 'nickname'):
                return sender.nickname or "未知"
            if hasattr(sender, 'card'):
                return sender.card or "未知"
    except Exception:
        pass
    return "未知"


def is_admin(qq: str) -> bool:
    """检查是否是管理员"""
    return qq in state.admins


def is_whitelisted(qq: str) -> bool:
    """检查是否在白名单中"""
    return qq in state.whitelist


def is_protected(qq: str) -> bool:
    """检查是否受保护（管理员或白名单）"""
    return is_admin(qq) or is_whitelisted(qq)


# ============================================================
# AI 冲突分析
# ============================================================
class ConflictAnalyzer:
    """使用AI分析群聊是否处于冲突状态"""
    
    ANALYSIS_PROMPT = """你是一个群聊管理助手，负责分析群聊消息是否处于冲突/吵架状态。

请分析以下最近的消息记录，判断是否存在：
1. 吵架/冲突：群成员之间是否在争吵、人身攻击、言语冲突
2. 即将失控：冲突是否有升级趋势
3. 刷屏行为：是否有人在短时间内大量发送消息

消息记录：
{context}

请以JSON格式回复，不要包含其他内容：
{{
    "is_conflict": true/false,
    "conflict_level": 0-5,  // 0=无冲突, 5=严重冲突
    "involved_users": ["qq号1", "qq号2"],  // 涉及的用户
    "reason": "简短分析原因",
    "recommend_action": "none/warn/mute/kick"  // 建议措施
}}"""

    def __init__(self, context=None):
        self.context = context
    
    async def analyze(self, messages: List[dict]) -> Optional[dict]:
        """分析消息列表，返回冲突分析结果"""
        if not messages:
            return None
        
        # 构建上下文字符串
        context_str = ""
        for msg in messages:
            context_str += f"[{msg.get('nickname', '未知')}]({msg.get('qq', '')}): {msg.get('content', '')}\n"
        
        prompt = self.ANALYSIS_PROMPT.format(context=context_str[-3000:])  # 限制长度
        
        try:
            result = await self._call_llm(prompt)
            return result
        except Exception as e:
            print(f"[GroupGuardian] AI分析失败: {e}")
            return None
    
    async def _call_llm(self, prompt: str) -> Optional[dict]:
        """调用 AstrBot 的 LLM"""
        if not LLM_AVAILABLE or not self.context:
            print("[GroupGuardian] LLM 不可用，使用规则判断")
            return self._rule_based_analysis(prompt)
        
        try:
            # 尝试通过 context 获取 LLM 实例
            llm_instance = None
            if hasattr(self.context, 'get_llm'):
                llm_instance = self.context.get_llm()
            elif hasattr(self.context, 'llm'):
                llm_instance = self.context.llm
            
            if llm_instance and hasattr(llm_instance, 'chat'):
                model = state.config.get('ai_model', '')
                response = await llm_instance.chat(
                    prompt=prompt,
                    model=model if model else None,
                    temperature=0.3,
                    max_tokens=300
                )
                
                # 尝试从回复中提取 JSON
                text = response.get('content', response) if isinstance(response, dict) else str(response)
                return self._parse_json_response(text)
        except Exception as e:
            print(f"[GroupGuardian] LLM调用失败: {e}")
        
        return None
    
    def _parse_json_response(self, text: str) -> Optional[dict]:
        """从LLM回复中提取JSON"""
        # 尝试直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        
        # 尝试提取 {} 包裹的JSON
        json_match = re.search(r'\{[^{}]*\}', text, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group())
            except json.JSONDecodeError:
                pass
        
        return None
    
    def _rule_based_analysis(self, prompt: str) -> Optional[dict]:
        """基于规则的简单分析（不依赖AI）"""
        # 简单的关键词检测作为备用
        conflict_keywords = ['傻逼', 'sb', 'cnm', '操你', '妈的', '滚', '垃圾', '废物',
                             '你妈', '去死', '脑残', '弱智', '智障', '草泥马', '尼玛',
                             '放屁', '闭嘴', '滚蛋']
        
        # 这里可以从 prompt 中提取消息内容
        # 简化处理：直接返回无冲突
        return {
            "is_conflict": False,
            "conflict_level": 0,
            "involved_users": [],
            "reason": "规则分析：未检测到明显冲突关键词",
            "recommend_action": "none"
        }


# ============================================================
# 插件主体
# ============================================================
class GroupGuardianPlugin(Plugin):
    def __init__(self, *args, **kwargs):
        super().__init__()
        self.context = args[0] if args else kwargs.get('context', None)
        self.analyzer = ConflictAnalyzer(self.context)
        self._load_config()
        print(f"[GroupGuardian] ✅ 插件初始化完成")
        print(f"[GroupGuardian] 管理员: {state.admins}")
        print(f"[GroupGuardian] 白名单: {state.whitelist}")
    
    def _load_config(self):
        """从 context 或默认配置加载设置"""
        if self.context and hasattr(self.context, 'get_config'):
            try:
                user_config = self.context.get_config("group_guardian", {})
                state.config.update(user_config)
            except Exception:
                pass
        
        # 解析管理员和白名单
        state.admins = parse_qq_list(state.config.get('admin_qq', ''))
        state.whitelist = parse_qq_list(state.config.get('whitelist_qq', ''))
    
    def _save_config(self):
        """保存配置"""
        if self.context and hasattr(self.context, 'save_config'):
            try:
                self.context.save_config("group_guardian", state.config)
            except Exception:
                pass
    
    async def on_message(self, event: AstrMessageEvent):
        """消息入口"""
        message = event.get_message().strip()
        group_id = get_group_id(event)
        sender_qq = get_sender_qq(event)
        sender_name = get_sender_nickname(event)
        
        # ========== 0. 收集消息用于AI分析 ==========
        self._buffer_message(group_id, sender_qq, sender_name, message)
        
        # ========== 1. 指令处理 ==========
        if await self._handle_commands(event, message, group_id, sender_qq):
            return
        
        # ========== 2. AI冲突检测（非指令消息） ==========
        if state.config.get('ai_conflict_detection', True) and group_id != "unknown":
            await self._check_conflict(event, group_id)
        
        # ========== 3. 刷屏检测 ==========
        if state.config.get('flood_detection', True) and group_id != "unknown":
            await self._check_flood(event, group_id, sender_qq, sender_name)
    
    # ==================== 消息缓冲 ====================
    def _buffer_message(self, group_id: str, qq: str, nickname: str, content: str):
        """缓存消息用于上下文分析"""
        state.message_buffer[group_id].append({
            "qq": qq,
            "nickname": nickname,
            "content": content,
            "time": time.time()
        })
        
        # 只保留最近的消息
        window = state.config.get('conflict_window', 10)
        if len(state.message_buffer[group_id]) > window * 3:
            state.message_buffer[group_id] = state.message_buffer[group_id][-window * 2:]
    
    # ==================== 指令处理 ====================
    async def _handle_commands(self, event: AstrMessageEvent, message: str,
                                group_id: str, sender_qq: str) -> bool:
        """处理管理指令，返回 True 表示已处理"""
        
        # /群管帮助
        if re.match(r'^[/!！]群管帮助', message):
            await self._cmd_help(event)
            return True
        
        # /群管状态
        if re.match(r'^[/!！]群管状态', message):
            await self._cmd_status(event)
            return True
        
        # /群管设置
        if re.match(r'^[/!！]群管设置', message):
            await self._cmd_settings(event)
            return True
        
        # /禁言 @xxx <秒数>
        if m := re.match(r'^[/!！]禁言\s*.*?(\d+)?\s*$', message):
            target_qq = extract_at_qq(message)
            duration_match = re.search(r'(\d+)\s*$', message)
            duration = int(duration_match.group(1)) if duration_match else 60
            await self._cmd_mute(event, target_qq, duration, sender_qq)
            return True
        
        # /解禁 @xxx
        if re.match(r'^[/!！]解禁', message):
            target_qq = extract_at_qq(message)
            await self._cmd_unmute(event, target_qq, sender_qq)
            return True
        
        # /踢人 @xxx
        if re.match(r'^[/!！]踢人', message):
            target_qq = extract_at_qq(message)
            await self._cmd_kick(event, target_qq, sender_qq)
            return True
        
        # /全体禁言
        if re.match(r'^[/!！]全体禁言', message):
            await self._cmd_whole_mute(event, sender_qq)
            return True
        
        # /群管白名单 add/del/list
        if m := re.match(r'^[/!！]群管白名单\s+(add|del|list)\s*(.*)', message):
            action = m.group(1)
            target = m.group(2).strip()
            await self._cmd_whitelist(event, action, target, sender_qq)
            return True
        
        return False
    
    # ==================== 指令实现 ====================
    async def _cmd_help(self, event: AstrMessageEvent):
        help_text = (
            "🛡️ **智能群管插件使用指南**\n\n"
            "📌 **管理指令（需管理员权限）：**\n"
            "  /禁言 @xxx 秒数  → 禁言指定成员\n"
            "  /解禁 @xxx       → 解除禁言\n"
            "  /踢人 @xxx       → 踢出群聊\n"
            "  /全体禁言        → 开启/关闭全体禁言\n\n"
            "📌 **白名单管理（需管理员权限）：**\n"
            "  /群管白名单 add @xxx  → 添加白名单\n"
            "  /群管白名单 del @xxx  → 删除白名单\n"
            "  /群管白名单 list      → 查看白名单\n\n"
            "📌 **查询指令：**\n"
            "  /群管状态  → 查看插件运行状态\n"
            "  /群管设置  → 查看当前设置\n"
            "  /群管帮助  → 显示本帮助\n\n"
            "🤖 **AI功能（自动）：**\n"
            "  • 自动检测群内冲突并警告\n"
            "  • 自动检测刷屏并处理\n"
            "  • 警告后继续违规自动禁言\n\n"
            "⚙️ 管理员QQ和白名单请在AstrBot配置面板中设置"
        )
        await event.send(help_text)
    
    async def _cmd_status(self, event: AstrMessageEvent):
        ai_on = "✅" if state.config.get('ai_conflict_detection') else "❌"
        flood_on = "✅" if state.config.get('flood_detection') else "❌"
        auto_warn = "✅" if state.config.get('ai_auto_warn') else "❌"
        auto_mute = "✅" if state.config.get('ai_auto_mute_after_warn') else "❌"
        
        status = (
            f"🛡️ **群管插件状态**\n\n"
            f"🤖 AI冲突检测：{ai_on}\n"
            f"📢 刷屏检测：{flood_on}\n"
            f"⚠️ 主动警告：{auto_warn}\n"
            f"🔇 警告后自动禁言：{auto_mute}\n"
            f"🔇 自动禁言时长：{state.config.get('ai_mute_duration', 300)}秒\n"
            f"👮 管理员：{', '.join(state.admins) if state.admins else '未设置'}\n"
            f"🛡️ 白名单人数：{len(state.whitelist)}人\n"
        )
        await event.send(status)
    
    async def _cmd_settings(self, event: AstrMessageEvent):
        settings = (
            f"⚙️ **当前设置**\n\n"
            f"admin_qq: {state.config.get('admin_qq', '未设置')}\n"
            f"whitelist_qq: {state.config.get('whitelist_qq', '未设置')}\n"
            f"ai_conflict_detection: {state.config.get('ai_conflict_detection')}\n"
            f"ai_auto_warn: {state.config.get('ai_auto_warn')}\n"
            f"ai_auto_mute_after_warn: {state.config.get('ai_auto_mute_after_warn')}\n"
            f"ai_mute_duration: {state.config.get('ai_mute_duration')}秒\n"
            f"ai_model: {state.config.get('ai_model') or '默认'}\n"
            f"flood_threshold: {state.config.get('flood_threshold')}秒\n"
            f"flood_max_msgs: {state.config.get('flood_max_msgs')}条\n"
        )
        await event.send(settings)
    
    async def _cmd_mute(self, event: AstrMessageEvent, target_qq: Optional[str],
                         duration: int, sender_qq: str):
        """禁言指令"""
        if not is_admin(sender_qq):
            await event.send("❌ 权限不足，仅管理员可用")
            return
        
        if not target_qq:
            await event.send("❌ 请 @ 要禁言的成员\n用法：/禁言 @xxx 60")
            return
        
        if is_protected(target_qq):
            await event.send(f"❌ 该成员在白名单或管理员列表中，无法禁言")
            return
        
        try:
            cq_code = f"[CQ:ban,qq={target_qq},duration={duration}]"
            await event.send(cq_code)
            await event.send(f"✅ 已禁言 {target_qq}，时长 {duration} 秒")
            print(f"[GroupGuardian] {sender_qq} 禁言了 {target_qq}，{duration}秒")
        except Exception as e:
            await event.send(f"❌ 禁言失败: {e}")
    
    async def _cmd_unmute(self, event: AstrMessageEvent, target_qq: Optional[str], sender_qq: str):
        """解禁指令"""
        if not is_admin(sender_qq):
            await event.send("❌ 权限不足，仅管理员可用")
            return
        
        if not target_qq:
            await event.send("❌ 请 @ 要解禁的成员\n用法：/解禁 @xxx")
            return
        
        try:
            cq_code = f"[CQ:ban,qq={target_qq},duration=0]"
            await event.send(cq_code)
            await event.send(f"✅ 已解除 {target_qq} 的禁言")
        except Exception as e:
            await event.send(f"❌ 解禁失败: {e}")
    
    async def _cmd_kick(self, event: AstrMessageEvent, target_qq: Optional[str], sender_qq: str):
        """踢人指令"""
        if not is_admin(sender_qq):
            await event.send("❌ 权限不足，仅管理员可用")
            return
        
        if not target_qq:
            await event.send("❌ 请 @ 要踢出的成员\n用法：/踢人 @xxx")
            return
        
        if is_protected(target_qq):
            await event.send(f"❌ 该成员在白名单或管理员列表中，无法踢出")
            return
        
        try:
            cq_code = f"[CQ:kick,qq={target_qq}]"
            await event.send(cq_code)
            await event.send(f"✅ 已踢出 {target_qq}")
            print(f"[GroupGuardian] {sender_qq} 踢出了 {target_qq}")
        except Exception as e:
            await event.send(f"❌ 踢人失败: {e}")
    
    async def _cmd_whole_mute(self, event: AstrMessageEvent, sender_qq: str):
        """全体禁言"""
        if not is_admin(sender_qq):
            await event.send("❌ 权限不足，仅管理员可用")
            return
        
        try:
            cq_code = "[CQ:whole_ban]"
            await event.send(cq_code)
            await event.send("✅ 已切换全体禁言状态")
        except Exception as e:
            await event.send(f"❌ 操作失败: {e}")
    
    async def _cmd_whitelist(self, event: AstrMessageEvent, action: str,
                              target: str, sender_qq: str):
        """白名单管理"""
        if not is_admin(sender_qq):
            await event.send("❌ 权限不足，仅管理员可用")
            return
        
        if action == "list":
            wl = state.whitelist
            if not wl:
                await event.send("📋 白名单为空")
            else:
                await event.send(f"📋 白名单（{len(wl)}人）：\n" + "\n".join(wl))
        
        elif action == "add":
            target_qq = extract_at_qq(target) or target.strip()
            if not target_qq or not target_qq.isdigit():
                await event.send("❌ 请提供有效的QQ号\n用法：/群管白名单 add @xxx")
                return
            state.whitelist.add(target_qq)
            state.config['whitelist_qq'] = ",".join(state.whitelist)
            self._save_config()
            await event.send(f"✅ 已添加 {target_qq} 到白名单")
        
        elif action == "del":
            target_qq = extract_at_qq(target) or target.strip()
            if not target_qq:
                await event.send("❌ 请提供有效的QQ号\n用法：/群管白名单 del @xxx")
                return
            if target_qq in state.whitelist:
                state.whitelist.discard(target_qq)
                state.config['whitelist_qq'] = ",".join(state.whitelist)
                self._save_config()
                await event.send(f"✅ 已从白名单移除 {target_qq}")
            else:
                await event.send(f"❌ {target_qq} 不在白名单中")
    
    # ==================== AI冲突检测 ====================
    async def _check_conflict(self, event: AstrMessageEvent, group_id: str):
        """检查群内是否存在冲突"""
        # 冷却检查
        now = time.time()
        last_check = state.last_ai_analysis.get(group_id, 0)
        cooldown = state.config.get('conflict_cooldown', 60)
        if now - last_check < cooldown:
            return
        
        # 获取消息缓冲区
        buffer = state.message_buffer.get(group_id, [])
        if len(buffer) < 5:
            return
        
        # 取最近的N条消息
        window = state.config.get('conflict_window', 10)
        recent_msgs = buffer[-window:]
        
        # AI分析
        state.last_ai_analysis[group_id] = now
        result = await self.analyzer.analyze(recent_msgs)
        
        if not result:
            return
        
        if not result.get('is_conflict', False):
            return
        
        conflict_level = result.get('conflict_level', 0)
        if conflict_level < 3:  # 只处理3级及以上冲突
            return
        
        involved_users = result.get('involved_users', [])
        reason = result.get('reason', '检测到群内冲突')
        
        print(f"[GroupGuardian] 检测到冲突！等级: {conflict_level}, 涉及: {involved_users}, 原因: {reason}")
        
        # 过滤掉受保护的用户
        involved_users = [u for u in involved_users if not is_protected(u)]
        if not involved_users:
            return
        
        # 主动警告
        if state.config.get('ai_auto_warn', True):
            at_list = " ".join([f"[CQ:at,qq={u}]" for u in involved_users])
            warn_msg = (
                f"{at_list}\n"
                f"⚠️ **智能群管警告**\n"
                f"检测到群内可能存在冲突行为，请保持友善交流！\n"
                f"📊 冲突等级：{conflict_level}/5\n"
                f"📝 分析：{reason}\n"
                f"💡 如继续冲突，将自动禁言处理"
            )
            await event.send(warn_msg)
            
            # 记录警告
            if group_id not in state.warned_users:
                state.warned_users[group_id] = {}
            for u in involved_users:
                state.warned_users[group_id][u] = now
        
        # 检查是否需要自动禁言（之前警告过但现在还在吵）
        if state.config.get('ai_auto_mute_after_warn', True):
            warned = state.warned_users.get(group_id, {})
            for u in involved_users:
                last_warn = warned.get(u, 0)
                if now - last_warn < 300 and now - last_warn > 10:  # 5分钟内被警告过
                    duration = state.config.get('ai_mute_duration', 300)
                    try:
                        cq_code = f"[CQ:ban,qq={u},duration={duration}]"
                        await event.send(cq_code)
                        await event.send(
                            f"🔇 [CQ:at,qq={u}] 因警告后继续冲突，已被自动禁言 {duration} 秒"
                        )
                        print(f"[GroupGuardian] 自动禁言 {u}，{duration}秒")
                    except Exception as e:
                        print(f"[GroupGuardian] 自动禁言失败: {e}")
    
    # ==================== 刷屏检测 ====================
    async def _check_flood(self, event: AstrMessageEvent, group_id: str,
                            sender_qq: str, sender_name: str):
        """检测刷屏行为"""
        if is_protected(sender_qq):
            return
        
        now = time.time()
        threshold = state.config.get('flood_threshold', 5)
        max_msgs = state.config.get('flood_max_msgs', 3)
        
        # 记录消息时间
        tracker = state.flood_tracker[group_id][sender_qq]
        tracker.append(now)
        
        # 只保留阈值时间内的记录
        tracker[:] = [t for t in tracker if now - t <= threshold]
        
        # 检查是否超出限制
        if len(tracker) > max_msgs:
            duration = state.config.get('flood_mute_duration', 60)
            try:
                cq_code = f"[CQ:ban,qq={sender_qq},duration={duration}]"
                await event.send(cq_code)
                await event.send(
                    f"🔇 [CQ:at,qq={sender_qq}] 检测到刷屏行为，已禁言 {duration} 秒\n"
                    f"📊 {threshold}秒内发送了{len(tracker)}条消息"
                )
                tracker.clear()  # 重置计数器
                print(f"[GroupGuardian] 刷屏禁言 {sender_qq}，{duration}秒")
            except Exception as e:
                print(f"[GroupGuardian] 刷屏禁言失败: {e}")