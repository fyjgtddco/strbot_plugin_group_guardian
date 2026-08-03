"""
AstrBot 智能群管插件 (GroupGuardian)
=====================================
所有指令（支持 at 机器人）均在 on_llm_request 中处理，
匹配成功后阻止 LLM 回复。

指令：
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
from astrbot.api.message_components import Plain


class GroupGuardianPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._last_analysis: dict = {}
        self._warned: dict = {}
        self._msg_buf: dict = defaultdict(list)
        self._flood: dict = defaultdict(lambda: defaultdict(list))
        logger.info("[GroupGuardian] ✅ 初始化完成")

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

    def _cfg(self, key, default=None):
        return self.config.get(key, default)

    def _extract_at(self, msg: str) -> Optional[str]:
        m = re.search(r'\[CQ:at,qq=(\d+)\]', msg)
        return m.group(1) if m else None

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

    def _strip_cq_at(self, msg: str) -> str:
        """去掉消息中的 [CQ:at,qq=xxx]，提取纯文本指令"""
        return re.sub(r'\[CQ:at,qq=\d+\]', '', msg).strip()

    async def _reply(self, event: AstrMessageEvent, text: str):
        """发送文本回复"""
        try:
            await self.context.send_message(
                event.unified_msg_origin,
                MessageChain([Plain(text)])
            )
        except Exception as e:
            logger.error(f"[GroupGuardian] 回复失败: {e}")

    # ==================== 统一消息处理（on_llm_request） ====================
    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, *args, **kwargs):
        raw = event.message_str
        clean = self._strip_cq_at(raw)

        # ---- 指令判断（匹配后阻止 LLM） ----
        if clean.startswith("/群管帮助"):
            event.stop_event()
            await self._reply(event,
                "🛡️ 智能群管插件\n"
                "/禁言 @xxx 秒数 | /解禁 @xxx | /踢人 @xxx | /全体禁言\n"
                "/群管白名单 add/del/list | /群管状态 | /群管帮助\n"
                "AI自动：冲突检测→警告→升级禁言 | 刷屏检测→禁言")
            return

        if clean.startswith("/群管状态"):
            event.stop_event()
            a = self._admins()
            w = self._whitelist()
            await self._reply(event,
                f"🛡️ 群管状态\n"
                f"AI冲突：{'✅' if self._cfg('ai_conflict_detection') else '❌'} | "
                f"刷屏检测：{'✅' if self._cfg('flood_detection') else '❌'}\n"
                f"主动警告：{'✅' if self._cfg('ai_auto_warn') else '❌'} | "
                f"警告后禁言：{'✅' if self._cfg('ai_auto_mute_after_warn') else '❌'}\n"
                f"自动禁言：{self._cfg('ai_mute_duration')}s\n"
                f"管理员：{', '.join(a) if a else '未设置'}\n"
                f"白名单：{len(w)}人")
            return

        if clean.startswith("/禁言"):
            event.stop_event()
            await self._handle_mute(event, raw)
            return

        if clean.startswith("/解禁"):
            event.stop_event()
            await self._handle_unmute(event, raw)
            return

        if clean.startswith("/踢人"):
            event.stop_event()
            await self._handle_kick(event, raw)
            return

        if clean.startswith("/全体禁言"):
            event.stop_event()
            await self._handle_whole(event)
            return

        if clean.startswith("/群管白名单"):
            event.stop_event()
            await self._handle_wl(event, raw)
            return

        # ---- 非指令消息：AI 检测 ----
        gid = self._gid(event)
        if not gid:
            return

        self._buf(event, gid)
        if self._cfg("flood_detection"):
            await self._flood_check(event, gid)
        if self._cfg("ai_conflict_detection"):
            await self._conflict_check(event, gid)

    # ==================== 指令处理 ====================
    async def _handle_mute(self, event, raw):
        sender = event.message_obj.sender.user_id
        if not self._is_admin(sender):
            await self._reply(event, "❌ 权限不足")
            return
        t = self._extract_at(raw)
        if not t:
            await self._reply(event, "❌ 请 @ 要禁言的成员")
            return
        if self._is_protected(t):
            await self._reply(event, "❌ 该成员受保护")
            return
        dur = 60
        m = re.search(r'(\d+)\s*$', raw)
        if m:
            dur = int(m.group(1))
        await self._reply(event, f"[CQ:ban,qq={t},duration={dur}]")
        await self._reply(event, f"✅ 已禁言 {t}，{dur}s")

    async def _handle_unmute(self, event, raw):
        if not self._is_admin(event.message_obj.sender.user_id):
            await self._reply(event, "❌ 权限不足")
            return
        t = self._extract_at(raw)
        if not t:
            await self._reply(event, "❌ 请 @ 要解禁的成员")
            return
        await self._reply(event, f"[CQ:ban,qq={t},duration=0]")
        await self._reply(event, f"✅ 已解禁 {t}")

    async def _handle_kick(self, event, raw):
        sender = event.message_obj.sender.user_id
        if not self._is_admin(sender):
            await self._reply(event, "❌ 权限不足")
            return
        t = self._extract_at(raw)
        if not t:
            await self._reply(event, "❌ 请 @ 要踢出的成员")
            return
        if self._is_protected(t):
            await self._reply(event, "❌ 该成员受保护")
            return
        await self._reply(event, f"[CQ:kick,qq={t}]")
        await self._reply(event, f"✅ 已踢出 {t}")

    async def _handle_whole(self, event):
        if not self._is_admin(event.message_obj.sender.user_id):
            await self._reply(event, "❌ 权限不足")
            return
        await self._reply(event, "[CQ:whole_ban]")
        await self._reply(event, "✅ 已切换全体禁言")

    async def _handle_wl(self, event, raw):
        if not self._is_admin(event.message_obj.sender.user_id):
            await self._reply(event, "❌ 权限不足")
            return
        clean = self._strip_cq_at(raw)
        parts = clean.split()
        act = parts[1] if len(parts) > 1 else "list"

        if act == "list":
            w = self._whitelist()
            await self._reply(event, f"📋 白名单({len(w)}人)：\n" + "\n".join(w) if w else "📋 白名单为空")
            return

        target = self._extract_at(raw) or (parts[2] if len(parts) > 2 else "")
        if not target or not target.isdigit():
            await self._reply(event, "❌ 请提供有效QQ号")
            return

        cur = [x.strip() for x in self.config.get("whitelist_qq", "").split(",") if x.strip()]

        if act == "add":
            if target in cur:
                await self._reply(event, "⚠️ 已在白名单")
            else:
                cur.append(target)
                self.config["whitelist_qq"] = ",".join(cur)
                await self.config.save_config()
                await self._reply(event, f"✅ 已添加 {target}")
        elif act == "del":
            if target in cur:
                cur.remove(target)
                self.config["whitelist_qq"] = ",".join(cur)
                await self.config.save_config()
                await self._reply(event, f"✅ 已移除 {target}")
            else:
                await self._reply(event, f"❌ {target} 不在白名单")
        else:
            await self._reply(event, "❌ 用法：add / del / list")

    # ==================== AI 检测（同前） ====================
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
            await self._reply(event, f"[CQ:ban,qq={qq},duration={dur}]")
            await self._reply(event, f"🔇 [CQ:at,qq={qq}] 刷屏，禁言{dur}s")
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
            await self._reply(event, f"{ats}\n⚠️ 智能群管警告\n等级：{lv}/5\n原因：{res.get('reason','冲突')}\n继续冲突将自动禁言")
            self._warned.setdefault(gid, {})
            for u in users:
                self._warned[gid][u] = now

        if self._cfg("ai_auto_mute_after_warn"):
            for u in users:
                last = self._warned.get(gid, {}).get(u, 0)
                if 10 < now - last < 300:
                    d = self._cfg("ai_mute_duration", 300)
                    await self._reply(event, f"[CQ:ban,qq={u},duration={d}]")
                    await self._reply(event, f"🔇 [CQ:at,qq={u}] 警告后继续冲突，禁言{d}s")
                    logger.info(f"[GroupGuardian] 自动禁言 {u}")

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