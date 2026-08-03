"""
AstrBot 智能群管插件 (GroupGuardian)
=====================================
指令（纯文本或 @机器人 + 指令均可）：
  /群管帮助               → 帮助
  /群管状态               → 状态
  /禁言 @xxx <秒数>       → 禁言
  /解禁 @xxx              → 解禁
  /踢人 @xxx              → 踢出
  /全体禁言               → 切换全体禁言
  /群管白名单 add @xxx    → 加白名单
  /群管白名单 del @xxx    → 删白名单
  /群管白名单 list        → 查白名单
"""

import re
import time
import json
from typing import Optional, Set, List
from collections import defaultdict

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star
from astrbot.api import logger, AstrBotConfig
from astrbot.api.message_components import Plain, At


class GroupGuardianPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._last_analysis: dict = {}
        self._warned: dict = {}
        self._msg_buf: dict = defaultdict(list)
        self._flood: dict = defaultdict(lambda: defaultdict(list))
        self._bot_id: str = self._get_bot_id()
        logger.info(f"[GroupGuardian] ✅ 初始化完成，机器人QQ: {self._bot_id}")

    def _get_bot_id(self) -> str:
        """尝试获取机器人自己的QQ号"""
        try:
            if hasattr(self.context, 'get_using_provider'):
                p = self.context.get_using_provider()
                if hasattr(p, 'get_self_id'):
                    return str(p.get_self_id())
        except Exception:
            pass
        return ""

    # ==================== 配置 ====================
    def _admins(self) -> Set[str]:
        s = self.config.get("admin_qq", "")
        return {x.strip() for x in s.split(",") if x.strip()} if s.strip() else set()

    def _whitelist(self) -> Set[str]:
        s = self.config.get("whitelist_qq", "")
        return {x.strip() for x in s.split(",") if x.strip()} if s.strip() else set()

    def _is_admin(self, qq: str) -> bool:
        return qq in self._admins()

    def _is_whitelisted(self, qq: str) -> bool:
        return qq in self._whitelist()

    def _is_protected(self, qq: str) -> bool:
        return self._is_admin(qq) or self._is_whitelisted(qq)

    def _cfg(self, key: str, default=None):
        return self.config.get(key, default)

    def _extract_at(self, msg: str) -> Optional[str]:
        m = re.search(r'\[CQ:at,qq=(\d+)\]', msg)
        if m:
            return m.group(1)
        m = re.search(r'@(\d{5,11})', msg)
        if m:
            return m.group(1)
        return None

    def _gid(self, event: AstrMessageEvent) -> Optional[str]:
        try:
            g = event.message_obj.group_id
            return str(g) if g else None
        except Exception:
            pass
        try:
            return event.get_group_id()
        except Exception:
            pass
        return None

    # ==================== @filter.message 全量监听 ====================
    @filter.message()
    async def on_message(self, event: AstrMessageEvent):
        """
        全量消息监听，处理「@机器人 /指令」格式的消息。
        @filter.command 只匹配纯文本 /指令，这里补充处理带 at 的情况。
        """
        raw = event.message_str.strip()
        bot_id = self._bot_id

        if not bot_id:
            return

        # 检查消息是否以 @机器人 开头
        at_bot = f"[CQ:at,qq={bot_id}]"
        if raw.startswith(at_bot):
            # 去掉 at 前缀，拿到纯指令
            cmd_str = raw[len(at_bot):].strip()
            await self._dispatch(event, cmd_str)

    def _has_at_prefix(self, raw: str) -> bool:
        """检查消息是否有 at 机器人的前缀"""
        bot_id = self._bot_id
        if not bot_id:
            return False
        return raw.startswith(f"[CQ:at,qq={bot_id}]") or raw.startswith(f"@{bot_id}")

    async def _dispatch(self, event: AstrMessageEvent, cmd_str: str):
        """手动解析并分发指令"""
        parts = cmd_str.split()
        if not parts:
            return
        cmd = parts[0]

        # 去掉 / 前缀（兼容 /群管帮助 和 群管帮助）
        if cmd.startswith("/") or cmd.startswith("！"):
            cmd = cmd[1:]
            parts[0] = cmd

        # 路由
        if cmd == "群管帮助":
            yield self._mk_reply(event, self._help_text())
        elif cmd == "群管状态":
            yield self._mk_reply(event, self._status_text())
        elif cmd == "禁言":
            yield self._mk_reply(event, await self._do_mute(event, parts))
        elif cmd == "解禁":
            yield self._mk_reply(event, await self._do_unmute(event, parts))
        elif cmd == "踢人":
            yield self._mk_reply(event, await self._do_kick(event, parts))
        elif cmd == "全体禁言":
            yield self._mk_reply(event, await self._do_whole(event))
        elif cmd == "群管白名单":
            yield self._mk_reply(event, await self._do_wl(event, parts))
        elif cmd == "群管白名单":
            yield self._mk_reply(event, await self._do_wl(event, parts))

    def _mk_reply(self, event, text):
        """构建回复消息"""
        return MessageChain([Plain(text)])

    # ==================== 指令文本生成 ====================
    def _help_text(self) -> str:
        return (
            "🛡️ 智能群管插件\n"
            "/禁言 @xxx 秒数 | /解禁 @xxx | /踢人 @xxx | /全体禁言\n"
            "/群管白名单 add/del/list | /群管状态 | /群管帮助\n"
            "AI自动：冲突检测→警告→升级禁言 | 刷屏检测→禁言"
        )

    def _status_text(self) -> str:
        a = self._admins()
        w = self._whitelist()
        return (
            f"🛡️ 群管状态\n"
            f"AI冲突：{'✅' if self._cfg('ai_conflict_detection') else '❌'} | "
            f"刷屏检测：{'✅' if self._cfg('flood_detection') else '❌'}\n"
            f"主动警告：{'✅' if self._cfg('ai_auto_warn') else '❌'} | "
            f"警告后禁言：{'✅' if self._cfg('ai_auto_mute_after_warn') else '❌'}\n"
            f"自动禁言：{self._cfg('ai_mute_duration')}s\n"
            f"管理员：{', '.join(a) if a else '未设置'}\n"
            f"白名单：{len(w)}人"
        )

    # ==================== 指令执行 ====================
    async def _do_mute(self, event, parts: list) -> str:
        sender = event.message_obj.sender.user_id
        if not self._is_admin(sender):
            return "❌ 权限不足"
        t = self._extract_at(event.message_str)
        if not t:
            return "❌ 请 @ 要禁言的成员"
        if self._is_protected(t):
            return "❌ 该成员受保护"
        dur = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 60
        chain = MessageChain([Plain(f"[CQ:ban,qq={t},duration={dur}]")])
        try:
            await self.context.send_message(event.unified_msg_origin, chain)
        except Exception:
            pass
        return f"✅ 已禁言 {t}，{dur}s"

    async def _do_unmute(self, event, parts: list) -> str:
        if not self._is_admin(event.message_obj.sender.user_id):
            return "❌ 权限不足"
        t = self._extract_at(event.message_str)
        if not t:
            return "❌ 请 @ 要解禁的成员"
        chain = MessageChain([Plain(f"[CQ:ban,qq={t},duration=0]")])
        try:
            await self.context.send_message(event.unified_msg_origin, chain)
        except Exception:
            pass
        return f"✅ 已解禁 {t}"

    async def _do_kick(self, event, parts: list) -> str:
        sender = event.message_obj.sender.user_id
        if not self._is_admin(sender):
            return "❌ 权限不足"
        t = self._extract_at(event.message_str)
        if not t:
            return "❌ 请 @ 要踢出的成员"
        if self._is_protected(t):
            return "❌ 该成员受保护"
        chain = MessageChain([Plain(f"[CQ:kick,qq={t}]")])
        try:
            await self.context.send_message(event.unified_msg_origin, chain)
        except Exception:
            pass
        return f"✅ 已踢出 {t}"

    async def _do_whole(self, event) -> str:
        if not self._is_admin(event.message_obj.sender.user_id):
            return "❌ 权限不足"
        chain = MessageChain([Plain("[CQ:whole_ban]")])
        try:
            await self.context.send_message(event.unified_msg_origin, chain)
        except Exception:
            pass
        return "✅ 已切换全体禁言"

    async def _do_wl(self, event, parts: list) -> str:
        if not self._is_admin(event.message_obj.sender.user_id):
            return "❌ 权限不足"
        act = parts[1] if len(parts) > 1 else "list"

        if act == "list":
            w = self._whitelist()
            return f"📋 白名单({len(w)}人)：\n" + "\n".join(w) if w else "📋 白名单为空"

        target = self._extract_at(event.message_str) or (parts[2] if len(parts) > 2 else "")
        if not target or not target.isdigit():
            return "❌ 请提供有效QQ号"

        cur = [x.strip() for x in self.config.get("whitelist_qq", "").split(",") if x.strip()]

        if act == "add":
            if target in cur:
                return "⚠️ 已在白名单"
            cur.append(target)
            self.config["whitelist_qq"] = ",".join(cur)
            await self.config.save_config()
            return f"✅ 已添加 {target}"
        elif act == "del":
            if target in cur:
                cur.remove(target)
                self.config["whitelist_qq"] = ",".join(cur)
                await self.config.save_config()
                return f"✅ 已移除 {target}"
            else:
                return f"❌ {target} 不在白名单"
        else:
            return "❌ 用法：add / del / list"

    # ==================== @filter.command（纯文本指令） ====================
    @filter.command("群管帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        yield event.plain_result(self._help_text())

    @filter.command("群管状态")
    async def cmd_status(self, event: AstrMessageEvent):
        yield event.plain_result(self._status_text())

    @filter.command("禁言")
    async def cmd_mute(self, event: AstrMessageEvent, target: str = "", duration: int = 60):
        sender = event.message_obj.sender.user_id
        if not self._is_admin(sender):
            yield event.plain_result("❌ 权限不足")
            return
        t = self._extract_at(event.message_str)
        if not t:
            yield event.plain_result("❌ 请 @ 要禁言的成员")
            return
        if self._is_protected(t):
            yield event.plain_result("❌ 该成员受保护")
            return
        yield MessageChain([Plain(f"[CQ:ban,qq={t},duration={duration}]")])
        yield event.plain_result(f"✅ 已禁言 {t}，{duration}s")

    @filter.command("解禁")
    async def cmd_unmute(self, event: AstrMessageEvent):
        if not self._is_admin(event.message_obj.sender.user_id):
            yield event.plain_result("❌ 权限不足")
            return
        t = self._extract_at(event.message_str)
        if not t:
            yield event.plain_result("❌ 请 @ 要解禁的成员")
            return
        yield MessageChain([Plain(f"[CQ:ban,qq={t},duration=0]")])
        yield event.plain_result(f"✅ 已解禁 {t}")

    @filter.command("踢人")
    async def cmd_kick(self, event: AstrMessageEvent):
        sender = event.message_obj.sender.user_id
        if not self._is_admin(sender):
            yield event.plain_result("❌ 权限不足")
            return
        t = self._extract_at(event.message_str)
        if not t:
            yield event.plain_result("❌ 请 @ 要踢出的成员")
            return
        if self._is_protected(t):
            yield event.plain_result("❌ 该成员受保护")
            return
        yield MessageChain([Plain(f"[CQ:kick,qq={t}]")])
        yield event.plain_result(f"✅ 已踢出 {t}")

    @filter.command("全体禁言")
    async def cmd_whole(self, event: AstrMessageEvent):
        if not self._is_admin(event.message_obj.sender.user_id):
            yield event.plain_result("❌ 权限不足")
            return
        yield MessageChain([Plain("[CQ:whole_ban]")])
        yield event.plain_result("✅ 已切换全体禁言")

    @filter.command("群管白名单")
    async def cmd_wl(self, event: AstrMessageEvent):
        sender = event.message_obj.sender.user_id
        if not self._is_admin(sender):
            yield event.plain_result("❌ 权限不足")
            return
        parts = event.message_str.strip().split()
        act = parts[1] if len(parts) > 1 else "list"

        if act == "list":
            w = self._whitelist()
            yield event.plain_result(
                f"📋 白名单({len(w)}人)：\n" + "\n".join(w) if w else "📋 白名单为空"
            )
            return

        target = self._extract_at(event.message_str) or (parts[2] if len(parts) > 2 else "")
        if not target or not target.isdigit():
            yield event.plain_result("❌ 请提供有效QQ号")
            return

        cur = [x.strip() for x in self.config.get("whitelist_qq", "").split(",") if x.strip()]

        if act == "add":
            if target in cur:
                yield event.plain_result("⚠️ 已在白名单")
            else:
                cur.append(target)
                self.config["whitelist_qq"] = ",".join(cur)
                await self.config.save_config()
                yield event.plain_result(f"✅ 已添加 {target}")
        elif act == "del":
            if target in cur:
                cur.remove(target)
                self.config["whitelist_qq"] = ",".join(cur)
                await self.config.save_config()
                yield event.plain_result(f"✅ 已移除 {target}")
            else:
                yield event.plain_result(f"❌ {target} 不在白名单")
        else:
            yield event.plain_result("❌ 用法：add / del / list")

    # ==================== on_llm_request ====================
    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, *args, **kwargs):
        # 如果消息是 @机器人 的指令，阻止 LLM 处理
        if self._has_at_prefix(event.message_str.strip()):
            event.stop_event()
            return

        gid = self._gid(event)
        if not gid:
            return

        self._buf(event, gid)
        if self._cfg("flood_detection"):
            await self._flood_check(event, gid)
        if self._cfg("ai_conflict_detection"):
            await self._conflict_check(event, gid)

    def _buf(self, event, gid):
        self._msg_buf[gid].append({
            "qq": event.message_obj.sender.user_id,
            "name": event.get_sender_name(),
            "msg": event.message_str,
            "ts": time.time()
        })
        w = self._cfg("conflict_window", 10)
        if len(self._msg_buf[gid]) > w * 3:
            self._msg_buf[gid] = self._msg_buf[gid][-w * 2:]

    async def _flood_check(self, event, gid):
        qq = event.message_obj.sender.user_id
        if self._is_protected(qq):
            return
        now = time.time()
        win = self._cfg("flood_threshold", 5)
        mx = self._cfg("flood_max_msgs", 3)
        t = self._flood[gid][qq]
        t.append(now)
        t[:] = [x for x in t if now - x <= win]
        if len(t) > mx:
            dur = self._cfg("flood_mute_duration", 60)
            await self._send(event, f"[CQ:ban,qq={qq},duration={dur}]")
            await self._send(event, f"🔇 [CQ:at,qq={qq}] 刷屏，禁言{dur}s")
            t.clear()
            logger.info(f"[GroupGuardian] 刷屏禁言 {qq}")

    async def _conflict_check(self, event, gid):
        now = time.time()
        if now - self._last_analysis.get(gid, 0) < self._cfg("conflict_cooldown", 60):
            return
        buf = self._msg_buf.get(gid, [])
        w = self._cfg("conflict_window", 10)
        if len(buf) < w:
            return
        msgs = buf[-w:]
        self._last_analysis[gid] = now
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

        if self._cfg("ai_auto_warn"):
            ats = " ".join(f"[CQ:at,qq={u}]" for u in users)
            await self._send(event, f"{ats}\n⚠️ 智能群管警告\n等级：{lv}/5\n原因：{res.get('reason','冲突')}\n继续冲突将自动禁言")
            self._warned.setdefault(gid, {})
            for u in users:
                self._warned[gid][u] = now

        if self._cfg("ai_auto_mute_after_warn"):
            for u in users:
                last = self._warned.get(gid, {}).get(u, 0)
                if 10 < now - last < 300:
                    d = self._cfg("ai_mute_duration", 300)
                    await self._send(event, f"[CQ:ban,qq={u},duration={d}]")
                    await self._send(event, f"🔇 [CQ:at,qq={u}] 警告后继续冲突，禁言{d}s")
                    logger.info(f"[GroupGuardian] 自动禁言 {u}")

    async def _send(self, event, text):
        try:
            await self.context.send_message(
                event.unified_msg_origin,
                MessageChain([Plain(text)])
            )
        except Exception as e:
            logger.error(f"发送失败: {e}")

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
            if hasattr(self.context, 'get_using_provider'):
                p = self.context.get_using_provider()
                if p:
                    r = await p.text_chat(prompt=prompt)
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