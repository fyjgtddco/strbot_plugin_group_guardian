"""
AstrBot 智能群管插件 (GroupGuardian)
=====================================
基于 AstrBot V4 官方 API 开发。

指令列表：
  /禁言 @xxx <秒数>       → 禁言指定成员
  /解禁 @xxx              → 解除禁言
  /踢人 @xxx              → 踢出群聊
  /全体禁言               → 开启/关闭全体禁言
  /群管白名单 add @xxx    → 添加白名单
  /群管白名单 del @xxx    → 删除白名单
  /群管白名单 list        → 查看白名单
  /群管状态               → 查看插件运行状态
  /群管帮助               → 查看帮助
"""

import re
import time
import json
from typing import Optional, List, Set
from collections import defaultdict

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star
from astrbot.api import logger, AstrBotConfig
from astrbot.api.message_components import Plain


class GroupGuardianPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        self._last_ai_analysis: dict = {}
        self._warned_users: dict = {}
        self._message_buffer: dict = defaultdict(list)
        self._flood_tracker: dict = defaultdict(lambda: defaultdict(list))

        logger.info("[GroupGuardian] ✅ 插件初始化完成")

    # ==================== 配置读取 ====================
    def _get_admins(self) -> Set[str]:
        s = self.config.get("admin_qq", "")
        return {q.strip() for q in s.split(",") if q.strip()} if s and s.strip() else set()

    def _get_whitelist(self) -> Set[str]:
        s = self.config.get("whitelist_qq", "")
        return {q.strip() for q in s.split(",") if q.strip()} if s and s.strip() else set()

    def _is_admin(self, qq: str) -> bool:
        return qq in self._get_admins()

    def _is_whitelisted(self, qq: str) -> bool:
        return qq in self._get_whitelist()

    def _is_protected(self, qq: str) -> bool:
        return self._is_admin(qq) or self._is_whitelisted(qq)

    def _extract_at(self, msg: str) -> Optional[str]:
        m = re.search(r'\[CQ:at,qq=(\d+)\]', msg)
        return m.group(1) if m else None

    def _get_group_id(self, event: AstrMessageEvent) -> Optional[str]:
        try:
            if hasattr(event, 'message_obj') and hasattr(event.message_obj, 'group_id'):
                gid = event.message_obj.group_id
                return str(gid) if gid else None
        except Exception:
            pass
        try:
            return event.get_group_id()
        except Exception:
            pass
        return None

    # ==================== 指令 ====================
    @filter.command("群管帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        yield event.plain_result(
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
            "  /群管帮助  → 显示本帮助\n\n"
            "🤖 **AI功能（自动）：**\n"
            "  • 自动检测群内冲突并 @ 警告\n"
            "  • 自动检测刷屏并禁言\n"
            "  • 警告后继续违规自动升级为禁言\n\n"
            "⚙️ 管理员QQ和白名单请在 Web 管理面板中设置"
        )

    @filter.command("群管状态")
    async def cmd_status(self, event: AstrMessageEvent):
        a = self._get_admins()
        w = self._get_whitelist()
        yield event.plain_result(
            f"🛡️ **群管插件状态**\n\n"
            f"🤖 AI冲突检测：{'✅' if self.config.get('ai_conflict_detection') else '❌'}\n"
            f"📢 刷屏检测：{'✅' if self.config.get('flood_detection') else '❌'}\n"
            f"⚠️ 主动警告：{'✅' if self.config.get('ai_auto_warn') else '❌'}\n"
            f"🔇 警告后自动禁言：{'✅' if self.config.get('ai_auto_mute_after_warn') else '❌'}\n"
            f"🔇 自动禁言时长：{self.config.get('ai_mute_duration')}秒\n"
            f"👮 管理员：{', '.join(a) if a else '未设置'}\n"
            f"🛡️ 白名单人数：{len(w)}人"
        )

    @filter.command("禁言")
    async def cmd_mute(self, event: AstrMessageEvent, target: str = "", duration: int = 60):
        sender = event.message_obj.sender.user_id
        if not self._is_admin(sender):
            yield event.plain_result("❌ 权限不足，仅管理员可用")
            return
        t = self._extract_at(event.message_str)
        if not t:
            yield event.plain_result("❌ 请 @ 要禁言的成员\n用法：/禁言 @xxx 60")
            return
        if self._is_protected(t):
            yield event.plain_result("❌ 该成员受保护，无法禁言")
            return
        yield event.plain_result(f"[CQ:ban,qq={t},duration={duration}]")
        yield event.plain_result(f"✅ 已禁言 {t}，{duration}秒")
        logger.info(f"[GroupGuardian] {sender} 禁言 {t} {duration}s")

    @filter.command("解禁")
    async def cmd_unmute(self, event: AstrMessageEvent):
        sender = event.message_obj.sender.user_id
        if not self._is_admin(sender):
            yield event.plain_result("❌ 权限不足，仅管理员可用")
            return
        t = self._extract_at(event.message_str)
        if not t:
            yield event.plain_result("❌ 请 @ 要解禁的成员")
            return
        yield event.plain_result(f"[CQ:ban,qq={t},duration=0]")
        yield event.plain_result(f"✅ 已解除 {t} 的禁言")

    @filter.command("踢人")
    async def cmd_kick(self, event: AstrMessageEvent):
        sender = event.message_obj.sender.user_id
        if not self._is_admin(sender):
            yield event.plain_result("❌ 权限不足，仅管理员可用")
            return
        t = self._extract_at(event.message_str)
        if not t:
            yield event.plain_result("❌ 请 @ 要踢出的成员")
            return
        if self._is_protected(t):
            yield event.plain_result("❌ 该成员受保护，无法踢出")
            return
        yield event.plain_result(f"[CQ:kick,qq={t}]")
        yield event.plain_result(f"✅ 已踢出 {t}")
        logger.info(f"[GroupGuardian] {sender} 踢出 {t}")

    @filter.command("全体禁言")
    async def cmd_whole_mute(self, event: AstrMessageEvent):
        if not self._is_admin(event.message_obj.sender.user_id):
            yield event.plain_result("❌ 权限不足，仅管理员可用")
            return
        yield event.plain_result("[CQ:whole_ban]")
        yield event.plain_result("✅ 已切换全体禁言状态")

    @filter.command("群管白名单")
    async def cmd_whitelist(self, event: AstrMessageEvent):
        sender = event.message_obj.sender.user_id
        if not self._is_admin(sender):
            yield event.plain_result("❌ 权限不足，仅管理员可用")
            return
        parts = event.message_str.strip().split()
        act = parts[1] if len(parts) > 1 else "list"

        if act == "list":
            w = self._get_whitelist()
            yield event.plain_result(f"📋 白名单({len(w)}人)：\n" + "\n".join(w) if w else "📋 白名单为空")
            return

        target = self._extract_at(event.message_str) or (parts[2] if len(parts) > 2 else "")
        if not target or not target.isdigit():
            yield event.plain_result("❌ 请提供有效QQ号")
            return

        cur = [q.strip() for q in self.config.get("whitelist_qq", "").split(",") if q.strip()]

        if act == "add":
            if target in cur:
                yield event.plain_result(f"⚠️ {target} 已在白名单")
            else:
                cur.append(target)
                self.config["whitelist_qq"] = ",".join(cur)
                await self.config.save_config()
                yield event.plain_result(f"✅ 已添加 {target} 到白名单")
        elif act == "del":
            if target in cur:
                cur.remove(target)
                self.config["whitelist_qq"] = ",".join(cur)
                await self.config.save_config()
                yield event.plain_result(f"✅ 已从白名单移除 {target}")
            else:
                yield event.plain_result(f"❌ {target} 不在白名单")
        else:
            yield event.plain_result("❌ 用法：add / del / list")

    # ==================== 消息监听 ====================
    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, *args, **kwargs):
        """
        框架调用时可能传入 self + event + 额外参数。
        使用 *args, **kwargs 兜底吸收所有多余参数。
        """
        gid = self._get_group_id(event)
        if not gid:
            return

        self._buffer(event, gid)

        if self.config.get("flood_detection"):
            await self._flood_check(event, gid)
        if self.config.get("ai_conflict_detection"):
            await self._conflict_check(event, gid)

    def _buffer(self, event: AstrMessageEvent, gid: str):
        self._message_buffer[gid].append({
            "qq": event.message_obj.sender.user_id,
            "name": event.get_sender_name(),
            "msg": event.message_str,
            "ts": time.time()
        })
        win = self.config.get("conflict_window", 10)
        if len(self._message_buffer[gid]) > win * 3:
            self._message_buffer[gid] = self._message_buffer[gid][-win * 2:]

    async def _flood_check(self, event: AstrMessageEvent, gid: str):
        qq = event.message_obj.sender.user_id
        if self._is_protected(qq):
            return

        now = time.time()
        dur = self.config.get("flood_threshold", 5)
        max_n = self.config.get("flood_max_msgs", 3)

        t = self._flood_tracker[gid][qq]
        t.append(now)
        t[:] = [x for x in t if now - x <= dur]

        if len(t) > max_n:
            mute_dur = self.config.get("flood_mute_duration", 60)
            try:
                await self._send(event, f"[CQ:ban,qq={qq},duration={mute_dur}]")
                await self._send(event, f"🔇 [CQ:at,qq={qq}] 刷屏，禁言{mute_dur}s")
                t.clear()
                logger.info(f"[GroupGuardian] 刷屏禁言 {qq}")
            except Exception as e:
                logger.error(f"刷屏禁言失败: {e}")

    async def _conflict_check(self, event: AstrMessageEvent, gid: str):
        now = time.time()
        if now - self._last_ai_analysis.get(gid, 0) < self.config.get("conflict_cooldown", 60):
            return

        buf = self._message_buffer.get(gid, [])
        if len(buf) < self.config.get("conflict_window", 10):
            return

        msgs = buf[-self.config.get("conflict_window", 10):]
        self._last_ai_analysis[gid] = now

        res = await self._ai_analyze(msgs)
        if not res or not res.get("is_conflict"):
            return
        lv = res.get("conflict_level", 0)
        if lv < 3:
            return

        users = [str(u) for u in res.get("involved_users", []) if not self._is_protected(str(u))]
        if not users:
            return

        logger.info(f"[GroupGuardian] 冲突 Lv{lv}: {users}")

        if self.config.get("ai_auto_warn"):
            ats = " ".join(f"[CQ:at,qq={u}]" for u in users)
            await self._send(event,
                f"{ats}\n⚠️ 智能群管警告\n等级：{lv}/5\n原因：{res.get('reason','冲突')}\n继续冲突将自动禁言")
            self._warned_users.setdefault(gid, {})
            for u in users:
                self._warned_users[gid][u] = now

        if self.config.get("ai_auto_mute_after_warn"):
            for u in users:
                last = self._warned_users.get(gid, {}).get(u, 0)
                if 10 < now - last < 300:
                    d = self.config.get("ai_mute_duration", 300)
                    try:
                        await self._send(event, f"[CQ:ban,qq={u},duration={d}]")
                        await self._send(event, f"🔇 [CQ:at,qq={u}] 警告后继续冲突，禁言{d}s")
                        logger.info(f"[GroupGuardian] 自动禁言 {u}")
                    except Exception as e:
                        logger.error(f"自动禁言失败: {e}")

    # ==================== 发送辅助 ====================
    async def _send(self, event: AstrMessageEvent, text: str):
        try:
            await self.context.send_message(
                event.unified_msg_origin,
                MessageChain([Plain(text)])
            )
        except Exception as e:
            logger.error(f"[GroupGuardian] 发送失败: {e}")

    # ==================== AI 分析 ====================
    async def _ai_analyze(self, msgs: List[dict]) -> Optional[dict]:
        try:
            ctx = "\n".join(
                f"[{m.get('name','?')}](QQ:{m.get('qq','')}): {m.get('msg','')}"
                for m in msgs
            )
            prompt = (
                "你是群聊管理助手。判断是否存在吵架/冲突。\n"
                f"消息：\n{ctx[-3000:]}\n"
                "只回复JSON：\n"
                '{"is_conflict":bool,"conflict_level":0-5,"involved_users":["qq"],'
                '"reason":"原因","recommend_action":"none/warn/mute/kick"}'
            )
            model = self.config.get("ai_model") or None
            if hasattr(self.context, 'get_using_provider'):
                p = self.context.get_using_provider()
                if p:
                    r = await p.text_chat(prompt=prompt, model=model, temperature=0.3, max_tokens=300)
                    txt = r.get('content', str(r)) if isinstance(r, dict) else str(r)
                    return self._parse(txt)
            return self._fallback()
        except Exception as e:
            logger.error(f"AI分析失败: {e}")
            return None

    def _parse(self, text: str) -> Optional[dict]:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        m = re.search(r'\{[^{}]*\}', text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
        return None

    def _fallback(self) -> dict:
        return {"is_conflict": False, "conflict_level": 0, "involved_users": [],
                "reason": "规则判断", "recommend_action": "none"}

    async def terminate(self):
        logger.info("[GroupGuardian] 已卸载")